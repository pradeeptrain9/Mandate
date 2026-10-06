"""The store: legal transitions only, derived reserved-ness, single-use tokens."""

from __future__ import annotations

from datetime import timedelta

import pytest

from mandate.engine.quote import Category
from mandate.gateway.state import HoldState, IllegalTransition
from mandate.gateway.store import Store, UnknownHold, token_digest

from helpers import quote


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def received(store, now, *, decision_id="dec_1", **kw):
    q = quote(**kw)
    store.create(
        decision_id=decision_id,
        quote=q,
        policy_id="demo-ops-agent",
        engine_outcome="allow",
        state=HoldState.RECEIVED,
        at=now,
    )
    return q


def test_create_records_the_entry_as_an_event(store, now):
    received(store, now)
    hold = store.get("dec_1")
    assert hold.state is HoldState.RECEIVED
    events = store.events("dec_1")
    assert len(events) == 1
    assert events[0]["from_state"] is None
    assert events[0]["to_state"] == "received"


def test_unknown_hold_raises(store):
    with pytest.raises(UnknownHold):
        store.get("nope")


def test_transition_records_from_and_to(store, now):
    received(store, now)
    store.transition("dec_1", HoldState.AWAITING_BUYER, detail="order created", at=now)
    store.transition("dec_1", HoldState.HELD, detail="auth placed", at=now)
    assert [(e["from_state"], e["to_state"]) for e in store.events("dec_1")] == [
        (None, "received"),
        ("received", "awaiting_buyer"),
        ("awaiting_buyer", "held"),
    ]


def test_illegal_transition_is_refused(store, now):
    received(store, now)
    store.transition("dec_1", HoldState.REFUSED, at=now)
    with pytest.raises(IllegalTransition, match="terminal"):
        store.transition("dec_1", HoldState.HELD, at=now)


def test_captured_cannot_go_back_to_held(store, now):
    received(store, now)
    store.transition("dec_1", HoldState.AWAITING_BUYER, at=now)
    store.transition("dec_1", HoldState.HELD, at=now)
    store.transition("dec_1", HoldState.CAPTURED, at=now)
    with pytest.raises(IllegalTransition):
        store.transition("dec_1", HoldState.HELD, at=now)


def test_transition_sets_paypal_columns_in_the_same_statement(store, now):
    """There must be no moment where state says HELD and the authorization id is
    still null."""
    received(store, now)
    store.transition("dec_1", HoldState.AWAITING_BUYER, at=now, paypal_order_id="ORDER-1")
    hold = store.transition(
        "dec_1",
        HoldState.HELD,
        at=now,
        authorization_id="AUTH-9",
        authorization_expires_at=now + timedelta(days=29),
    )
    assert hold.authorization_id == "AUTH-9"
    assert hold.paypal_order_id == "ORDER-1"
    assert (hold.authorization_expires_at - now).days == 29


def test_unknown_columns_are_rejected_rather_than_ignored(store, now):
    received(store, now)
    with pytest.raises(ValueError, match="not columns on holds"):
        store.transition("dec_1", HoldState.REFUSED, at=now, amount_minor=1)


def test_find_by_authorization_and_order(store, now):
    received(store, now)
    store.transition("dec_1", HoldState.AWAITING_BUYER, at=now, paypal_order_id="ORDER-1")
    store.transition("dec_1", HoldState.HELD, at=now, authorization_id="AUTH-9")
    assert store.find_by_order("ORDER-1").decision_id == "dec_1"
    assert store.find_by_authorization("AUTH-9").decision_id == "dec_1"
    assert store.find_by_order("ORDER-X") is None


# -- approval tokens --------------------------------------------------------


def test_only_the_token_digest_is_stored(store, now):
    received(store, now)
    store.transition("dec_1", HoldState.AWAITING_HUMAN, at=now)
    token = store.issue_approval_token("dec_1", at=now)
    # tuple(), not str(row): sqlite3.Row's repr hides the column values, which
    # would make this assertion pass for the wrong reason.
    dumped = "\n".join(
        repr(tuple(row)) for row in store._connection.execute("SELECT * FROM holds").fetchall()
    )
    assert token not in dumped
    assert token_digest(token) in dumped


def test_a_token_works_once(store, now):
    received(store, now)
    store.transition("dec_1", HoldState.AWAITING_HUMAN, at=now)
    token = store.issue_approval_token("dec_1", at=now)
    assert store.consume_approval_token(token, at=now).decision_id == "dec_1"
    assert store.consume_approval_token(token, at=now) is None


def test_an_expired_token_is_refused(store, now):
    received(store, now)
    store.transition("dec_1", HoldState.AWAITING_HUMAN, at=now)
    token = store.issue_approval_token("dec_1", ttl=timedelta(minutes=15), at=now)
    assert store.consume_approval_token(token, at=now + timedelta(minutes=16)) is None


def test_an_unknown_token_is_refused(store):
    assert store.consume_approval_token("not-a-token") is None


def test_a_token_for_a_hold_that_moved_on_is_refused(store, now):
    received(store, now)
    store.transition("dec_1", HoldState.AWAITING_HUMAN, at=now)
    token = store.issue_approval_token("dec_1", at=now)
    store.transition("dec_1", HoldState.DECLINED_BY_HUMAN, at=now)
    assert store.consume_approval_token(token, at=now) is None


# -- webhook replay ---------------------------------------------------------


def test_a_webhook_event_is_new_exactly_once(store, now):
    assert store.remember_webhook("WH-1", "PAYMENT.CAPTURE.COMPLETED", at=now) is True
    assert store.remember_webhook("WH-1", "PAYMENT.CAPTURE.COMPLETED", at=now) is False


# -- the window the engine reads -------------------------------------------


def _window_for(store, now, state, *, decision_id, amount="50.00"):
    q = received(
        store,
        now,
        decision_id=decision_id,
        items=[("SKU-X", "x", Category.OFFICE_SUPPLIES, amount, 1)],
    )
    if state is HoldState.RECEIVED:
        return q
    store.transition(decision_id, HoldState.AWAITING_BUYER, at=now, placed_at=now)
    if state is HoldState.AWAITING_BUYER:
        return q
    store.transition(decision_id, HoldState.HELD, at=now)
    if state is HoldState.HELD:
        return q
    store.transition(decision_id, state, at=now)
    return q


def test_reserved_is_derived_from_state_so_it_cannot_drift(store, now):
    _window_for(store, now, HoldState.HELD, decision_id="d_held")
    _window_for(store, now, HoldState.VOIDED, decision_id="d_void")
    _window_for(store, now, HoldState.CAPTURED, decision_id="d_cap")
    window = store.ledger_window(since=now - timedelta(days=1))
    # All three baskets are identical, so they share a fingerprint -- count the
    # entries rather than keying them by one.
    assert len(window.entries) == 3
    assert sum(1 for e in window.entries if e.reserved) == 2  # held and captured, not voided


def test_refused_and_awaiting_human_never_reach_the_window(store, now):
    _window_for(store, now, HoldState.RECEIVED, decision_id="d_recv")
    received(store, now, decision_id="d_refused")
    store.transition("d_refused", HoldState.REFUSED, at=now)
    received(store, now, decision_id="d_human")
    store.transition("d_human", HoldState.AWAITING_HUMAN, at=now)
    assert store.ledger_window(since=now - timedelta(days=1)).entries == ()


def test_an_order_awaiting_the_buyer_already_occupies_the_envelope(store, now):
    """Otherwise an agent could queue a hundred unapproved orders, each measured
    against an empty envelope, and settle them all at once."""
    _window_for(store, now, HoldState.AWAITING_BUYER, decision_id="d_await")
    window = store.ledger_window(since=now - timedelta(days=1))
    assert [e.reserved for e in window.entries] == [True]


def test_window_respects_the_since_cutoff(store, now):
    _window_for(store, now, HoldState.HELD, decision_id="d_old")
    store._connection.execute(
        "UPDATE holds SET requested_at = ?, placed_at = ? WHERE decision_id = 'd_old'",
        ((now - timedelta(days=5)).isoformat(), (now - timedelta(days=5)).isoformat()),
    )
    assert store.ledger_window(since=now - timedelta(days=1)).entries == ()


def test_expiring_before_finds_only_held_authorizations(store, now):
    _window_for(store, now, HoldState.HELD, decision_id="d_held")
    store.transition(
        "d_held", HoldState.HELD, at=now, authorization_expires_at=now + timedelta(days=2)
    )
    _window_for(store, now, HoldState.VOIDED, decision_id="d_void")
    soon = store.expiring_before(now + timedelta(days=3))
    assert [h.decision_id for h in soon] == ["d_held"]
    assert store.expiring_before(now + timedelta(hours=1)) == []
