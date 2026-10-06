"""Decision records: signed, append-only, and executable.

Most systems that move money keep an audit log. A log says what happened. This
keeps something stronger: the complete input to a decision, so the decision can
be *re-made* and compared. `mandate replay` reads a record, feeds the stored
policy, request and ledger window back through `engine.policy.evaluate`, and
asserts the outcome and every rule verdict come out identical.

That turns two vague claims into tested ones. "The engine is deterministic" is
checked against real history rather than asserted in a README. And "this is what
the policy said at the time" survives the policy being edited afterwards,
because the record carries the policy it ran against rather than a reference to
a mutable row.

The HMAC is over the canonical bytes of the record minus its own signature.
It detects tampering by anything that does not hold the key -- which includes
every component of this system except the gateway. It is explicitly not a
defence against the gateway itself; that would need an external notary, and
saying so is better than implying a property the code does not have.

`key_id` exists because of a real incident in this repository. The ledger key was
rotated, and the four records signed with the old one started reporting
`TAMPERED` -- indistinguishable, from the output, from someone having edited the
file. That is a bad failure mode for an audit log: the alarm that should mean
"something is wrong" instead meant "a key changed months ago", and an operator who
learns to expect the alarm stops reading it.

So each record now carries a short digest naming the key that signed it. Two
things about it matter more than the feature:

  * **It is not part of the signed payload.** It cannot be, because adding a field
    to the payload would invalidate every record ever written. That is a real
    constraint, not a shortcut, and the consequence is that `key_id` is
    *forgeable*.
  * **So it is a routing hint and never a verdict.** It selects which key to try.
    It never decides whether a record is genuine, and a record whose `key_id` names
    a key nobody has is a failure -- `WrongLedgerKey` is a subclass of
    `SignatureInvalid` precisely so that no existing caller can accidentally start
    treating "signed with another key" as "fine".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Sequence
from typing import Any, Iterator

from ..engine.policy import Evaluation, LedgerWindow, Policy, evaluate
from ..engine.quote import MerchantQuote, PolicyInput
from . import codec

RECORD_VERSION = "1"


class SignatureInvalid(ValueError):
    """A record whose HMAC does not match its contents."""


class WrongLedgerKey(SignatureInvalid):
    """The record names a different signing key than the one supplied.

    Still a verification failure, and deliberately a *subclass* of
    SignatureInvalid: every caller that treated an unverifiable record as a
    problem keeps doing so without being edited. The extra type only lets a caller
    that wants to distinguish rotation from tampering do it on purpose.

    It is not proof of rotation either. `key_id` is unsigned, so anyone editing a
    record can write whatever they like in it -- including the id of a key that
    does not exist, which would make a forgery describe itself as "just an old
    key". That is why this remains an error and why its message says so.
    """


def key_id(key: bytes) -> str:
    """A short, non-reversible name for a signing key.

    Domain-separated so the digest cannot be matched against the same key hashed
    somewhere else, and truncated because it identifies rather than authenticates:
    a collision lets someone mislabel a record they still cannot sign.
    """
    if not key:
        return ""
    return hashlib.sha256(b"mandate/ledger-key-id/v1\x00" + key).hexdigest()[:16]


class ReplayMismatch(AssertionError):
    """Re-running a stored decision produced a different answer.

    Either the engine changed behaviour without a version bump, or the record
    was altered by something holding the key. Both are serious; neither is
    recoverable by retrying.
    """


@dataclass(frozen=True)
class DecisionRecord:
    """One decision, with everything needed to make it again."""

    decision_id: str
    created_at: datetime
    evaluated_at: datetime  # the `now` passed to the engine, which replay must reuse
    quote: MerchantQuote
    request: PolicyInput
    policy: Policy
    ledger_window: LedgerWindow
    evaluation: Evaluation
    signature: str = ""
    #: Which key signed this. Outside the signed payload -- see the module
    #: docstring. Empty on records written before this field existed.
    key_id: str = ""
    record_version: str = RECORD_VERSION

    # -- serialisation ---------------------------------------------------

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "record_version": self.record_version,
            "decision_id": self.decision_id,
            "created_at": codec.enc_dt(self.created_at),
            "evaluated_at": codec.enc_dt(self.evaluated_at),
            "quote": codec.enc_quote(self.quote),
            "request": codec.enc_policy_input(self.request),
            "policy": codec.enc_policy(self.policy),
            "ledger_window": codec.enc_ledger_window(self.ledger_window),
            "evaluation": codec.enc_evaluation(self.evaluation),
        }

    def sign(self, key: bytes) -> "DecisionRecord":
        mac = hmac.new(key, codec.canonical_json(self.unsigned_payload()), hashlib.sha256)
        return DecisionRecord(
            **{**_fields(self), "signature": mac.hexdigest(), "key_id": key_id(key)}
        )

    def verify(self, key: bytes) -> None:
        """Check the HMAC. Raises on any failure; the type says which kind.

        The signature is always computed and compared, whatever `key_id` says. The
        hint only shapes the message, because believing an unsigned field about
        which key was used would make the unsigned field load-bearing.
        """
        expected = hmac.new(
            key, codec.canonical_json(self.unsigned_payload()), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(expected, self.signature or ""):
            return

        mine = key_id(key)
        if self.key_id and self.key_id != mine:
            raise WrongLedgerKey(
                f"record {self.decision_id} was signed with ledger key {self.key_id}, "
                f"not the configured {mine}. This is still unverified: either the key "
                f"was rotated, or the record was altered by someone who also wrote a "
                f"key id nobody holds."
            )
        if not self.key_id:
            raise SignatureInvalid(
                f"record {self.decision_id} does not verify, and names no signing key "
                f"(written before key ids existed), so a rotated key and an altered "
                f"record cannot be told apart here"
            )
        raise SignatureInvalid(
            f"record {self.decision_id} does not verify under the key it names ({mine})"
        )

    def to_json(self) -> str:
        payload = self.unsigned_payload()
        payload["signature"] = self.signature
        # Written beside the signature rather than inside the signed payload. A
        # reader needs it before it can choose a key, which is the one moment it
        # cannot already have verified anything.
        if self.key_id:
            payload["key_id"] = self.key_id
        return codec.canonical_json(payload).decode("ascii")

    @classmethod
    def from_json(cls, line: str) -> "DecisionRecord":
        raw = json.loads(line)
        return cls(
            decision_id=raw["decision_id"],
            created_at=codec.dec_dt(raw["created_at"]),
            evaluated_at=codec.dec_dt(raw["evaluated_at"]),
            quote=codec.dec_quote(raw["quote"]),
            request=codec.dec_policy_input(raw["request"]),
            policy=codec.dec_policy(raw["policy"]),
            ledger_window=codec.dec_ledger_window(raw["ledger_window"]),
            evaluation=codec.dec_evaluation(raw["evaluation"]),
            signature=raw.get("signature", ""),
            key_id=raw.get("key_id", ""),
            record_version=raw["record_version"],
        )

    # -- the point of all this -------------------------------------------

    def replay(self) -> Evaluation:
        """Re-run the stored inputs through the live engine.

        Note `self.evaluated_at` rather than the current time: a replay that
        used "now" would drift every rolling window and fail for reasons that
        have nothing to do with correctness.
        """
        return evaluate(self.request, self.policy, self.ledger_window, self.evaluated_at)

    def assert_replays(self) -> Evaluation:
        fresh = self.replay()
        stored = self.evaluation
        if fresh.outcome is not stored.outcome:
            raise ReplayMismatch(
                f"{self.decision_id}: stored {stored.outcome.value}, replayed {fresh.outcome.value}"
            )
        if codec.enc_evaluation(fresh) != codec.enc_evaluation(stored):
            stored_ids = {r.rule_id: r.outcome.value for r in stored.results}
            fresh_ids = {r.rule_id: r.outcome.value for r in fresh.results}
            differing = sorted(
                k for k in stored_ids.keys() | fresh_ids.keys() if stored_ids.get(k) != fresh_ids.get(k)
            )
            raise ReplayMismatch(
                f"{self.decision_id}: outcome matched but the rule trace differs"
                + (f" on {', '.join(differing)}" if differing else " in rule facts or messages")
            )
        return fresh


def _fields(record: DecisionRecord) -> dict[str, Any]:
    return {
        "decision_id": record.decision_id,
        "created_at": record.created_at,
        "evaluated_at": record.evaluated_at,
        "quote": record.quote,
        "request": record.request,
        "policy": record.policy,
        "ledger_window": record.ledger_window,
        "evaluation": record.evaluation,
        "key_id": record.key_id,
        "record_version": record.record_version,
    }


def build(
    *,
    quote: MerchantQuote,
    policy: Policy,
    ledger_window: LedgerWindow,
    evaluated_at: datetime,
    key: bytes,
    decision_id: str | None = None,
) -> DecisionRecord:
    """Evaluate a quote and return the signed record of having done so."""
    request = quote.to_policy_input()
    evaluation = evaluate(request, policy, ledger_window, evaluated_at)
    record = DecisionRecord(
        decision_id=decision_id or f"dec_{uuid.uuid4().hex[:16]}",
        created_at=datetime.now(timezone.utc),
        evaluated_at=evaluated_at,
        quote=quote,
        request=request,
        policy=policy,
        ledger_window=ledger_window,
        evaluation=evaluation,
    )
    return record.sign(key)


class Ledger:
    """Append-only JSONL on disk.

    One line per record, opened with "a" and never rewritten. A database would
    be tidier and would also hand whoever has the connection an UPDATE
    statement; a file that is only ever appended to makes rewriting history
    something you have to do visibly.
    """

    def __init__(self, path: Path, key: bytes, retired_keys: Sequence[bytes] = ()) -> None:
        self.path = Path(path)
        self.key = key
        #: Keys that no longer sign anything but still have to verify history.
        #:
        #: Without this, `key_id` would only let an operator read a nicer error
        #: message about records they still cannot check. An append-only ledger
        #: outlives its key by definition -- you cannot re-sign the past without
        #: rewriting it, which is the one thing the format exists to prevent -- so
        #: rotation has to mean "add a key", never "replace one".
        self.retired_keys: dict[str, bytes] = {key_id(k): k for k in retired_keys if k}
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: DecisionRecord) -> DecisionRecord:
        record.verify(self.key)
        with self.path.open("a", encoding="ascii") as handle:
            handle.write(record.to_json() + "\n")
        return record

    def __iter__(self) -> Iterator[DecisionRecord]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="ascii") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield DecisionRecord.from_json(line)

    def check(self, record: DecisionRecord) -> str:
        """Verify under the current key, falling back to a retired one it names.

        Returns the id of the key that worked, so a caller can report *which*.
        Raises exactly as `verify` does when none of them do.

        The fallback is tried only for the key the record names, and only for keys
        this ledger was handed. A record cannot nominate a key into existence.
        """
        try:
            record.verify(self.key)
            return key_id(self.key)
        except WrongLedgerKey:
            retired = self.retired_keys.get(record.key_id)
            if retired is None:
                raise
            # If this verifies, the record is genuine and was simply signed before
            # a rotation. If it does not, the original error stands.
            record.verify(retired)
            return record.key_id

    def verify_all(self) -> int:
        count = 0
        for record in self:
            self.check(record)
            count += 1
        return count

    def replay_all(self) -> int:
        count = 0
        for record in self:
            self.check(record)
            record.assert_replays()
            count += 1
        return count
