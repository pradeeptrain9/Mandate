#!/usr/bin/env python
"""Which sandbox business account do these credentials belong to, and in what
currency can it receive money?

Written because "this seller doesn't accept payments in your currency" is a
PayPal-side mismatch that the order payload cannot reveal. A sandbox business
account is created per country, and an Indian one cannot receive USD however
well-formed the order is.

The check is indirect: create a tiny AUTHORIZE order in each candidate currency
and see which ones PayPal accepts. Nothing is approved and no funds move -- an
unapproved order expires on its own.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mandate.engine.money import MINOR_UNITS
from mandate.providers.paypal import SANDBOX, PayPalClient, PayPalError


async def main() -> int:
    client_id = os.environ.get("PAYPAL_CLIENT_ID", "")
    secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")
    if not client_id or not secret:
        print("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET first.", file=sys.stderr)
        return 2

    print(f"client id {client_id[:12]}...{client_id[-4:]}\n")
    async with PayPalClient(client_id, secret, base_url=SANDBOX) as pp:
        await pp.token()
        print("token ok\n\nprobing which currencies this account can receive:")
        for currency in sorted(MINOR_UNITS):
            value = "100" if MINOR_UNITS[currency] == 1 else "1.00"
            try:
                order = await pp.create_authorization_order(
                    currency=currency,
                    value=value,
                    items=[
                        {
                            "name": "probe",
                            "quantity": "1",
                            "unit_amount": {"currency_code": currency, "value": value},
                            "category": "DIGITAL_GOODS",
                        }
                    ],
                    decision_id=f"probe-{currency}",
                    return_url="https://example.test/return",
                    cancel_url="https://example.test/cancel",
                    request_id=f"probe-{currency}",
                )
            except PayPalError as exc:
                detail = ", ".join(exc.issues) or str(exc.status)
                print(f"  \033[31mno \033[0m {currency}  {detail}")
            else:
                print(f"  \033[32myes\033[0m {currency}  order {order.get('id')} (left unapproved)")

    print(
        "\nUse one of the 'yes' currencies with scripts/spike.py --currency.\n"
        "If only INR works, these are Indian sandbox accounts. The demo policy in\n"
        "src/mandate/policies.py is written in USD -- either create US sandbox\n"
        "accounts or we re-denominate the policy."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
