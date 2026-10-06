"""The sweep that settles held money, and the direction it fails in.

The rule under test is one sentence: **uncertainty resolves towards not taking the
money.** Most of these tests exist to pin the unobvious half of it -- that a hold
about to lapse with no delivery confirmation is released rather than captured, even
though capturing is what a merchant would prefer.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from mandate.delivery import Delivered, Delivery
from mandate.delivery.carrier import AlwaysDelivers, MerchantCarrier, NeverDelivers
from mandate.engine.quote import Category
from mandate.gateway.service import AuthorizationRequest, Gateway
from mandate.gateway.state import HoldState
from mandate.gateway.store import Store
from mandate.gateway.sweep import sweep
from mandate.ledger.records import Ledger
from mandate.policies import demo_policy

from fake_paypal import FakePayPal
from helpers import LEDGER_KEY, MERCHANT_SECRET, quote


class Oracle:
    """Answers with whatever a test hands it, and counts the asking."""

    name = "scripted"

    def __init__(self, delivery: Delivery) -> None:
        self.delivery = delivery
        self.asked: list[tuple[str, str]] = []

    async def check(self, *, decision_id: str, merchant_id: str) -> Delivery:
        self.asked.append((decision_id, merchant_id))
        return self.delivery


class Raises:
    name = "broken"

    def __init__(self) -> None:
        self.asked = 0

    async def check(self, *, decision_id: str, merchant_id: str) -> Delivery:
        self.asked += 1
        raise RuntimeError("the carrier's API fell over")


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
        webhook_id="WH-TEST",
    )
    yield gw
    store.close()


async def held(gateway, paypal, now, **kw):
    """One hold at HELD, reached the way a real one is: decide, approve, authorize."""
    result = await gateway.request_authorization(
        AuthorizationRequest(quote=quote(**kw), reason="restock", agent_id="ops-1"), now=now
    )
    paypal.approve_buyer(result.hold.paypal_order_id)
    hold = await gateway.place_hold(result.decision_id, now=now)
    assert hold.state is HoldState.HELD
    return hold


# -- the four branches ------------------------------------------------------


async def test_a_confirmed_delivery_is_captured(gateway, paypal, now):
    hold = await held(gateway, paypal, now)
    report = await sweep(gateway, oracle=AlwaysDelivers(), now=now)

    assert report.summary() == {"captured": 1}
    settled = gateway.store.get(hold.decision_id)
    assert settled.state is HoldState.CAPTURED
    # The amount came from the hold, not from the oracle. An oracle is asked
    # whether something arrived, never how much to pay for it.
    assert settled.captured == hold.amount


async def test_a_never_shipped_hold_is_voided_immediately(gateway, paypal, now):
    """No waiting for expiry: the answer is already known."""
    hold = await held(gateway, paypal, now)
    report = await sweep(gateway, oracle=NeverDelivers(), now=now)

    assert report.summary() == {"voided": 1}
    assert gateway.store.get(hold.decision_id).state is HoldState.VOIDED


async def test_a_hold_still_in_transit_is_left_alone(gateway, paypal, now):
    hold = await held(gateway, paypal, now)
    oracle = Oracle(Delivery(Delivered.NOT_YET, "in transit"))
    report = await sweep(gateway, oracle=oracle, now=now)

    assert report.summary() == {"waiting": 1}
    assert gateway.store.get(hold.decision_id).state is HoldState.HELD
    assert oracle.asked == [(hold.decision_id, hold.merchant_id)]


async def test_a_lapsing_undelivered_hold_is_released_not_captured(gateway, paypal, now):
    """The unobvious half of the rule, and the reason the module exists.

    Capturing here is what a merchant would prefer and it is still wrong: a wrongly
    voided hold costs a reauthorization, a wrongly captured one costs money that
    only comes back on someone else's goodwill. An unsupervised agent must fail in
    the recoverable direction.
    """
    hold = await held(gateway, paypal, now)
    # 28 days on: inside PayPal's 29-day window, past the grace period.
    later = now + timedelta(days=28, hours=12)

    report = await sweep(gateway, oracle=Oracle(Delivery(Delivered.NOT_YET, "in transit")), now=later)

    assert report.summary() == {"voided_on_expiry": 1}
    settled = gateway.store.get(hold.decision_id)
    assert settled.state is HoldState.VOIDED
    assert settled.captured is None


async def test_an_unknown_delivery_is_never_captured(gateway, paypal, now):
    """UNKNOWN is about our ignorance, not about the goods. It can never buy
    anything -- only wait, or release."""
    hold = await held(gateway, paypal, now)

    waiting = await sweep(gateway, oracle=Oracle(Delivery(Delivered.UNKNOWN, "no tracking")), now=now)
    assert waiting.summary() == {"waiting": 1}
    assert gateway.store.get(hold.decision_id).state is HoldState.HELD

    later = now + timedelta(days=28, hours=12)
    lapsing = await sweep(
        gateway, oracle=Oracle(Delivery(Delivered.UNKNOWN, "no tracking")), now=later
    )
    assert lapsing.summary() == {"voided_on_expiry": 1}
    assert gateway.store.get(hold.decision_id).state is HoldState.VOIDED


# -- robustness -------------------------------------------------------------


async def test_an_oracle_that_raises_becomes_unknown_not_a_crash(gateway, paypal, now):
    """One merchant's broken endpoint must not stop every other hold settling."""
    hold = await held(gateway, paypal, now)
    oracle = Raises()
    report = await sweep(gateway, oracle=oracle, now=now)

    assert oracle.asked == 1
    assert report.actions[0].delivery is Delivered.UNKNOWN
    assert report.summary() == {"waiting": 1}
    assert gateway.store.get(hold.decision_id).state is HoldState.HELD


async def test_a_failed_capture_is_reported_and_leaves_the_hold_alone(gateway, paypal, now):
    hold = await held(gateway, paypal, now)
    paypal.break_next_call()
    report = await sweep(gateway, oracle=AlwaysDelivers(), now=now)

    assert list(report.summary()) == ["capture_failed"]
    assert gateway.store.get(hold.decision_id).state is not HoldState.CAPTURED


async def test_only_held_authorizations_are_swept(gateway, paypal, now):
    """A refused decision has no money anywhere near it, and a captured hold is
    finished. Sweeping either would be a second chance to move money."""
    refused = await gateway.request_authorization(
        AuthorizationRequest(
            quote=quote(items=[("SKU-GC", "gift card", Category.GIFT_CARD, "100.00", 1)]),
            reason="x",
            agent_id="ops-1",
        ),
        now=now,
    )
    assert refused.hold.state is HoldState.REFUSED
    oracle = Oracle(Delivery(Delivered.YES, "delivered"))

    report = await sweep(gateway, oracle=oracle, now=now)
    assert report.checked == 0
    assert oracle.asked == []


async def test_a_hold_with_no_recorded_expiry_is_not_treated_as_urgent(gateway, paypal, now):
    """Guessing an expiry would make the sweep void holds it has no evidence
    about."""
    hold = await held(gateway, paypal, now)
    # Reaching past the store's own API on purpose: there is no supported way to
    # produce this row, and the state it represents -- a hold PayPal never gave an
    # expiry for -- is exactly what the sweep has to not panic about.
    with gateway.store._tx() as db:
        db.execute(
            "UPDATE holds SET authorization_expires_at = NULL WHERE decision_id = ?",
            (hold.decision_id,),
        )

    far_future = now + timedelta(days=400)
    report = await sweep(gateway, oracle=Oracle(Delivery(Delivered.UNKNOWN, "?")), now=far_future)
    assert report.summary() == {"waiting": 1}
    assert gateway.store.get(hold.decision_id).state is HoldState.HELD


# -- the carrier adapter ----------------------------------------------------


@pytest.fixture
def carrier_transport(monkeypatch):
    """Point every AsyncClient the carrier opens at a scripted transport.

    The carrier opens its own client per call, which is the right shape for the
    production code -- a long-lived client on a module-level oracle is a connection
    pool nobody closes -- and it means a test has to intercept at construction.
    """

    def install(handler):
        transport = httpx.MockTransport(handler)
        real_init = httpx.AsyncClient.__init__

        def init(self, *args, **kwargs):
            kwargs["transport"] = transport
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
        return MerchantCarrier("http://merchant.test")

    return install


async def test_the_carrier_reads_the_merchant_stubs_answers(carrier_transport):
    """Delivered, never_shipped and in-transit each map to a distinct value."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "ghost" in request.url.path:
            return httpx.Response(200, json={"status": "never_shipped", "delivered": False})
        if "slow" in request.url.path:
            return httpx.Response(200, json={"status": "in_transit", "delivered": False})
        return httpx.Response(200, json={"status": "delivered", "delivered": True})

    carrier = carrier_transport(handler)
    assert (await carrier.check(decision_id="d1", merchant_id="m_acme")).status is Delivered.YES
    assert (await carrier.check(decision_id="d1", merchant_id="m_ghost")).status is Delivered.NEVER
    assert (await carrier.check(decision_id="d1", merchant_id="m_slow")).status is Delivered.NOT_YET


async def test_an_unreachable_carrier_is_unknown_not_never(carrier_transport):
    """Treating a network fault as proof of non-delivery would void holds for
    honest merchants during an outage."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = await carrier_transport(handler).check(decision_id="d1", merchant_id="m_acme")
    assert result.status is Delivered.UNKNOWN
    assert "unreachable" in result.detail


async def test_a_carrier_404_is_unknown_not_never(carrier_transport):
    """No record of a shipment is not proof there will not be one."""
    carrier = carrier_transport(lambda r: httpx.Response(404, json={}))
    result = await carrier.check(decision_id="d1", merchant_id="m_acme")
    assert result.status is Delivered.UNKNOWN


async def test_a_carrier_that_answers_with_prose_is_unknown(carrier_transport):
    """A carrier is a source of one bit of fact, not a source of text. Anything
    unparseable is ignorance, not a delivery."""
    carrier = carrier_transport(lambda r: httpx.Response(200, content=b"shipped, probably"))
    result = await carrier.check(decision_id="d1", merchant_id="m_acme")
    assert result.status is Delivered.UNKNOWN
