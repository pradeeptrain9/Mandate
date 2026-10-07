"""Where the live rules live, and why editing them appends rather than overwrites.

Making the policy editable is the difference between a demonstration and something
an operations team can run. It is also the single most dangerous surface in this
project: everything else decides *within* the rules, and this decides *what the
rules are*. A mistake here does not produce a wrong decision, it produces a wrong
decision procedure.

Three consequences follow, and they are the whole design.

**Every edit is a new version. Nothing is ever updated in place.** The ledger's
claim is that a stored decision replays -- re-run its inputs through the engine and
get the identical answer. That survives policy edits only because each decision
record carries the full policy it ran against rather than a reference to one. This
table is the same argument applied to the rules themselves: "what were the limits
in March" is a question someone asks after something goes wrong, and an UPDATE
destroys the answer.

**Every edit is attributed.** Who changed the ceiling, when, and why they said they
were doing it. A spending limit that can be raised anonymously is not a limit.

**Every edit is validated before it is accepted, and dangerous ones are named.**
Refusing a nonsensical policy is easy. The harder and more useful part is telling an
admin plainly that they have just tripled a cap or disabled a category check --
things that are legitimate and should still never happen silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..engine.money import Money
from ..engine.policy import Policy
from ..ledger.codec import dec_policy, enc_policy


class PolicyRejected(ValueError):
    """The proposed rules are not a usable policy. Carries every problem, not the
    first: an admin fixing one field at a time, reloading between each, is an admin
    who stops reading."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class PolicyVersion:
    version: int
    policy: Policy
    author: str
    note: str
    created_at: datetime


def validate(policy: Policy) -> list[str]:
    """Everything wrong with this policy, in plain language. Empty means usable."""
    problems: list[str] = []

    if not policy.policy_id.strip():
        problems.append("the policy needs a name")
    if policy.hard_per_transaction_cap.minor <= 0:
        problems.append("the hard per-transaction cap must be more than zero")
    if policy.approval_threshold.minor < 0:
        problems.append("the approval threshold cannot be negative")
    if policy.approval_threshold > policy.hard_per_transaction_cap:
        # Not a style objection. Above the hard cap nothing is approvable at all,
        # so the threshold would name a tier that cannot be reached, and the
        # "ask a human" outcome would never occur.
        problems.append(
            f"the approval threshold ({policy.approval_threshold.to_paypal()}) is above the "
            f"hard cap ({policy.hard_per_transaction_cap.to_paypal()}), which would mean no "
            "purchase is ever small enough to ask a human about"
        )
    if policy.currency not in policy.allowed_currencies:
        problems.append(
            f"the policy is written in {policy.currency} but {policy.currency} is not in the "
            "allowed currencies, so every request would be refused"
        )
    overlap = policy.allowed_categories & policy.denied_categories
    if overlap:
        names = ", ".join(sorted(c.value for c in overlap))
        problems.append(f"these categories are both allowed and denied: {names}")
    for category, cap in policy.category_caps:
        if cap.minor <= 0:
            problems.append(f"the cap for {category.value} must be more than zero")
    for envelope in policy.envelopes:
        if envelope.cap.minor <= 0:
            problems.append(f"the {envelope.label} budget must be more than zero")
        if envelope.duration.total_seconds() <= 0:
            problems.append(f"the {envelope.label} budget needs a window longer than zero")
    if policy.velocity_limit < 0:
        problems.append("the velocity limit cannot be negative")
    if policy.quote_max_age.total_seconds() <= 0:
        problems.append("quotes must be allowed to be at least a moment old")
    return problems


def warnings(old: Policy | None, new: Policy) -> list[str]:
    """Changes that are legitimate and should never happen silently.

    Separate from `validate` on purpose. These do not block a save -- an operations
    team raising a limit on purpose should not have to fight the tool -- but they are
    surfaced and written into the change note, because "nobody noticed the ceiling
    moved" is how this kind of system fails.
    """
    if old is None:
        return []
    notes: list[str] = []

    def rose(label: str, before: Money, after: Money) -> None:
        if after > before:
            notes.append(f"{label} raised from {before.to_paypal()} to {after.to_paypal()}")

    rose("hard per-transaction cap", old.hard_per_transaction_cap, new.hard_per_transaction_cap)
    rose("approval threshold", old.approval_threshold, new.approval_threshold)

    old_caps = {e.label: e.cap for e in old.envelopes}
    for envelope in new.envelopes:
        before = old_caps.get(envelope.label)
        if before is not None and envelope.cap > before:
            rose(f"{envelope.label} budget", before, envelope.cap)
    for name in old_caps:
        if name not in {e.label for e in new.envelopes}:
            notes.append(f"the {name} budget was removed")

    dropped_denies = old.denied_categories - new.denied_categories
    if dropped_denies:
        notes.append(
            "no longer refused outright: " + ", ".join(sorted(c.value for c in dropped_denies))
        )
    newly_allowed = new.allowed_categories - old.allowed_categories
    if newly_allowed:
        notes.append("newly allowed: " + ", ".join(sorted(c.value for c in newly_allowed)))

    old_merchants = {m.merchant_id: m for m in old.merchants}
    for merchant in new.merchants:
        before = old_merchants.get(merchant.merchant_id)
        if before is None:
            notes.append(f"new merchant: {merchant.merchant_id}")
        elif not before.enabled and merchant.enabled:
            notes.append(f"{merchant.merchant_id} re-enabled")
        elif (
            before.per_transaction_cap
            and merchant.per_transaction_cap
            and merchant.per_transaction_cap > before.per_transaction_cap
        ):
            rose(
                f"{merchant.merchant_id} cap",
                before.per_transaction_cap,
                merchant.per_transaction_cap,
            )
    if old.velocity_limit and new.velocity_limit > old.velocity_limit:
        notes.append(
            f"velocity limit raised from {old.velocity_limit} to {new.velocity_limit} per window"
        )
    if old.velocity_limit and not new.velocity_limit:
        notes.append("the velocity limit was switched off")
    return notes


class PolicyStore:
    """Versioned rules, in the same database as the holds.

    Takes the `Store` rather than a bare connection, and goes through its
    transaction helper for every statement. That is not indirection for its own
    sake: `Store` opens SQLite with `check_same_thread=False`, which is only safe
    because a single re-entrant lock guards every statement it runs. A second
    object writing to the same connection around that lock would reintroduce
    exactly the corruption the lock exists to prevent.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def save(self, policy: Policy, *, author: str, note: str = "") -> PolicyVersion:
        problems = validate(policy)
        if problems:
            raise PolicyRejected(problems)
        if not author.strip():
            raise PolicyRejected(["every policy change has to say who made it"])

        flagged = warnings(self.current(), policy)
        full_note = note.strip()
        if flagged:
            # Appended rather than stored separately, so the warning travels with
            # the change in every view that shows the note at all.
            joined = "; ".join(flagged)
            full_note = f"{full_note} [{joined}]" if full_note else f"[{joined}]"

        at = datetime.now(UTC)
        with self._store.transaction() as db:
            cur = db.execute(
                "INSERT INTO policies (policy_id, document, author, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    policy.policy_id,
                    json.dumps(enc_policy(policy), sort_keys=True, separators=(",", ":")),
                    author.strip(),
                    full_note,
                    at.isoformat(),
                ),
            )
            version = int(cur.lastrowid)
        return PolicyVersion(version, policy, author.strip(), full_note, at)

    def current(self) -> Policy | None:
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT document FROM policies ORDER BY version DESC LIMIT 1"
            ).fetchone()
        return dec_policy(json.loads(row[0])) if row else None

    def current_version(self) -> PolicyVersion | None:
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT version, document, author, note, created_at FROM policies "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()
        return self._row(row) if row else None

    def at_version(self, version: int) -> PolicyVersion | None:
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT version, document, author, note, created_at FROM policies "
                "WHERE version = ?",
                (version,),
            ).fetchone()
        return self._row(row) if row else None

    def history(self, limit: int = 50) -> list[PolicyVersion]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT version, document, author, note, created_at FROM policies "
                "ORDER BY version DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: Any) -> PolicyVersion:
        return PolicyVersion(
            version=int(row[0]),
            policy=dec_policy(json.loads(row[1])),
            author=row[2],
            note=row[3],
            created_at=datetime.fromisoformat(row[4]),
        )


def seed(store: PolicyStore, policy: Policy, *, author: str = "bootstrap") -> Policy:
    """Install a starting policy if the table is empty, and otherwise leave it alone.

    Called on startup. Deliberately not an upsert: a deployment that reset its rules
    to the defaults on every restart would quietly undo an admin's work, and would do
    it at the moment nobody is watching.
    """
    existing = store.current()
    if existing is not None:
        return existing
    store.save(policy, author=author, note="initial policy")
    return policy
