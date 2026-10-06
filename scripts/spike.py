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

If PayPal says "this seller doesn't accept payments in your currency", the
sandbox business account cannot receive the order's currency -- sandbox accounts
are created per country and an Indian business account cannot take USD. Either
create US sandbox accounts, or run the spike in the accounts' own currency:

    python scripts/spike.py --currency INR
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402

from mandate.engine.money import MINOR_UNITS, Money  # noqa: E402
from mandate.providers.paypal import (  # noqa: E402
    SANDBOX,
    PayPalClient,
    PayPalError,
    approval_link,
    new_request_id,
)

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


async def step_hold(pp: PayPalClient, *, void_instead: bool, currency: str) -> None:
    # The basket, priced in whatever currency the sandbox accounts can handle.
    # Amounts are scaled so a zero-decimal currency like JPY stays whole.
    paper = Money.from_paypal("850" if currency == "JPY" else "8.50", currency)
    stapler = Money.from_paypal("1200" if currency == "JPY" else "12.00", currency)
    basket = paper * 4 + stapler
    part = Money.from_paypal("2000" if currency == "JPY" else "20.00", currency)

    say("2. create an AUTHORIZE order", f"{basket} ({currency})")
    order = await pp.create_authorization_order(
        currency=currency,
        value=basket.to_paypal(),
        items=[
            {
                "name": "A4 paper, 500 sheets",
                "quantity": "4",
                "unit_amount": {"currency_code": currency, "value": paper.to_paypal()},
                "category": "PHYSICAL_GOODS",
            },
            {
                "name": "Stapler",
                "quantity": "1",
                "unit_amount": {"currency_code": currency, "value": stapler.to_paypal()},
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
        auth.authorization_id, currency=currency, value=part.to_paypal(), final_capture=True
    )
    ok(f"capture {capture.capture_id}, status {capture.status}, took {capture.amount_value} {currency}")
    after = await pp.get_authorization(auth.authorization_id)
    ok(f"authorization status now {after.status}")

    say("   capturing again must fail")
    # An explicit, unique request id. The first version of this check reused the
    # provider's default key, so PayPal replayed the original 201 and the script
    # reported a double capture that had not happened. The real question is
    # whether PayPal refuses a genuinely NEW attempt.
    try:
        await pp.capture_authorization(
            auth.authorization_id,
            currency=currency,
            value=(basket - part).to_paypal(),
            final_capture=True,
            request_id=new_request_id("spike-second"),
        )
        bad("a second capture succeeded -- final_capture does not close the hold")
    except PayPalError as exc:
        ok(f"refused as expected: {exc.issues or exc.status}")

    say("   and retrying the FIRST capture is still idempotent")
    replay = await pp.capture_authorization(
        auth.authorization_id, currency=currency, value=part.to_paypal(), final_capture=True
    )
    if replay.capture_id == capture.capture_id:
        ok(f"same capture id returned ({replay.capture_id}); retry is safe")
    else:
        bad(f"retry produced a NEW capture {replay.capture_id} -- idempotency is broken")


MCP_BASE = "https://mcp.sandbox.paypal.com"
MCP_PATHS = ("/mcp", "/sse", "/")
PROTOCOL = "2025-06-18"
TOOLS_WE_CARE_ABOUT = (
    "create_invoice",
    "list_disputes",
    "list_transactions",
    "create_shipment_tracking",
    "get_merchant_insights",
)


async def _mcp_token(http: httpx.AsyncClient, client_id: str, secret: str) -> str | None:
    """Get a token for the MCP server, which has its own endpoint.

    The first version of this probe reused the REST token from api-m.sandbox,
    which is a different audience. The server advertises its own token endpoint in
    OAuth metadata, so ask that first and fall back to REST only if it refuses.
    """
    attempts = (
        ("mcp /token", MCP_BASE + "/token"),
        ("rest oauth2", SANDBOX + "/v1/oauth2/token"),
    )
    for label, url in attempts:
        try:
            response = await http.post(
                url, auth=(client_id, secret), data={"grant_type": "client_credentials"}
            )
        except httpx.HTTPError as exc:
            print("    " + label + ": " + type(exc).__name__, flush=True)
            continue
        if response.status_code == 200 and "access_token" in response.text:
            ok("token from " + label)
            return response.json()["access_token"]
        print("    " + label + ": " + str(response.status_code) + " " + response.text[:120], flush=True)
    return None


async def step_mcp() -> None:
    say("5. remote MCP server", MCP_BASE)
    client_id = os.environ.get("PAYPAL_CLIENT_ID", "")
    secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
        discovery = await http.get(MCP_BASE + "/.well-known/oauth-authorization-server")
        if discovery.status_code == 200:
            meta = discovery.json()
            ok("OAuth metadata: token endpoint " + str(meta.get("token_endpoint")))
            print("    grant types: " + str(meta.get("grant_types_supported")), flush=True)
        else:
            bad("no OAuth metadata (" + str(discovery.status_code) + ")")

        token = await _mcp_token(http, client_id, secret)
        if not token:
            bad("no usable token for the MCP server")
            _mcp_verdict()
            return

        for path in MCP_PATHS:
            url = MCP_BASE + path
            headers = {
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                # Streamable HTTP wants BOTH, or a compliant server answers 406.
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL,
            }
            # A streamable-HTTP server expects `initialize` first and replies with
            # an Mcp-Session-Id that later calls must echo. Sending tools/list
            # cold -- which the first version of this probe did -- is not a fair
            # test, and a 404 there says nothing about whether the server works.
            init = await http.post(
                url,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL,
                        "capabilities": {},
                        "clientInfo": {"name": "mandate-spike", "version": "0.1.0"},
                    },
                },
            )
            if init.status_code >= 300:
                print(
                    "    " + path + ": initialize -> " + str(init.status_code) + " " + init.text[:140],
                    flush=True,
                )
                continue
            ok(path + ": initialize accepted")
            session = init.headers.get("mcp-session-id")
            if session:
                headers["Mcp-Session-Id"] = session
                ok("session " + session[:16] + "...")
            await http.post(
                url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            listed = await http.post(
                url, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            )
            if listed.status_code >= 300:
                print(
                    "    " + path + ": tools/list -> " + str(listed.status_code) + " " + listed.text[:200],
                    flush=True,
                )
                continue
            body = listed.text
            ok(path + ": tools/list answered, " + str(len(body)) + " bytes")
            for name in TOOLS_WE_CARE_ABOUT:
                mark = "found " if name in body else "absent"
                print("    " + mark + ": " + name, flush=True)
            return

        bad("no MCP path answered an initialize")
        _mcp_verdict()


def _mcp_verdict() -> None:
    print(
        "\n  \033[1mSettled on 2026-10-06, not a blocker.\033[0m The metadata above is the\n"
        "  answer: grant_types_supported is [authorization_code, refresh_token] with no\n"
        "  client_credentials, so the remote server wants an interactive browser consent\n"
        "  flow with dynamic client registration. A headless gateway cannot do that, and\n"
        "  REST credentials come back as invalid_client: Client not found.\n"
        "\n"
        "  Mandate therefore uses the paypal-agent-toolkit package for merchant-side\n"
        "  work -- see src/mandate/providers/toolkit.py and scripts/toolkit_check.py.\n"
        "  It takes client credentials directly and carries the same 42 tools.\n"
        "\n"
        "  Nothing structural changes either way. The remote server was only ever for\n"
        "  invoices, disputes, tracking and reporting; the hold lifecycle speaks raw\n"
        "  REST because the toolkit exposes no authorize, void or reauthorize.",
        flush=True,
    )
