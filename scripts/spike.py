#!/usr/bin/env python
"""Week 0 de-risking spike. Run this before trusting any of the architecture.

It proves, against the real PayPal sandbox, the four things the whole design
rests on. If any step fails the design changes, which is why this runs before
the gateway exists.

  1. Client credentials produce a token.
  2. An `intent=AUTHORIZE` order can be created, approved, and authorized --
     holding funds without taking them. The script prints PayPal's own
     expiry figure rather than the 29 days the docs promise, because the
     figure that matters is the one the API returns.
  3. A partial capture takes some of the hold and `final_capture` releases
     the rest; a separate run voids instead, releasing all of it.
  4. The remote MCP server at mcp.sandbox.paypal.com answers with a tool list.

Step 2 needs a human: PayPal will not hold a buyer's funds without the buyer
saying so. The script prints the approval URL, waits, and polls until the order
reaches APPROVED.

    PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... python scripts/spike.py
    python scripts/spike.py --void       # release instead of capturing
    python scripts/spike.py --mcp-only   # just step 4
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402

from mandate.providers.paypal import (  # noqa: E402
    SANDBOX,
    PayPalClient,
    PayPalError,
    approval_link,
)

MCP_SANDBOX = "https://mcp.sandbox.paypal.com/mcp"


def say(step: str, detail: str = "") -> None:
    print(f"\n\033[1m{step}\033[0m{(' ' + detail) if detail else ''}", flush=True)


def ok(detail: str) -> None:
    print(f"  \033[32mok\033[0m  {detail}", flush=True)


def bad(detail: str) -> None:
    print(f"  \033[31mFAIL\033[0m  {detail}", flush=True)


async def step_token(pp: PayPalClient) -> None:
    say("1. client credentials")
    token = await pp.token()
    ok(f"token acquired, {len(token)} chars, prefix {token[:6]}...")


async def step_hold(pp: PayPalClient, *, void_instead: bool) -> None:
    say("2. create an AUTHORIZE order")
    order = await pp.create_authorization_order(
        currency="USD",
        value="46.00",
        items=[
            {
                "name": "A4 paper, 500 sheets",
                "quantity": "4",
                "unit_amount": {"currency_code": "USD", "value": "8.50"},
                "category": "PHYSICAL_GOODS",
            },
            {
                "name": "Stapler",
                "quantity": "1",
                "unit_amount": {"currency_code": "USD", "value": "12.00"},
                "category": "PHYSICAL_GOODS",
            },
        ],
        decision_id="spike-0001",
        return_url="https://example.test/return",
        cancel_url="https://example.test/cancel",
    )
    order_id = order["id"]
    ok(f"order {order_id}, status {order['status']}")

    link = approval_link(order)
    if not link:
        bad(f"no approval link in the response; links were {order.get('links')}")
        return
    print(
        f"\n  Open this as a sandbox PERSONAL account and approve it:\n\n    {link}\n\n"
        "  (Sandbox buyer logins are under Testing Tools -> Sandbox Accounts.)",
        flush=True,
    )

    say("   waiting for approval", "polling every 5s, Ctrl-C to stop")
    for attempt in range(120):
        current = await pp.get_order(order_id)
        status = current.get("status")
        if status == "APPROVED":
            ok(f"approved after ~{attempt * 5}s")
            break
        print(f"    status {status}", flush=True)
        await asyncio.sleep(5)
    else:
        bad("never reached APPROVED")
        return

    say("3. place the hold")
    auth = await pp.authorize_order(order_id)
    ok(f"authorization {auth.authorization_id}, status {auth.status}")
    ok(f"amount held {auth.amount_value} {auth.currency}")
    if auth.created_at and auth.expires_at:
        held_for = auth.expires_at - auth.created_at
        ok(f"created {auth.created_at.isoformat()}")
        ok(f"expires {auth.expires_at.isoformat()}  ({held_for.days} days per PayPal)")
        if held_for.days != 29:
            bad(f"expected a 29-day window, PayPal reported {held_for.days} -- update the plan")
    else:
        bad("no create_time/expiration_time in the authorization; expiry logic needs a rethink")

    if void_instead:
        say("4. void the hold (scene 3: nothing ever shipped)")
        await pp.void_authorization(auth.authorization_id)
        after = await pp.get_authorization(auth.authorization_id)
        ok(f"status now {after.status}")
        if after.status != "VOIDED":
            bad(f"expected VOIDED, got {after.status}")
        return

    say("4. partial capture, remainder released")
    capture = await pp.capture_authorization(
        auth.authorization_id, currency="USD", value="20.00", final_capture=True
    )
    ok(f"capture {capture.capture_id}, status {capture.status}, took {capture.amount_value} USD")
    after = await pp.get_authorization(auth.authorization_id)
    ok(f"authorization status now {after.status}")

    say("   capturing again must fail")
    try:
        await pp.capture_authorization(
            auth.authorization_id, currency="USD", value="26.00", final_capture=True
        )
        bad("a second capture succeeded -- final_capture does not close the hold")
    except PayPalError as exc:
        ok(f"refused as expected: {exc.issues or exc.status}")


async def step_mcp() -> None:
    say("5. remote MCP server", MCP_SANDBOX)
    client_id = os.environ.get("PAYPAL_CLIENT_ID", "")
    secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")
    async with httpx.AsyncClient(timeout=30.0) as http:
        discovery = await http.get(
            "https://mcp.sandbox.paypal.com/.well-known/oauth-authorization-server"
        )
        if discovery.status_code == 200:
            meta = discovery.json()
            ok(f"OAuth metadata: token endpoint {meta.get('token_endpoint')}")
        else:
            bad(f"no OAuth metadata ({discovery.status_code}); check the quickstart for the flow")

        # The MCP server accepts the same client credentials as the REST API.
        token_response = await http.post(
            f"{SANDBOX}/v1/oauth2/token",
            auth=(client_id, secret),
            data={"grant_type": "client_credentials"},
        )
        token = token_response.json().get("access_token", "")
        probe = await http.post(
            MCP_SANDBOX,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        if probe.status_code >= 300:
            bad(f"tools/list returned {probe.status_code}: {probe.text[:300]}")
            print(
                "  If this is a 401, the remote server wants the full OAuth 2.1 dance\n"
                "  rather than a client-credentials bearer. Fall back to the local MCP\n"
                "  server or the Python agent toolkit; neither changes the architecture.",
                flush=True,
            )
            return
        body = probe.text
        ok(f"tools/list answered, {len(body)} bytes")
        for name in ("create_order", "list_disputes", "create_invoice", "list_transactions"):
            print(f"    {'found' if name in body else 'absent'}: {name}", flush=True)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--void", action="store_true", help="release the hold instead of capturing")
    parser.add_argument("--mcp-only", action="store_true", help="run only the MCP probe")
    parser.add_argument("--skip-mcp", action="store_true")
    args = parser.parse_args()

    client_id = os.environ.get("PAYPAL_CLIENT_ID", "")
    secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")
    if not client_id or not secret:
        print(
            "Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET from a sandbox app at\n"
            "https://developer.paypal.com/dashboard/applications/sandbox",
            file=sys.stderr,
        )
        return 2

    if args.mcp_only:
        await step_mcp()
        return 0

    async with PayPalClient(client_id, secret, base_url=SANDBOX) as pp:
        await step_token(pp)
        await step_hold(pp, void_instead=args.void)
    if not args.skip_mcp:
        await step_mcp()

    print("\n\033[1mSpike complete.\033[0m Anything marked FAIL above changes the plan.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
