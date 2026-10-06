"""Canonical serialisation for everything a decision was made from.

A decision record is only worth something if it can be replayed, and it can only
be replayed if the inputs round-trip exactly. So the encoding here is explicit
and boring: one function per type, no reflection, no pickling, no "whatever
`default=str` does". When a field is added to `Policy`, this module fails to
compile against it or the round-trip test fails -- which is the behaviour we
want, because the alternative is a record that silently drops the new field and
replays green against a policy that is no longer the one that ran.

`canonical_json` is byte-stable: sorted keys, no insignificant whitespace,
ASCII-escaped. The HMAC in `records.py` is taken over its output, so two
structurally identical records always produce the same signature regardless of
dict insertion order or Python version.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ..engine.money import Money
from ..engine.policy import (
    Envelope,
    Evaluation,
    LedgerWindow,
    MerchantRule,
    Outcome,
    Policy,
    PriorAuthorization,
    RuleResult,
)
from ..engine.quote import Category, LineItem, MerchantQuote, PolicyInput


def canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


# -- scalars ----------------------------------------------------------------


def enc_money(value: Money) -> dict[str, Any]:
    return {"minor": value.minor, "currency": value.currency}


def dec_money(raw: dict[str, Any]) -> Money:
    return Money(int(raw["minor"]), str(raw["currency"]))


def enc_dt(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("refusing to serialise a naive datetime; a record without a zone is a guess")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def dec_dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def enc_td(value: timedelta) -> int:
    """Whole seconds. Every duration in a policy is coarse by design."""
    seconds = value.total_seconds()
    if seconds != int(seconds):
        raise ValueError(f"sub-second duration {value!r} cannot be a policy window")
    return int(seconds)


def dec_td(raw: int) -> timedelta:
    return timedelta(seconds=int(raw))


# -- quote ------------------------------------------------------------------


def enc_line_item(item: LineItem) -> dict[str, Any]:
    return {
        "sku": item.sku,
        "description": item.description,
        "category": item.category.value,
        "unit_price": enc_money(item.unit_price),
        "quantity": item.quantity,
    }


def dec_line_item(raw: dict[str, Any]) -> LineItem:
    return LineItem(
        sku=raw["sku"],
        description=raw["description"],
        category=Category(raw["category"]),
        unit_price=dec_money(raw["unit_price"]),
        quantity=int(raw["quantity"]),
    )


def enc_quote(quote: MerchantQuote) -> dict[str, Any]:
    return {
        "quote_id": quote.quote_id,
        "merchant_id": quote.merchant_id,
        "merchant_name": quote.merchant_name,
        "currency": quote.currency,
        "line_items": [enc_line_item(i) for i in quote.line_items],
        "declared_total": enc_money(quote.declared_total),
        "issued_at": enc_dt(quote.issued_at),
        "nonce": quote.nonce,
        "signature": quote.signature,
        "metadata": dict(quote.metadata),
    }


def dec_quote(raw: dict[str, Any]) -> MerchantQuote:
    return MerchantQuote(
        quote_id=raw["quote_id"],
        merchant_id=raw["merchant_id"],
        merchant_name=raw["merchant_name"],
        currency=raw["currency"],
        line_items=tuple(dec_line_item(i) for i in raw["line_items"]),
        declared_total=dec_money(raw["declared_total"]),
        issued_at=dec_dt(raw["issued_at"]),
        nonce=raw["nonce"],
        signature=raw["signature"],
        metadata=dict(raw["metadata"]),
    )


def enc_policy_input(request: PolicyInput) -> dict[str, Any]:
    return {
        "merchant_id": request.merchant_id,
        "currency": request.currency,
        "amount": enc_money(request.amount),
        "category_subtotals": [
            [category.value, enc_money(amount)] for category, amount in request.category_subtotals
        ],
        "item_count": request.item_count,
        "fingerprint": request.fingerprint,
    }


def dec_policy_input(raw: dict[str, Any]) -> PolicyInput:
    return PolicyInput(
        merchant_id=raw["merchant_id"],
        currency=raw["currency"],
        amount=dec_money(raw["amount"]),
        category_subtotals=tuple(
            (Category(c), dec_money(m)) for c, m in raw["category_subtotals"]
        ),
        item_count=int(raw["item_count"]),
        fingerprint=raw["fingerprint"],
    )


# -- policy -----------------------------------------------------------------


def enc_policy(policy: Policy) -> dict[str, Any]:
    return {
        "policy_id": policy.policy_id,
        "currency": policy.currency,
        "hard_per_transaction_cap": enc_money(policy.hard_per_transaction_cap),
        "approval_threshold": enc_money(policy.approval_threshold),
        "allowed_currencies": sorted(policy.allowed_currencies),
        "allowed_categories": sorted(c.value for c in policy.allowed_categories),
        "denied_categories": sorted(c.value for c in policy.denied_categories),
        "category_caps": [[c.value, enc_money(m)] for c, m in policy.category_caps],
        "merchants": [
            {
                "merchant_id": m.merchant_id,
                "enabled": m.enabled,
                "per_transaction_cap": (
                    enc_money(m.per_transaction_cap) if m.per_transaction_cap else None
                ),
            }
            for m in policy.merchants
        ],
        "envelopes": [
            {"label": e.label, "duration_s": enc_td(e.duration), "cap": enc_money(e.cap)}
            for e in policy.envelopes
        ],
        "velocity_limit": policy.velocity_limit,
        "velocity_window_s": enc_td(policy.velocity_window),
        "duplicate_window_s": enc_td(policy.duplicate_window),
        "quote_max_age_s": enc_td(policy.quote_max_age),
        "version": policy.version,
    }


def dec_policy(raw: dict[str, Any]) -> Policy:
    return Policy(
        policy_id=raw["policy_id"],
        currency=raw["currency"],
        hard_per_transaction_cap=dec_money(raw["hard_per_transaction_cap"]),
        approval_threshold=dec_money(raw["approval_threshold"]),
        allowed_currencies=frozenset(raw["allowed_currencies"]),
        allowed_categories=frozenset(Category(c) for c in raw["allowed_categories"]),
        denied_categories=frozenset(Category(c) for c in raw["denied_categories"]),
        category_caps=tuple((Category(c), dec_money(m)) for c, m in raw["category_caps"]),
        merchants=tuple(
            MerchantRule(
                merchant_id=m["merchant_id"],
                enabled=bool(m["enabled"]),
                per_transaction_cap=(
                    dec_money(m["per_transaction_cap"]) if m["per_transaction_cap"] else None
                ),
            )
            for m in raw["merchants"]
        ),
        envelopes=tuple(
            Envelope(label=e["label"], duration=dec_td(e["duration_s"]), cap=dec_money(e["cap"]))
            for e in raw["envelopes"]
        ),
        velocity_limit=int(raw["velocity_limit"]),
        velocity_window=dec_td(raw["velocity_window_s"]),
        duplicate_window=dec_td(raw["duplicate_window_s"]),
        quote_max_age=dec_td(raw["quote_max_age_s"]),
        version=raw["version"],
    )


# -- ledger window ----------------------------------------------------------


def enc_ledger_window(window: LedgerWindow) -> list[dict[str, Any]]:
    return [
        {
            "at": enc_dt(e.at),
            "merchant_id": e.merchant_id,
            "amount": enc_money(e.amount),
            "fingerprint": e.fingerprint,
            "categories": sorted(c.value for c in e.categories),
            "reserved": e.reserved,
        }
        for e in window.entries
    ]


def dec_ledger_window(raw: list[dict[str, Any]]) -> LedgerWindow:
    return LedgerWindow(
        entries=tuple(
            PriorAuthorization(
                at=dec_dt(e["at"]),
                merchant_id=e["merchant_id"],
                amount=dec_money(e["amount"]),
                fingerprint=e["fingerprint"],
                categories=frozenset(Category(c) for c in e["categories"]),
                reserved=bool(e["reserved"]),
            )
            for e in raw
        )
    )


# -- evaluation -------------------------------------------------------------


def enc_rule_result(result: RuleResult) -> dict[str, Any]:
    return {
        "rule_id": result.rule_id,
        "outcome": result.outcome.value,
        "message": result.message,
        "facts": dict(result.facts),
        "applicable": result.applicable,
    }


def dec_rule_result(raw: dict[str, Any]) -> RuleResult:
    return RuleResult(
        rule_id=raw["rule_id"],
        outcome=Outcome(raw["outcome"]),
        message=raw["message"],
        facts=dict(raw["facts"]),
        applicable=bool(raw["applicable"]),
    )


def enc_evaluation(evaluation: Evaluation) -> dict[str, Any]:
    return {
        "outcome": evaluation.outcome.value,
        "results": [enc_rule_result(r) for r in evaluation.results],
        "engine_version": evaluation.engine_version,
    }


def dec_evaluation(raw: dict[str, Any]) -> Evaluation:
    return Evaluation(
        outcome=Outcome(raw["outcome"]),
        results=tuple(dec_rule_result(r) for r in raw["results"]),
        engine_version=raw["engine_version"],
    )
