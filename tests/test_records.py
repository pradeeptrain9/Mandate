"""Decision records: round-trip, tamper detection, and replay."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from mandate.engine.policy import LedgerWindow, Outcome, PriorAuthorization
from mandate.engine.quote import Category
from mandate.ledger import codec
from mandate.ledger.records import (
    DecisionRecord,
    Ledger,
    ReplayMismatch,
    SignatureInvalid,
    build,
)

from helpers import LEDGER_KEY, quote, usd


def record(policy, now, **kw):
    return build(
        quote=kw.pop("q", None) or quote(),
        policy=policy,
        ledger_window=kw.pop("ledger_window", LedgerWindow()),
        evaluated_at=now,
        key=LEDGER_KEY,
        **kw,
    )


def test_round_trips_through_json_without_loss(policy, now):
    original = record(policy, now)
    revived = DecisionRecord.from_json(original.to_json())
    assert revived.to_json() == original.to_json()
    revived.verify(LEDGER_KEY)
    assert revived.evaluation.outcome is original.evaluation.outcome
    assert revived.policy == original.policy
    assert revived.quote == original.quote
    assert revived.request == original.request


def test_round_trip_preserves_a_populated_ledger_window(policy, now):
    window = LedgerWindow(
        (
            PriorAuthorization(
                at=now - timedelta(minutes=20),
                merchant_id="m_acme",
                amount=usd("40.00"),
                fingerprint="abc123",
                categories=frozenset({Category.OFFICE_SUPPLIES, Category.FOOD}),
            ),
        )
    )
    original = record(policy, now, ledger_window=window)
    revived = DecisionRecord.from_json(original.to_json())
    assert revived.ledger_window == original.ledger_window


def test_signature_verifies_and_detects_a_doctored_amount(policy, now):
    original = record(policy, now)
    original.verify(LEDGER_KEY)
    forged = replace(original, quote=replace(original.quote, declared_total=usd("0.01")))
    with pytest.raises(SignatureInvalid):
        forged.verify(LEDGER_KEY)


def test_signature_detects_a_doctored_outcome(policy, now):
    """The most tempting edit: leave the inputs, change the answer."""
    original = record(policy, now)
    forged = replace(
        original, evaluation=replace(original.evaluation, outcome=Outcome.ALLOW)
    )
    if original.evaluation.outcome is Outcome.ALLOW:
        forged = replace(original, evaluation=replace(original.evaluation, outcome=Outcome.DENY))
    with pytest.raises(SignatureInvalid):
        forged.verify(LEDGER_KEY)


def test_a_record_replays_to_the_same_answer(policy, now):
    original = record(policy, now)
    assert original.assert_replays().outcome is original.evaluation.outcome


def test_replay_uses_the_recorded_instant_not_the_current_one(policy, now):
    """A rolling window evaluated against 'now' would drift and fail for reasons
    that have nothing to do with correctness."""
    window = LedgerWindow(
        (
            PriorAuthorization(
                at=now - timedelta(minutes=30),
                merchant_id="m_acme",
                amount=usd("150.00"),
                fingerprint="xyz",
                categories=frozenset({Category.OFFICE_SUPPLIES}),
            ),
        )
    )
    q = quote(items=[("SKU-PAPER", "Paper", Category.OFFICE_SUPPLIES, "60.00", 1)])
    original = record(policy, now, q=q, ledger_window=window)
    assert original.evaluation.outcome is Outcome.DENY  # hour envelope would be breached
    original.assert_replays()  # still DENY, because evaluated_at is replayed too


def test_replay_mismatch_is_raised_when_the_stored_answer_was_rewritten(policy, now):
    """Someone with the key edits the stored outcome and re-signs. The signature
    now verifies -- replay is what catches it."""
    original = record(policy, now)
    assert original.evaluation.outcome is Outcome.ALLOW
    doctored = replace(
        original, evaluation=replace(original.evaluation, outcome=Outcome.DENY)
    ).sign(LEDGER_KEY)
    doctored.verify(LEDGER_KEY)  # the forgery is internally consistent
    with pytest.raises(ReplayMismatch, match="stored deny, replayed allow"):
        doctored.assert_replays()


def test_replay_mismatch_catches_a_rewritten_rule_trace(policy, now):
    original = record(policy, now)
    results = list(original.evaluation.results)
    results[0] = replace(results[0], message="nothing to see here")
    doctored = replace(
        original, evaluation=replace(original.evaluation, results=tuple(results))
    ).sign(LEDGER_KEY)
    with pytest.raises(ReplayMismatch, match="rule trace differs"):
        doctored.assert_replays()


def test_naive_datetimes_are_refused_rather_than_assumed_to_be_utc(policy, now):
    original = record(policy, now)
    naive = replace(original, evaluated_at=datetime(2026, 10, 6, 12, 0, 0))
    with pytest.raises(ValueError, match="naive datetime"):
        naive.to_json()


# -- the ledger file --------------------------------------------------------


def test_ledger_appends_and_reads_back(tmp_path, policy, now):
    ledger = Ledger(tmp_path / "decisions.jsonl", LEDGER_KEY)
    written = [ledger.append(record(policy, now)) for _ in range(3)]
    read_back = list(ledger)
    assert [r.decision_id for r in read_back] == [r.decision_id for r in written]
    assert ledger.verify_all() == 3
    assert ledger.replay_all() == 3


def test_ledger_refuses_to_append_an_unsigned_record(tmp_path, policy, now):
    ledger = Ledger(tmp_path / "decisions.jsonl", LEDGER_KEY)
    unsigned = replace(record(policy, now), signature="")
    with pytest.raises(SignatureInvalid):
        ledger.append(unsigned)
    assert not (tmp_path / "decisions.jsonl").exists()


def test_verify_all_fails_on_a_line_edited_in_place(tmp_path, policy, now):
    path = tmp_path / "decisions.jsonl"
    ledger = Ledger(path, LEDGER_KEY)
    ledger.append(record(policy, now))
    path.write_text(path.read_text().replace('"item_count":4', '"item_count":400'))
    with pytest.raises(SignatureInvalid):
        ledger.verify_all()


def test_canonical_json_is_byte_stable_regardless_of_key_order(policy, now):
    original = record(policy, now)
    payload = original.unsigned_payload()
    shuffled = dict(reversed(list(payload.items())))
    assert codec.canonical_json(payload) == codec.canonical_json(shuffled)
