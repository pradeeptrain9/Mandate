"""Decision records: round-trip, tamper detection, and replay."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from mandate.engine.policy import LedgerWindow, Outcome, PriorAuthorization
from mandate.engine.quote import Category
from mandate.ledger import codec
from mandate.ledger.records import (
    DecisionRecord,
    Ledger,
    ReplayMismatch,
    SignatureInvalid,
    WrongLedgerKey,
    build,
    key_id,
)
from mandate.policies import demo_policy

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


# -- which key signed this --------------------------------------------------
#
# Added after four records in this repository's own ledger started reporting
# TAMPERED following a key rotation -- indistinguishable, from the output, from
# someone having edited the file. An alarm that cries wolf about a key change is an
# alarm an operator learns to ignore.


def test_a_record_names_the_key_that_signed_it():
    record = build(
        quote=quote(),
        policy=demo_policy(),
        ledger_window=LedgerWindow(()),
        evaluated_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
        key=b"key-alpha",
    )
    assert record.key_id == key_id(b"key-alpha")
    # Short, and not the key: it identifies, it does not authenticate.
    assert len(record.key_id) == 16
    assert "key-alpha" not in record.to_json()


def test_key_ids_differ_per_key_and_are_stable():
    assert key_id(b"a") != key_id(b"b")
    assert key_id(b"a") == key_id(b"a")
    assert key_id(b"") == ""


def test_key_id_survives_a_round_trip_through_json():
    record = build(
        quote=quote(),
        policy=demo_policy(),
        ledger_window=LedgerWindow(()),
        evaluated_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
        key=b"key-alpha",
    )
    back = DecisionRecord.from_json(record.to_json())
    assert back.key_id == record.key_id
    back.verify(b"key-alpha")


def test_key_id_is_outside_the_signature_so_old_records_still_verify():
    """It has to be, and the consequence is that it is forgeable.

    Adding a field to the signed payload would invalidate every record ever
    written, which for an append-only ledger means rewriting history to fix a
    diagnostic. So key_id sits beside the signature, and the tests below pin that
    this never makes an unverifiable record look acceptable.
    """
    record = build(
        quote=quote(),
        policy=demo_policy(),
        ledger_window=LedgerWindow(()),
        evaluated_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
        key=b"key-alpha",
    )
    assert "key_id" not in record.unsigned_payload()
    # Strip it and the signature is unaffected: proof it is not covered.
    stripped = DecisionRecord(**{**_record_fields(record), "key_id": ""})
    stripped.verify(b"key-alpha")


def _record_fields(record: DecisionRecord) -> dict:
    return {
        "decision_id": record.decision_id,
        "created_at": record.created_at,
        "evaluated_at": record.evaluated_at,
        "quote": record.quote,
        "request": record.request,
        "policy": record.policy,
        "ledger_window": record.ledger_window,
        "evaluation": record.evaluation,
        "signature": record.signature,
        "record_version": record.record_version,
    }


def test_the_wrong_key_is_reported_as_the_wrong_key():
    record = build(
        quote=quote(),
        policy=demo_policy(),
        ledger_window=LedgerWindow(()),
        evaluated_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
        key=b"key-alpha",
    )
    with pytest.raises(WrongLedgerKey) as caught:
        record.verify(b"key-beta")
    message = str(caught.value)
    assert key_id(b"key-alpha") in message
    assert key_id(b"key-beta") in message
    # And it still says the record is unverified, because it is.
    assert "still unverified" in message


def test_the_wrong_key_error_is_still_a_signature_failure():
    """A subclass on purpose. Every caller that treated an unverifiable record as a
    problem keeps doing so without being edited, and no one can accidentally start
    reading "signed with another key" as "fine"."""
    assert issubclass(WrongLedgerKey, SignatureInvalid)


def test_a_forged_key_id_does_not_excuse_a_broken_signature():
    """key_id is unsigned, so an attacker can write anything in it -- including the
    id of a key nobody holds, which would let a forgery describe itself as merely
    old. It must stay an error."""
    record = build(
        quote=quote(),
        policy=demo_policy(),
        ledger_window=LedgerWindow(()),
        evaluated_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
        key=b"key-alpha",
    )
    forged = DecisionRecord(
        **{**_record_fields(record), "key_id": "0000000000000000", "signature": "00" * 32}
    )
    with pytest.raises(SignatureInvalid):
        forged.verify(b"key-alpha")


def test_a_record_with_no_key_id_says_it_cannot_tell():
    """The honest answer for records written before the field existed."""
    record = build(
        quote=quote(),
        policy=demo_policy(),
        ledger_window=LedgerWindow(()),
        evaluated_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
        key=b"key-alpha",
    )
    legacy = DecisionRecord(**{**_record_fields(record), "key_id": ""})
    with pytest.raises(SignatureInvalid) as caught:
        legacy.verify(b"key-beta")
    assert "cannot be told apart" in str(caught.value)


# -- rotation ---------------------------------------------------------------


def test_a_retired_key_verifies_the_records_it_signed(tmp_path):
    """Rotation adds a key, it never replaces one.

    An append-only ledger outlives its key by definition: re-signing the past would
    mean rewriting it, which is the one thing the format exists to prevent.
    """
    old = Ledger(tmp_path / "l.jsonl", b"key-alpha")
    old.append(
        build(
            quote=quote(),
            policy=demo_policy(),
            ledger_window=LedgerWindow(()),
            evaluated_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
            key=b"key-alpha",
        )
    )

    rotated = Ledger(tmp_path / "l.jsonl", b"key-beta")
    with pytest.raises(WrongLedgerKey):
        rotated.verify_all()

    with_history = Ledger(tmp_path / "l.jsonl", b"key-beta", retired_keys=[b"key-alpha"])
    assert with_history.verify_all() == 1


def test_check_reports_which_key_verified(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", b"key-alpha")
    record = build(
        quote=quote(),
        policy=demo_policy(),
        ledger_window=LedgerWindow(()),
        evaluated_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
        key=b"key-alpha",
    )
    ledger.append(record)

    rotated = Ledger(tmp_path / "l.jsonl", b"key-beta", retired_keys=[b"key-alpha"])
    assert rotated.check(record) == key_id(b"key-alpha")

    fresh = build(
        quote=quote(),
        policy=demo_policy(),
        ledger_window=LedgerWindow(()),
        evaluated_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
        key=b"key-beta",
    )
    assert rotated.check(fresh) == key_id(b"key-beta")


def test_a_record_cannot_nominate_a_key_into_existence(tmp_path):
    """The fallback only tries keys this ledger was handed. A record naming a key
    nobody configured fails, which is what stops key_id becoming load-bearing."""
    ledger = Ledger(tmp_path / "l.jsonl", b"key-beta", retired_keys=[b"key-gamma"])
    record = build(
        quote=quote(),
        policy=demo_policy(),
        ledger_window=LedgerWindow(()),
        evaluated_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
        key=b"key-alpha",
    )
    with pytest.raises(WrongLedgerKey):
        ledger.check(record)


def test_appending_still_requires_the_current_key(tmp_path):
    """A retired key verifies history; it must not be able to write new history."""
    ledger = Ledger(tmp_path / "l.jsonl", b"key-beta", retired_keys=[b"key-alpha"])
    stale = build(
        quote=quote(),
        policy=demo_policy(),
        ledger_window=LedgerWindow(()),
        evaluated_at=datetime(2026, 10, 6, 12, tzinfo=timezone.utc),
        key=b"key-alpha",
    )
    with pytest.raises(WrongLedgerKey):
        ledger.append(stale)
    assert not (tmp_path / "l.jsonl").exists() or (tmp_path / "l.jsonl").read_text() == ""
