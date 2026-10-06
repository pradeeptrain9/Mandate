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
    DELIVERY_METHODS,
    DISPUTE_METHODS,
    SANDBOX_UNAVAILABLE,
    Toolkit,
    ToolkitError,
    ToolkitUnavailableInSandbox,
)


def ok(detail: str) -> None:
    print(f"  \033[32mok\033[0m    {detail}", flush=True)


def bad(detail: str) -> None:
    print(f"  \033[31mFAIL\033[0m  {detail}", flush=True)


def note(detail: str) -> None:
    print(f"        {detail}", flush=True)


#: Read-only probes. Nothing here creates or moves anything.
PROBES: tuple[tuple[str, dict[str, object], str], ...] = (
    ("list_disputes", {"page_size": 2}, "Customer Disputes"),
    ("list_transactions", {}, "Transaction Search"),
    ("list_invoices", {"page_size": 2}, "Invoicing"),
    ("list_products", {"page_size": 2}, "(no feature flag; catalog is always on)"),
)


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

    print(f"\n\033[1mPayPal Agent Toolkit against sandbox\033[0m  ({len(Toolkit.methods())} methods)\n")
    toolkit = Toolkit(client_id, secret, sandbox=True)

    print("read-only probes:")
    reachable = 0
    for method, params, feature in PROBES:
        try:
            result = await toolkit.call(method, **params)
        except ToolkitError as exc:
            bad(f"{method}: {exc}")
            note(f"if this is a permissions error, tick '{feature}' on the app")
            continue
        reachable += 1
        shape = (
            f"{len(result)} key(s): {sorted(result)[:4]}"
            if isinstance(result, dict)
            else f"{type(result).__name__}: {str(result)[:80]}"
        )
        ok(f"{method} -> {shape}")

    print("\nsandbox limitations:")
    for method in sorted(SANDBOX_UNAVAILABLE):
        try:
            await toolkit.call(method)
            bad(f"{method} unexpectedly worked -- it can be used after all")
        except ToolkitUnavailableInSandbox:
            ok(f"{method} refused in sandbox, as expected; nothing depends on it")

    print("\ntool schemas Mandate will hand to Claude:")
    for spec in Toolkit.specs(DELIVERY_METHODS + DISPUTE_METHODS):
        params = list(spec.input_schema.get("properties", {}))
        ok(f"{spec.method:<28} {params}")

    verdict = "usable" if reachable else "NOT usable -- check the app's Features"
    print(f"\n\033[1mVerdict:\033[0m toolkit is {verdict}.")
    print(
        "The hold lifecycle still speaks raw REST: the toolkit has no authorize,\n"
        "void or reauthorize, which is the whole reason providers/paypal.py exists.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
