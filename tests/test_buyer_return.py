"""Where PayPal sends the buyer back.

This is the last step of a purchase and the easiest to leave missing, because
nothing in the test suite or the gateway's own logs notices: the order is
created, the approval URL is correct, PayPal takes the payment approval, and the
only thing that breaks is a page on a host PayPal redirects to. It is reported
as "I clicked Pay Now and got Not Found", which reads as a payment failure.

The second property here matters more than the page. Two independent messages
say the buyer approved -- the redirect and CHECKOUT.ORDER.APPROVED -- and both
reserve the funds. If they can interleave, one of them authorizes an order the
other has already authorized, PayPal refuses the duplicate, and the hold that is
genuinely held is recorded as FAILED.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from mandate.engine.quote import Category
from mandate.gateway.api import create_app
from mandate.gateway.service import AuthorizationRequest, Gateway
from mandate.gateway.state import HoldState
from mandate.gateway.store import Store
from mandate.ledger.records import Ledger
from mandate.policies import demo_policy
from mandate.providers.paypal import PayPalClient

from fake_paypal import FakePayPal
from helpers import LEDGER_KEY, MERCHANT_SECRET, quote


@pytest.fixture
def paypal(now):
    return FakePayPal(now=now)


@pytest.fixture
def gw(tmp_path, paypal):
    store = Store(tmp_path / "state.db")
    g = Gateway(
        store=store,
        ledger=Ledger(tmp_path / "decisions.jsonl", LEDGER_KEY),
        policy=demo_policy(),
        merchant_secret=MERCHANT_SECRET,
        paypal=paypal.client(),
        public_url="https://mandate.test",
    )
    yield g
    store.close()


@pytest.fixture
def client(gw):
    with TestClient(create_app(gw)) as c:
        yield c


async def _allowed(gw, now):
    """One purchase the policy allows, taken as far as AWAITING_BUYER."""
    return await gw.request_authorization(
        AuthorizationRequest(
            quote=quote(
                items=[("SKU-PAPER-A4", "A4 paper, 500 sheets", Category.OFFICE_SUPPLIES, "8.50", 2)],
                at=now,
            ),
            reason="restock",
        ),
        now=now,
    )


# -- the route exists at the URL PayPal is given ----------------------------


def test_the_return_url_the_gateway_hands_paypal_is_a_route_it_serves(gw, paypal, now, client):
    """The one assertion that would have caught the 404.

    Asserted against the URL `_create_order` actually sent, not a literal, so a
    change to either side has to change both.
    """
    asyncio.run(_allowed(gw, now))
    order = paypal.only_order()
    sent = paypal.return_urls[order.order_id]
    assert sent.startswith("https://mandate.test/")
    path = sent.removeprefix("https://mandate.test")

    paypal.approve_buyer(order.order_id)
    assert client.get(path, params={"token": order.order_id}).status_code == 200


def test_returning_from_paypal_reserves_the_funds(gw, paypal, now, client):
    result = asyncio.run(_allowed(gw, now))
    order = paypal.only_order()
    paypal.approve_buyer(order.order_id)

    page = client.get(f"/buyer/return/{result.decision_id}", params={"token": order.order_id})

    assert page.status_code == 200
    assert "Money held" in page.text
    assert gw.store.get(result.decision_id).state is HoldState.HELD


def test_the_page_names_the_amount_and_the_merchant(gw, paypal, now, client):
    result = asyncio.run(_allowed(gw, now))
    order = paypal.only_order()
    paypal.approve_buyer(order.order_id)

    page = client.get(f"/buyer/return/{result.decision_id}", params={"token": order.order_id}).text

    assert "17.00" in page
    assert "Acme" in page
    assert result.decision_id in page


def test_cancelling_at_paypal_is_terminal_and_takes_nothing(gw, paypal, now, client):
    result = asyncio.run(_allowed(gw, now))
    order = paypal.only_order()

    page = client.get(f"/buyer/cancel/{result.decision_id}", params={"token": order.order_id})

    assert page.status_code == 200
    assert "Cancelled" in page.text
    assert gw.store.get(result.decision_id).state is HoldState.BUYER_CANCELLED
    assert not paypal.authorizations


# -- what the route refuses -------------------------------------------------


def test_a_token_for_a_different_order_is_refused(gw, paypal, now, client):
    result = asyncio.run(_allowed(gw, now))
    paypal.approve_buyer(paypal.only_order().order_id)

    page = client.get(f"/buyer/return/{result.decision_id}", params={"token": "ORDER-SOMEONE-ELSE"})

    assert page.status_code == 400
    assert gw.store.get(result.decision_id).state is HoldState.AWAITING_BUYER


def test_an_unknown_decision_is_not_found(client):
    assert client.get("/buyer/return/dec_does_not_exist").status_code == 404


def test_returning_before_paypal_approves_leaves_the_hold_alone(gw, paypal, now, client):
    """PayPal has not marked the order approved, so `authorize` fails there.

    The page must still render -- it is the buyer's only feedback -- and it must
    say the truth, which at this point is that something went wrong rather than
    that the money is held.
    """
    result = asyncio.run(_allowed(gw, now))
    order = paypal.only_order()

    page = client.get(f"/buyer/return/{result.decision_id}", params={"token": order.order_id})

    assert page.status_code == 200
    assert "Money held" not in page.text
    assert gw.store.get(result.decision_id).state is HoldState.FAILED


# -- the race ---------------------------------------------------------------


def test_the_redirect_and_the_webhook_cannot_both_authorize(tmp_path, paypal, now):
    """Both arrive saying the buyer approved. Only one may call PayPal.

    Without the per-decision lock in `place_hold`, both read AWAITING_BUYER,
    both authorize, PayPal refuses the second, and the hold that really is held
    ends up recorded as FAILED.

    The PayPal client here yields inside `/authorize`, which the suite's usual
    `MockTransport` handler does not: its handler is synchronous, so awaiting it
    never reaches the event loop and the first placement runs to completion
    before the second starts. That makes the race invisible to a fake and is
    exactly the condition a real network call does not have -- a test written
    against the plain fake passes whether the lock is there or not.
    """

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authorize"):
            await asyncio.sleep(0)
        return paypal.handle(request)

    store = Store(tmp_path / "race.db")
    gw = Gateway(
        store=store,
        ledger=Ledger(tmp_path / "race.jsonl", LEDGER_KEY),
        policy=demo_policy(),
        merchant_secret=MERCHANT_SECRET,
        paypal=PayPalClient("cid", "secret", transport=httpx.MockTransport(handle)),
        public_url="https://mandate.test",
    )

    async def both() -> None:
        result = await _allowed(gw, now)
        paypal.approve_buyer(paypal.only_order().order_id)
        outcomes = await asyncio.gather(
            gw.place_hold(result.decision_id),
            gw.place_hold(result.decision_id),
            return_exceptions=True,
        )
        held = [o for o in outcomes if not isinstance(o, Exception)]
        assert len(held) == 1, f"both placements succeeded: {outcomes}"
        assert len(paypal.authorizations) == 1
        assert gw.store.get(result.decision_id).state is HoldState.HELD

    try:
        asyncio.run(both())
    finally:
        store.close()


def test_a_second_return_after_the_webhook_shows_the_hold_not_an_error(gw, paypal, now, client):
    """The buyer reloads the page, or the webhook got there first."""
    result = asyncio.run(_allowed(gw, now))
    order = paypal.only_order()
    paypal.approve_buyer(order.order_id)

    first = client.get(f"/buyer/return/{result.decision_id}", params={"token": order.order_id})
    second = client.get(f"/buyer/return/{result.decision_id}", params={"token": order.order_id})

    assert (first.status_code, second.status_code) == (200, 200)
    assert "Money held" in second.text
    assert len(paypal.authorizations) == 1
