"""Builders shared by the test modules. Fixtures live in conftest.py."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from mandate.engine.money import Money
from mandate.engine.quote import Category, LineItem, MerchantQuote

MERCHANT_SECRET = b"merchant-shared-secret"
LEDGER_KEY = b"gateway-ledger-key"


def usd(value: str) -> Money:
    return Money.from_paypal(value, "USD")


def quote(
    *,
    merchant_id: str = "m_acme",
    items: list[tuple[str, str, Category, str, int]] | None = None,
    at: datetime | None = None,
    currency: str = "USD",
    merchant_name: str = "Acme Supplies Ltd",
    total_override: Money | None = None,
    sign_with: bytes | None = MERCHANT_SECRET,
) -> MerchantQuote:
    """Build a quote. `items` are (sku, description, category, unit_price, qty)."""
    items = items or [("SKU-PAPER", "A4 paper, 500 sheets", Category.OFFICE_SUPPLIES, "8.50", 4)]
    line_items = tuple(
        LineItem(sku, description, category, Money.from_paypal(price, currency), qty)
        for sku, description, category, price, qty in items
    )
    computed = Money(sum(i.line_total.minor for i in line_items), currency)
    built = MerchantQuote(
        quote_id=f"q_{uuid.uuid4().hex[:8]}",
        merchant_id=merchant_id,
        merchant_name=merchant_name,
        currency=currency,
        line_items=line_items,
        declared_total=total_override or computed,
        issued_at=at or datetime(2026, 10, 6, 11, 58, 0, tzinfo=UTC),
        nonce=uuid.uuid4().hex[:12],
    )
    return built.sign(sign_with) if sign_with else built


def sign_in(
    client,
    *,
    username: str = "ops@example.com",
    password: str = "test-password-1",
    approver: bool = True,
):
    """Create an account and log the TestClient into it.

    Every operator route requires an approver session now, so a fixture that does
    not do this is testing the gate rather than the route behind it.
    """
    from mandate.gateway.accounts import Accounts, Role

    accounts = Accounts(client.app.state.gateway.store)
    if accounts.get(username) is None:
        accounts.create(
            username, password, role=Role.APPROVER if approver else Role.REQUESTER
        )
    response = client.post("/app/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()
