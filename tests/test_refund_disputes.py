"""Giving money back, and looking at what happened after it moved.

The two halves of the system that run *after* a decision. Everything else in this
project is about the moment before a purchase; a firewall that never looks at its
own outcomes cannot learn that a decision which passed every rule was wrong.
"""

from __future__ import annotations

import json
import re

import pytest

from mandate.engine.money import Money
from mandate.engine.quote import Category
from mandate.gateway.disputes import Disputes, fetch
from mandate.gateway.service import AuthorizationRequest, Gateway, GatewayError
from mandate.gateway.state import HoldState
from mandate.gateway.store import Store
from mandate.ledger.records import Ledger
from mandate.policies import demo_policy
from mandate.providers.toolkit import ToolkitError

from fake_paypal import FakePayPal
from helpers import LEDGER_KEY, MERCHANT_SECRET, quote


@pytest.fixture
def paypal(now):
    return FakePayPal(now=now)


@pytest.fixture
def gateway(tmp_path, paypal):
    store = Store(tmp_path / "state.db")
    gw = Gateway(
        store=store,
        ledger=Ledger(tmp_path / "decisions.jsonl", LEDGER_KEY),
        policy=demo_policy(),
        merchant_secret=MERCHANT_SECRET,
        paypal=paypal.client(),
        public_url="https://mandate.test",
    )
    yield gw
    store.close()


async def captured(gateway, paypal, now, **kw):
    """Walk a basket all the way to money actually taken."""
    result = await gateway.request_authorization(
        AuthorizationRequest(quote=quote(**kw), reason="restocking", agent_id="ops-1"), now=now
    )
    paypal.approve_buyer(result.hold.paypal_order_id)
    await gateway.place_hold(result.decision_id, now=now)
    return await gateway.capture(result.decision_id, now=now)


# -- refund ----------------------------------------------------------------


async def test_a_refund_returns_what_was_captured(gateway, paypal, now):
    hold = await captured(gateway, paypal, now)
    assert hold.state is HoldState.CAPTURED

    refunded = await gateway.refund(hold.decision_id, now=now)
    assert refunded.state is HoldState.REFUNDED
    assert paypal.only_authorization().status == "REFUNDED"


async def test_a_refund_follows_the_capture_not_the_authorization(gateway, paypal, now):
    """They differ after a partial capture, and refunding the held figure would
    hand back money that was never collected."""
    hold = await captured(gateway, paypal, now)
    assert hold.captured == Money.from_paypal("34.00", "USD")

    with pytest.raises(GatewayError, match=re.escape("only 34.00 USD was captured")):
        await gateway.refund(hold.decision_id, amount=Money.from_paypal("40.00", "USD"), now=now)


async def test_a_partial_refund_is_allowed(gateway, paypal, now):
    hold = await captured(gateway, paypal, now)
    refunded = await gateway.refund(
        hold.decision_id, amount=Money.from_paypal("10.00", "USD"), now=now
    )
    assert refunded.state is HoldState.REFUNDED


async def test_only_a_captured_hold_can_be_refunded(gateway, paypal, now):
    result = await gateway.request_authorization(
        AuthorizationRequest(quote=quote(), reason="restocking", agent_id="ops-1"), now=now
    )
    with pytest.raises(GatewayError, match="not captured"):
        await gateway.refund(result.decision_id, now=now)


async def test_a_refused_decision_cannot_be_refunded(gateway, now):
    result = await gateway.request_authorization(
        AuthorizationRequest(
            quote=quote(items=[("SKU-GC", "Gift card", Category.GIFT_CARD, "100.00", 40)]),
            reason="rewards",
            agent_id="ops-1",
        ),
        now=now,
    )
    with pytest.raises(GatewayError, match="not captured"):
        await gateway.refund(result.decision_id, now=now)


async def test_a_failed_refund_leaves_the_hold_captured(gateway, paypal, now):
    """Not FAILED. The money *is* still captured, and saying otherwise would lose
    that fact and invite someone to retry the capture."""
    hold = await captured(gateway, paypal, now)
    paypal.break_next_call()

    with pytest.raises(GatewayError, match="refund failed"):
        await gateway.refund(hold.decision_id, now=now)

    after = gateway.store.get(hold.decision_id)
    assert after.state is HoldState.CAPTURED
    assert "refund failed" in (after.last_error or "")


# -- disputes --------------------------------------------------------------


class FakeToolkit:
    def __init__(self, payload=None, raises=None):
        self.payload = payload
        self.raises = raises
        self.calls: list[str] = []

    async def call(self, method: str, **params):
        self.calls.append(method)
        if self.raises is not None:
            raise self.raises
        return self.payload


DISPUTE = {
    "dispute_id": "PP-D-1234",
    "status": "WAITING_FOR_SELLER_RESPONSE",
    "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
    "dispute_amount": {"value": "34.00", "currency_code": "USD"},
    "disputed_transactions": [{"seller_transaction_id": "CAP-1"}],
}


async def test_a_dispute_is_joined_to_the_capture_it_names():
    toolkit = FakeToolkit({"items": [DISPUTE]})
    result = await fetch(toolkit)
    assert result.reachable
    assert toolkit.calls == ["list_disputes"]
    found = result.for_capture("CAP-1")
    assert found["dispute_id"] == "PP-D-1234"
    assert found["status"] == "WAITING_FOR_SELLER_RESPONSE"
    assert result.for_capture("CAP-OTHER") is None


async def test_the_toolkit_returning_json_as_a_string_is_handled():
    """Most toolkit methods return a JSON string rather than a parsed object."""
    result = await fetch(FakeToolkit(json.dumps({"items": [DISPUTE]})))
    assert result.reachable
    assert result.for_capture("CAP-1")["reason"].endswith("NOT_RECEIVED")


async def test_no_credentials_is_not_the_same_as_no_disputes():
    result = await fetch(None)
    assert result.reachable is False
    assert result.by_capture == {}
    # The distinction the whole column depends on: an empty table that could not be
    # read must never render the same as an empty table that was.
    assert "not checked" in result.detail


async def test_an_unreachable_dispute_api_reports_rather_than_raises():
    result = await fetch(FakeToolkit(raises=ToolkitError("403 from PayPal")))
    assert result.reachable is False
    assert "403" in result.detail


async def test_an_unexpected_exception_is_still_not_allowed_to_break_the_ledger():
    """A reporting column must never take out the operator view -- the same lesson
    as the record that failed to verify and 500'd the whole overview."""
    result = await fetch(FakeToolkit(raises=RuntimeError("something new")))
    assert result.reachable is False
    assert "RuntimeError" in result.detail


async def test_a_shape_change_at_paypal_does_not_raise():
    for payload in ("not json at all", {"items": "nonsense"}, {"items": [{"no": "transactions"}]}, []):
        result = await fetch(FakeToolkit(payload))
        assert result.by_capture == {}


def test_for_capture_tolerates_a_hold_with_no_capture_id():
    """Every refused decision has one, and they all render through this column."""
    assert Disputes(True).for_capture(None) is None
    assert Disputes(True).for_capture("") is None
