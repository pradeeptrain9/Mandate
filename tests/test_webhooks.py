"""Inbound from PayPal: verified before believed, and applied at most once.

This is the one endpoint in the project a stranger can post JSON at, so the tests
that matter here are the negative ones. Several of them would pass against an
endpoint that performed no verification at all, which is why each asserts on what
was *not* done as well as on what was.
"""

from __future__ import annotations

import json

import pytest

from mandate.gateway.service import AuthorizationRequest, Gateway, WebhookRejected
from mandate.gateway.state import HoldState
from mandate.gateway.store import Store
from mandate.ledger.records import Ledger
from mandate.policies import demo_policy

from fake_paypal import FakePayPal
from helpers import LEDGER_KEY, MERCHANT_SECRET, quote

SIGNED_HEADERS = {
    "paypal-auth-algo": "SHA256withRSA",
    "paypal-cert-url": "https://api.sandbox.paypal.com/cert.pem",
    "paypal-transmission-id": "tx-1",
    "paypal-transmission-sig": "sig",
    "paypal-transmission-time": "2026-10-06T12:00:00Z",
}


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
        webhook_id="WH-TEST",
    )
    yield gw
    store.close()


async def allowed_hold(gateway, now):
    """One hold sitting at AWAITING_BUYER, the way a real allowed decision leaves it."""
    result = await gateway.request_authorization(
        AuthorizationRequest(quote=quote(), reason="restock", agent_id="ops-1"), now=now
    )
    assert result.hold.state is HoldState.AWAITING_BUYER
    return result


def event(event_type: str, *, event_id: str = "WH-EVT-1", **resource) -> dict:
    return {"id": event_id, "event_type": event_type, "resource": resource}


async def deliver(gateway, body: dict, *, now=None, headers=None):
    raw = json.dumps(body).encode("utf-8")
    return await gateway.ingest_webhook(
        headers=headers if headers is not None else dict(SIGNED_HEADERS),
        raw_body=raw,
        event=body,
        now=now,
    )


# -- verification -----------------------------------------------------------


async def test_a_delivery_that_fails_verification_changes_nothing(gateway, paypal, now):
    """The whole point. A forged capture must not capture."""
    result = await allowed_hold(gateway, now)
    paypal.webhook_verification = "FAILURE"

    with pytest.raises(WebhookRejected, match="verification failed"):
        await deliver(
            gateway,
            event("PAYMENT.CAPTURE.COMPLETED", custom_id=result.decision_id),
            now=now,
        )

    assert gateway.store.get(result.decision_id).state is HoldState.AWAITING_BUYER


async def test_a_rejected_delivery_is_not_remembered_as_seen(gateway, paypal, now):
    """Dedupe must happen after verification, not before.

    If a forgery could register its event id, anyone able to guess an id could make
    the genuine delivery that follows look like a replay and be ignored -- a denial
    of service against the gateway's own view of the money.
    """
    result = await allowed_hold(gateway, now)
    body = event("PAYMENT.AUTHORIZATION.CREATED", event_id="WH-SAME", custom_id=result.decision_id)

    paypal.webhook_verification = "FAILURE"
    with pytest.raises(WebhookRejected):
        await deliver(gateway, body, now=now)

    paypal.webhook_verification = "SUCCESS"
    outcome = await deliver(gateway, body, now=now)
    assert outcome.action == "applied"
    assert gateway.store.get(result.decision_id).state is HoldState.HELD


async def test_a_gateway_with_no_webhook_id_refuses_everything(tmp_path, paypal, now):
    """Unconfigured means reject, not "accept for local development"."""
    store = Store(tmp_path / "state.db")
    gw = Gateway(
        store=store,
        ledger=Ledger(tmp_path / "decisions.jsonl", LEDGER_KEY),
        policy=demo_policy(),
        merchant_secret=MERCHANT_SECRET,
        paypal=paypal.client(),
        webhook_id="",
    )
    try:
        with pytest.raises(Exception, match="cannot verify webhooks"):
            await deliver(gw, event("PAYMENT.CAPTURE.COMPLETED"), now=now)
        # And it did not even ask PayPal, because there is nothing to ask with.
        assert paypal.verified_bodies == []
    finally:
        store.close()


async def test_verification_is_asked_about_the_exact_bytes(gateway, paypal, now):
    """Not a reserialised copy of them.

    The body is built with the original bytes spliced in, so a payload whose key
    order or spacing would not survive json.dumps still verifies.
    """
    result = await allowed_hold(gateway, now)
    raw = b'{"id":"WH-RAW","event_type":"PAYMENT.AUTHORIZATION.CREATED","resource":{"custom_id":"%s","id":"AUTH-X"}}' % result.decision_id.encode()
    await gateway.ingest_webhook(
        headers=dict(SIGNED_HEADERS), raw_body=raw, event=json.loads(raw), now=now
    )
    sent = paypal.verified_bodies[-1]
    assert raw in sent


async def test_a_delivery_without_signature_headers_is_rejected(gateway, now):
    """Not a verification failure to be retried -- not a PayPal delivery at all."""
    result = await allowed_hold(gateway, now)
    with pytest.raises(WebhookRejected):
        await deliver(
            gateway,
            event("PAYMENT.CAPTURE.COMPLETED", custom_id=result.decision_id),
            now=now,
            headers={"content-type": "application/json"},
        )
    assert gateway.store.get(result.decision_id).state is HoldState.AWAITING_BUYER


# -- replays ----------------------------------------------------------------


async def test_a_retried_delivery_is_applied_once(gateway, now):
    """PayPal retries. A capture applied twice reads as two captures."""
    result = await allowed_hold(gateway, now)
    body = event("PAYMENT.AUTHORIZATION.CREATED", event_id="WH-DUP", custom_id=result.decision_id, id="AUTH-1")

    first = await deliver(gateway, body, now=now)
    second = await deliver(gateway, body, now=now)

    assert first.action == "applied"
    assert second.action == "duplicate"
    events = gateway.store.events(result.decision_id)
    assert sum(1 for e in events if e["to_state"] == HoldState.HELD.value) == 1


async def test_a_verified_event_with_no_id_is_rejected(gateway, now):
    """Nothing to deduplicate on means it could be applied without limit."""
    result = await allowed_hold(gateway, now)
    body = {
        "event_type": "PAYMENT.AUTHORIZATION.CREATED",
        "resource": {"custom_id": result.decision_id},
    }
    with pytest.raises(WebhookRejected, match="no id"):
        await deliver(gateway, body, now=now)


# -- what each event does ---------------------------------------------------


async def test_the_buyer_approving_reserves_the_funds(gateway, paypal, now):
    """CHECKOUT.ORDER.APPROVED is an action, not a recorded fact.

    Nothing new is decided: the policy already allowed this basket and the order
    being authorized is one this gateway created.
    """
    result = await allowed_hold(gateway, now)
    order_id = gateway.store.get(result.decision_id).paypal_order_id
    paypal.approve_buyer(order_id)

    outcome = await deliver(
        gateway,
        event("CHECKOUT.ORDER.APPROVED", id=order_id, purchase_units=[{"custom_id": result.decision_id}]),
        now=now,
    )
    assert outcome.action == "applied"
    hold = gateway.store.get(result.decision_id)
    assert hold.state is HoldState.HELD
    assert hold.authorization_id


async def test_auto_place_off_records_without_reserving(tmp_path, paypal, now):
    store = Store(tmp_path / "state.db")
    gw = Gateway(
        store=store,
        ledger=Ledger(tmp_path / "decisions.jsonl", LEDGER_KEY),
        policy=demo_policy(),
        merchant_secret=MERCHANT_SECRET,
        paypal=paypal.client(),
        webhook_id="WH-TEST",
        auto_place=False,
    )
    try:
        result = await allowed_hold(gw, now)
        order_id = gw.store.get(result.decision_id).paypal_order_id
        paypal.approve_buyer(order_id)
        outcome = await deliver(
            gw, event("CHECKOUT.ORDER.APPROVED", id=order_id), now=now
        )
        assert outcome.action == "noted"
        assert gw.store.get(result.decision_id).state is HoldState.AWAITING_BUYER
    finally:
        store.close()


async def test_a_void_at_paypal_releases_our_hold(gateway, paypal, now):
    """An authorization can be voided by someone who is not this gateway."""
    result = await allowed_hold(gateway, now)
    await deliver(
        gateway,
        event("PAYMENT.AUTHORIZATION.CREATED", event_id="WH-A", custom_id=result.decision_id, id="AUTH-9"),
        now=now,
    )
    outcome = await deliver(
        gateway,
        event("PAYMENT.AUTHORIZATION.VOIDED", event_id="WH-B", id="AUTH-9"),
        now=now,
    )
    assert outcome.action == "applied"
    assert gateway.store.get(result.decision_id).state is HoldState.VOIDED


async def test_a_capture_records_its_amount(gateway, now):
    result = await allowed_hold(gateway, now)
    await deliver(
        gateway,
        event("PAYMENT.AUTHORIZATION.CREATED", event_id="WH-A", custom_id=result.decision_id, id="AUTH-7"),
        now=now,
    )
    amount = gateway.store.get(result.decision_id).amount
    outcome = await deliver(
        gateway,
        event(
            "PAYMENT.CAPTURE.COMPLETED",
            event_id="WH-C",
            id="CAP-1",
            amount={"value": amount.to_paypal(), "currency_code": amount.currency},
            supplementary_data={"related_ids": {"authorization_id": "AUTH-7"}},
        ),
        now=now,
    )
    assert outcome.action == "applied"
    hold = gateway.store.get(result.decision_id)
    assert hold.state is HoldState.CAPTURED
    assert hold.captured == amount


async def test_a_capture_in_another_currency_sets_no_amount(gateway, now):
    """Money refuses cross-currency arithmetic, and guessing would put a wrong
    number in a financial record. The state still moves; the figure does not."""
    result = await allowed_hold(gateway, now)
    await deliver(
        gateway,
        event("PAYMENT.AUTHORIZATION.CREATED", event_id="WH-A", custom_id=result.decision_id, id="AUTH-5"),
        now=now,
    )
    outcome = await deliver(
        gateway,
        event(
            "PAYMENT.CAPTURE.COMPLETED",
            event_id="WH-C",
            id="CAP-2",
            amount={"value": "10.00", "currency_code": "EUR"},
            supplementary_data={"related_ids": {"authorization_id": "AUTH-5"}},
        ),
        now=now,
    )
    assert outcome.action == "applied"
    assert "different currency" in outcome.detail
    hold = gateway.store.get(result.decision_id)
    assert hold.state is HoldState.CAPTURED
    assert hold.captured is None


# -- the things it refuses to do -------------------------------------------


async def test_an_unknown_event_type_is_acknowledged_not_silently_dropped(gateway, now):
    """A subscription changes under you. "We saw it and had no rule" beats
    "we never saw it"."""
    outcome = await deliver(
        gateway, event("CUSTOMER.DISPUTE.CREATED", event_id="WH-U"), now=now
    )
    assert outcome.action == "unhandled_event_type"
    assert outcome.event_type == "CUSTOMER.DISPUTE.CREATED"


async def test_an_event_about_someone_elses_order_is_not_forced_onto_a_hold(gateway, now):
    """The subscription is per-application, so a delivery can legitimately
    describe an order another process created."""
    await allowed_hold(gateway, now)
    outcome = await deliver(
        gateway,
        event("PAYMENT.CAPTURE.COMPLETED", event_id="WH-X", id="SOMEONE-ELSE"),
        now=now,
    )
    assert outcome.action == "no_matching_hold"
    assert outcome.decision_id is None


async def test_an_illegal_transition_is_reported_not_forced(gateway, now):
    """PayPal saying a captured hold is now held means our model is wrong.

    Overwriting the state would destroy the evidence of the disagreement, which is
    the one thing worth keeping.
    """
    result = await allowed_hold(gateway, now)
    await deliver(
        gateway,
        event("PAYMENT.AUTHORIZATION.CREATED", event_id="WH-A", custom_id=result.decision_id, id="AUTH-3"),
        now=now,
    )
    await deliver(
        gateway,
        event(
            "PAYMENT.CAPTURE.COMPLETED",
            event_id="WH-B",
            id="CAP-3",
            supplementary_data={"related_ids": {"authorization_id": "AUTH-3"}},
        ),
        now=now,
    )
    outcome = await deliver(
        gateway,
        event("PAYMENT.AUTHORIZATION.CREATED", event_id="WH-C", custom_id=result.decision_id, id="AUTH-3"),
        now=now,
    )
    assert outcome.action == "illegal_transition"
    assert gateway.store.get(result.decision_id).state is HoldState.CAPTURED


async def test_no_webhook_can_create_a_hold(gateway, now):
    """The transition table can only move holds that already exist. An event is
    allowed to say what happened at PayPal and nothing else."""
    before = len(gateway.store.list())
    await deliver(
        gateway,
        event("PAYMENT.CAPTURE.COMPLETED", event_id="WH-N", custom_id="dec_invented"),
        now=now,
    )
    assert len(gateway.store.list()) == before
