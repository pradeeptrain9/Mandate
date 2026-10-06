#!/usr/bin/env python3
"""Fill an empty ledger with a month of plausible history.

A dashboard with no rows demonstrates nothing, and `docker compose up` on a clean
clone starts with no rows. This writes some.

What it does *not* do is write JSON. Every record here is produced by the real
engine from a real signed quote, which matters for two reasons. The ledger's claim
is that it replays -- `mandate replay --all` re-runs each stored decision through
the live engine and fails on any divergence -- and hand-written fixtures would
either break that or quietly teach someone that the ledger can be edited. And the
outcomes are not scripted: the script asks for baskets and prints whatever the
engine decides, because the interesting rows are the ones where history changed
the verdict, and predicting those by hand is how you get a seed that stops
matching the policy.

Quotes are built and signed in process rather than fetched from the merchant stub,
so seeding needs no service running. They use the stub's own catalog, so the SKUs,
prices and categories are the same ones the demo shows.

With no PayPal credentials every refusal and every approval-threshold decision seeds
exactly as it would in production, and the allowed ones record that they were allowed
and could not be placed.

One consequence of that is worth knowing before you look at the result and think the
seed is broken. `Store.ledger_window` only counts holds where an order was actually
created -- a refused or unplaced decision consumed no budget and reached for nothing,
which is correct -- so with no credentials *nothing* contributes history. The budget
envelopes read zero, the velocity counter reads zero, and the duplicate-intent rule
cannot fire however many identical baskets are seeded. Those three rules only have
something to say once holds exist, which means sandbox credentials. The refusals that
depend on the basket alone -- denied category, the caps, the thresholds -- are
complete either way, and those are the ones the project is about.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mandate.engine.money import Money
from mandate.engine.policy import Outcome
from mandate.engine.quote import LineItem, MerchantQuote
from mandate.gateway.service import AuthorizationRequest, Gateway, GatewayError
from mandate.gateway.store import Store
from mandate.ledger.records import Ledger
from mandate.merchant.catalog import MERCHANTS
from mandate.policies import demo_policy
from mandate.providers.paypal import LIVE, SANDBOX, PayPalClient

#: (minutes before now, merchant, [(sku, quantity)], who asked and why).
#:
#: Spread over a month on purpose: the rolling envelopes and the velocity window
#: only mean anything against history, and a ledger whose rows all share one
#: timestamp makes the budget burn-down a flat line.
BASKETS: tuple[tuple[int, str, list[tuple[str, int]], str], ...] = (
    (26 * 24 * 60, "m_acme", [("SKU-TONER", 1)], "printer out of toner"),
    (20 * 24 * 60, "m_cloudspend", [("SKU-SEAT-CI", 2)], "two more CI runners for the release"),
    (12 * 24 * 60, "m_acme", [("SKU-PAPER-A4", 4), ("SKU-STAPLER", 1)], "quarterly stationery"),
    (9 * 24 * 60, "m_acme", [("SKU-DESK-LAMP", 1)], "desk lamp for the new starter"),
    # Over the $100 unattended threshold: parks and pages a human.
    (5 * 24 * 60, "m_cloudspend", [("SKU-GPU-A100", 1)], "GPU hours for the nightly training run"),
    # Gift cards are on the deny list, and 40 of them clears every cap at once.
    (3 * 24 * 60, "m_acme", [("SKU-GC100", 40)], "team rewards, approved by the account holder"),
    # A registered, allowed merchant that never ships. The hold is meant to be
    # taken and then released, which is what the sweep is for.
    (2 * 24 * 60, "m_ghost", [("SKU-CABLE-2M", 1)], "USB-C cable for the demo rig"),
    # Over the hard per-transaction cap: no human can approve this tier.
    (26 * 60, "m_acme", [("SKU-TONER", 20)], "bulk toner, end of budget year"),
    # Over the compute category cap, though under the merchant's own ceiling.
    (90, "m_cloudspend", [("SKU-GPU-A100", 2)], "two GPUs for the evaluation sweep"),
    (40, "m_acme", [("SKU-PAPER-A4", 3)], "we are out of A4 paper"),
    # The same basket 15 minutes later, inside the 30-minute duplicate window. A
    # retry, or an agent that lost track of whether it already bought the paper.
    # Not 30 minutes: that is the window's edge and lands outside it, which is a
    # mistake worth leaving a note about because the seed looked right either way.
    (25, "m_acme", [("SKU-PAPER-A4", 3)], "we are out of A4 paper"),
)


def sign_quote(merchant_id: str, lines: list[tuple[str, int]], *, at: datetime, secret: bytes):
    """Build the quote the merchant stub would have built, and sign it the same way."""
    seller = MERCHANTS[merchant_id]
    by_sku = {p.sku: p for p in seller.products}
    items = []
    for sku, quantity in lines:
        product = by_sku.get(sku)
        if product is None:
            raise SystemExit(f"{merchant_id} has no {sku}; the catalog changed and this seed did not")
        items.append(
            LineItem(product.sku, product.description, product.category, product.price("USD"), quantity)
        )
    items = tuple(items)
    return MerchantQuote(
        quote_id=f"q_{uuid.uuid4().hex[:12]}",
        merchant_id=seller.merchant_id,
        merchant_name=seller.name,
        currency="USD",
        line_items=items,
        declared_total=Money(sum(i.line_total.minor for i in items), "USD"),
        # Issued a minute before it is presented. The policy refuses a quote older
        # than ten minutes, so a seed stamped "now" for a decision dated last week
        # would be refused for staleness and teach nothing about the real rule.
        issued_at=at - timedelta(minutes=1),
        nonce=uuid.uuid4().hex[:16],
    ).sign(secret)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--var-dir",
        default=os.environ.get("MANDATE_VAR_DIR", "var"),
        help="where the ledger and the hold database live",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="seed even if the ledger already has records",
    )
    args = parser.parse_args()

    ledger_key = os.environ.get("MANDATE_LEDGER_KEY", "")
    merchant_secret = os.environ.get("MANDATE_MERCHANT_SECRET", "")
    if not ledger_key or not merchant_secret:
        print(
            "MANDATE_LEDGER_KEY and MANDATE_MERCHANT_SECRET are both required.\n"
            "Run scripts/bootstrap_env.sh, then `set -a; . ./.env; set +a`.",
            file=sys.stderr,
        )
        return 2

    var = Path(args.var_dir)
    var.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(var / "decisions.jsonl", ledger_key.encode())
    existing = len(list(ledger))
    if existing and not args.force:
        print(
            f"{var / 'decisions.jsonl'} already has {existing} record(s).\n"
            "Seeding would append to real history, which is the one thing an\n"
            "append-only ledger makes irreversible. Pass --force if that is what\n"
            "you want, or run scripts/reset_demo.sh --yes first.",
            file=sys.stderr,
        )
        return 1

    client_id = os.environ.get("PAYPAL_CLIENT_ID", "")
    client_secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")
    live = os.environ.get("PAYPAL_ENV", "sandbox").lower() == "live"
    paypal = (
        PayPalClient(client_id, client_secret, base_url=LIVE if live else SANDBOX)
        if client_id and client_secret
        else None
    )
    if paypal is None:
        print(
            "No PayPal credentials.\n"
            "  Allowed decisions will record that they could not be placed, and because\n"
            "  no order is ever created, nothing seeded here counts as prior authorization:\n"
            "  the envelopes, the velocity counter and duplicate_intent will all read empty.\n"
            "  Add PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET for a seed with history.\n"
        )

    store = Store(var / "state.db")
    gateway = Gateway(
        store=store,
        ledger=ledger,
        policy=demo_policy(),
        merchant_secret=merchant_secret.encode(),
        paypal=paypal,
        public_url=os.environ.get("MANDATE_PUBLIC_URL", "http://localhost:8000"),
    )

    now = datetime.now(UTC)
    counts: dict[str, int] = {}
    try:
        for minutes_ago, merchant_id, lines, reason in BASKETS:
            at = now - timedelta(minutes=minutes_ago)
            quote = sign_quote(merchant_id, lines, at=at, secret=merchant_secret.encode())
            basket = ", ".join(f"{q}×{sku}" for sku, q in lines)
            try:
                result = await gateway.request_authorization(
                    AuthorizationRequest(quote=quote, reason=reason, agent_id="seed-ops-agent"),
                    now=at,
                )
                outcome, detail = result.outcome.value, ""
                if result.outcome is not Outcome.ALLOW:
                    # Why, not just what. A seed whose rows say "deny" and nothing
                    # else is a seed that cannot be checked against the policy.
                    detail = ", ".join(result.evaluation.reason_ids)
            except GatewayError as exc:
                # Only the allow path reaches PayPal, so this branch is an allow
                # whose order could not be created. The record is already written
                # and already replays; only the money step did not happen.
                outcome, detail = "allow (unplaced)", str(exc)
            counts[outcome] = counts.get(outcome, 0) + 1
            stamp = at.strftime("%Y-%m-%d %H:%M")
            print(f"  {stamp}  {quote.declared_total.to_paypal():>8} USD  {outcome:<18} {basket}")
            if detail:
                print(f"{'':>31}{detail}")
    finally:
        store.close()
        if paypal is not None:
            await paypal.aclose()

    print()
    print("  " + "  ".join(f"{name}: {n}" for name, n in sorted(counts.items())))
    print()
    print(f"  Check it:    mandate --ledger {var / 'decisions.jsonl'} verify")
    print(f"               mandate --ledger {var / 'decisions.jsonl'} replay -v")
    print("  Look at it:  http://localhost:8000/v1/ops/dashboard")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
