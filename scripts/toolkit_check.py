#!/usr/bin/env python
"""Verify the PayPal Agent Toolkit works with plain client credentials.

This replaced a dead end. PayPal's remote MCP server at mcp.sandbox.paypal.com
advertises grant_types_supported: ["authorization_code", "refresh_token"] and no
client_credentials, so it wants an interactive browser consent flow with dynamic
client registration -- which a headless gateway cannot perform. The toolkit takes
client credentials directly.

This script confirms that against the real sandbox, and reports which of the
merchant-side calls Mandate wants are actually enabled on the app. A method that
comes back with a permissions error is a Features checkbox that needs ticking in
the developer dashboard, not a code problem -- the script says which.

    PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... python scripts/toolkit_check.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mandate.providers.toolkit import (  # noqa: E402
    BROKEN_IN_TOOLKIT,
    DELIVERY_METHODS,
    DISPUTE_METHODS,
    SANDBOX_UNAVAILABLE,
    Toolkit,
    ToolkitError,
    ToolkitMethodBroken,
    ToolkitUnavailableInSandbox,
    quiet_toolkit_logging,
)


def ok(detail: str) -> None:
    print(f"  \033[32mok\033[0m    {detail}", flush=True)


def bad(detail: str) -> None:
    print(f"  \033[31mFAIL\033[0m  {detail}", flush=True)


def note(detail: str) -> None:
    print(f"        {detail}", flush=True)


#: Read-only probes, narrowed to what Mandate actually uses. Nothing here creates
#: or moves anything. Invoicing and the product catalog are deliberately absent:
#: they return 403 on a merchant app without those features, and Mandate uses
#: neither, so a failure there would be noise rather than a finding.
PROBES: tuple[tuple[str, dict[str, object], str], ...] = (
    ("list_disputes", {"page_size": 2}, "Customer Disputes"),
)


def classify(message: str) -> tuple[str, str]:
    """A 400 and a 403 mean opposite things, and conflating them sends you hunting
    for a dashboard setting that was never the problem.

    403 / NOT_AUTHORIZED  -> a Features checkbox is unticked on the app.
    400 / INVALID_REQUEST -> the request itself is malformed; our side, or the
                             toolkit's. No setting will fix it.
    """
    if "403" in message or "NOT_AUTHORIZED" in message or "PERMISSION_DENIED" in message:
        return ("permissions", "tick the matching feature on the app in the developer dashboard")
    if "400" in message or "INVALID_REQUEST" in message:
        return (
            "malformed request",
            "not a permissions problem -- the call is built wrong and no setting fixes it",
        )
    return ("unknown", "read the error above")


async def main() -> int:
    client_id = os.environ.get("PAYPAL_CLIENT_ID", "")
    secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")
    if not client_id or not secret:
        print(
            "Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET. Source .env, not\n"
            ".env.example -- the example file has blank values.",
            file=sys.stderr,
        )
        return 2

    # The toolkit logs full response headers, set-cookie included, at ERROR level
    # on the root logger for every non-2xx. Expected 403s are not emergencies and
    # session cookies do not belong in a terminal scrollback.
    quiet_toolkit_logging()

    print(f"\n\033[1mPayPal Agent Toolkit against sandbox\033[0m  ({len(Toolkit.methods())} methods)\n")
    toolkit = Toolkit(client_id, secret, sandbox=True)

    print("what Mandate uses:")
    reachable = 0
    for method, params, feature in PROBES:
        try:
            result = await toolkit.call(method, **params)
        except ToolkitError as exc:
            kind, advice = classify(str(exc))
            bad(f"{method}: {kind}")
            note(advice + (f" ('{feature}')" if kind == "permissions" else ""))
            continue
        reachable += 1
        shape = (
            f"{len(result)} key(s): {sorted(result)[:4]}"
            if isinstance(result, dict)
            else f"{type(result).__name__}: {str(result)[:80]}"
        )
        ok(f"{method} -> {shape}")

    print("\nknown-bad paths, refused before they reach PayPal:")
    for method, replacement in sorted(BROKEN_IN_TOOLKIT.items()):
        try:
            await toolkit.call(method)
            bad(f"{method} unexpectedly worked -- the workaround may be removable")
        except ToolkitMethodBroken:
            ok(f"{method} refused; use {replacement}")
    for method in sorted(SANDBOX_UNAVAILABLE):
        try:
            await toolkit.call(method)
            bad(f"{method} unexpectedly worked in sandbox")
        except ToolkitUnavailableInSandbox:
            ok(f"{method} refused in sandbox; nothing depends on it")

    print("\ntool schemas Mandate will hand to Claude:")
    for spec in Toolkit.specs(DELIVERY_METHODS + DISPUTE_METHODS):
        params = list(spec.input_schema.get("properties", {}))
        ok(f"{spec.method:<28} {params}")

    print(
        f"\n\033[1mVerdict:\033[0m {reachable}/{len(PROBES)} probe(s) reachable.\n"
        "Disputes and shipment tracking go through the toolkit. Transaction search\n"
        "goes through PayPalClient.search_transactions because the toolkit builds\n"
        "start_date without a UTC offset. The hold lifecycle speaks raw REST\n"
        "throughout: the toolkit has no authorize, void or reauthorize.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
