"""Mutable operational state, in SQLite, with explicit SQL.

The division of labour matters. The **ledger** is the immutable record: signed,
append-only, replayable, and the thing you would show someone who asked what the
policy decided and why. The **store** is working state: which holds are open,
which PayPal authorization belongs to which decision, what is waiting on a human.
It has to be updatable, so it is kept deliberately small and every row in it
traces back to a ledger record by `decision_id`.

Hand-written SQL rather than an ORM, because there are two tables and because a
reviewer should be able to see exactly what is written when money moves.

Every state change goes through `transition`, which checks the move against
`state.TRANSITIONS` and appends to `hold_events` in the same SQLite transaction.
A state change that is not recorded, or a record without the state change, would
both be worse than failing.

Approval tokens are stored as SHA-256 digests. The token itself goes out by SMS
and is never written down here, so a reader of this database cannot approve
anything.

**Threading.** One connection, opened with `check_same_thread=False` and guarded
by a re-entrant lock that every statement in this module takes. This matters
because FastAPI runs synchronous route handlers in a worker threadpool, so
without it the first concurrent request raises `SQLite objects created in a
thread can only be used in that same thread` -- in production, not just in
tests. A connection pool would be the alternative, but SQLite serialises writes
regardless and the lock makes the serialisation explicit rather than emergent.
Nothing outside this class touches `_connection`.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..engine.money import Money
from ..engine.policy import LedgerWindow, PriorAuthorization
from ..engine.quote import Category, MerchantQuote
from .state import HoldState, check_transition, consumes_budget, counts_as_placement

SCHEMA = """
CREATE TABLE IF NOT EXISTS holds (
    decision_id               TEXT PRIMARY KEY,
    state                     TEXT NOT NULL,
    policy_id                 TEXT NOT NULL,
    merchant_id               TEXT NOT NULL,
    merchant_name             TEXT NOT NULL,
    quote_id                  TEXT NOT NULL,
    currency                  TEXT NOT NULL,
    amount_minor              INTEGER NOT NULL,
    fingerprint               TEXT NOT NULL,
    categories                TEXT NOT NULL,
    engine_outcome            TEXT NOT NULL,
    paypal_order_id           TEXT,
    authorization_id          TEXT,
    capture_id                TEXT,
    captured_minor            INTEGER,
    approval_url              TEXT,
    authorization_expires_at  TEXT,
    approval_token_sha256     TEXT,
    approval_token_expires_at TEXT,
    approved_by               TEXT,
    requested_at              TEXT NOT NULL,
    placed_at                 TEXT,
    updated_at                TEXT NOT NULL,
    last_error                TEXT
);
CREATE INDEX IF NOT EXISTS holds_by_state ON holds(state);
CREATE INDEX IF NOT EXISTS holds_by_requested_at ON holds(requested_at);
CREATE INDEX IF NOT EXISTS holds_by_token ON holds(approval_token_sha256);

CREATE TABLE IF NOT EXISTS hold_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL REFERENCES holds(decision_id),
    at          TEXT NOT NULL,
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS hold_events_by_decision ON hold_events(decision_id);

-- Webhook deliveries, so a replayed delivery cannot be processed twice. PayPal
-- retries, and a retried PAYMENT.CAPTURE.COMPLETED must not look like a second
-- capture.
CREATE TABLE IF NOT EXISTS seen_webhooks (
    event_id   TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    seen_at    TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Hold:
    decision_id: str
    state: HoldState
    policy_id: str
    merchant_id: str
    merchant_name: str
    quote_id: str
    amount: Money
    fingerprint: str
    categories: frozenset[Category]
    engine_outcome: str
    requested_at: datetime
    updated_at: datetime
    paypal_order_id: str | None = None
    authorization_id: str | None = None
    capture_id: str | None = None
    captured: Money | None = None
    approval_url: str | None = None
    authorization_expires_at: datetime | None = None
    approval_token_expires_at: datetime | None = None
    approved_by: str | None = None
    placed_at: datetime | None = None
    last_error: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state in {
            HoldState.AWAITING_HUMAN,
            HoldState.AWAITING_BUYER,
            HoldState.HELD,
        }


class UnknownHold(KeyError):
    pass


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is only safe because `_lock` below guards every
        # statement; see the module docstring.
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, params).fetchall()

    def _query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(sql, params).fetchone()

    # -- writes ----------------------------------------------------------

    def create(
        self,
        *,
        decision_id: str,
        quote: MerchantQuote,
        policy_id: str,
        engine_outcome: str,
        state: HoldState,
        at: datetime | None = None,
        detail: str = "",
    ) -> Hold:
        request = quote.to_policy_input()
        moment = at or _now()
        with self._tx() as db:
            db.execute(
                """INSERT INTO holds (
                       decision_id, state, policy_id, merchant_id, merchant_name, quote_id,
                       currency, amount_minor, fingerprint, categories, engine_outcome,
                       requested_at, updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    state.value,
                    policy_id,
                    quote.merchant_id,
                    quote.merchant_name,
                    quote.quote_id,
                    quote.currency,
                    request.amount.minor,
                    request.fingerprint,
                    ",".join(sorted(c.value for c in request.categories)),
                    engine_outcome,
                    _iso(moment),
                    _iso(moment),
                ),
            )
            db.execute(
                "INSERT INTO hold_events (decision_id, at, from_state, to_state, detail) "
                "VALUES (?,?,?,?,?)",
                (decision_id, _iso(moment), None, state.value, detail),
            )
        return self.get(decision_id)

    def transition(
        self,
        decision_id: str,
        target: HoldState,
        *,
        detail: str = "",
        at: datetime | None = None,
        **columns: object,
    ) -> Hold:
        """Move a hold, recording the move. Illegal transitions raise.

        `columns` sets any of the PayPal-side fields in the same transaction, so
        there is never a moment where the state says HELD and the authorization
        id is still null.
        """
        allowed = {
            "paypal_order_id",
            "authorization_id",
            "capture_id",
            "captured_minor",
            "approval_url",
            "authorization_expires_at",
            "approval_token_sha256",
            "approval_token_expires_at",
            "approved_by",
            "placed_at",
            "last_error",
        }
        unknown = set(columns) - allowed
        if unknown:
            raise ValueError(f"not columns on holds: {sorted(unknown)}")

        moment = at or _now()
        current = self.get(decision_id)
        check_transition(current.state, target)

        sets = ["state = ?", "updated_at = ?"]
        values: list[object] = [target.value, _iso(moment)]
        for name, value in columns.items():
            sets.append(f"{name} = ?")
            values.append(_iso(value) if isinstance(value, datetime) else value)
        values.append(decision_id)

        with self._tx() as db:
            # S608: `sets` holds only column names already checked against `allowed`
            # above, and every value is a bound parameter. No caller string reaches
            # the SQL text.
            db.execute(
                f"UPDATE holds SET {', '.join(sets)} WHERE decision_id = ?", values  # noqa: S608 - `sets` holds only column names checked against `allowed`; values are bound
            )
            db.execute(
                "INSERT INTO hold_events (decision_id, at, from_state, to_state, detail) "
                "VALUES (?,?,?,?,?)",
                (decision_id, _iso(moment), current.state.value, target.value, detail),
            )
        return self.get(decision_id)

    def note(self, decision_id: str, **columns: object) -> Hold:
        """Record something about a hold without moving it.

        Separate from `transition` on purpose. Every state change goes through the
        state machine and lands in hold_events; this writes a field and does not,
        because "the approval SMS bounced" is a fact about a hold, not a change in
        what the hold *is*. Folding it into transition would mean inventing a
        self-transition and filling the event history with non-events.
        """
        allowed = {"last_error", "approved_by"}
        unknown = set(columns) - allowed
        if unknown:
            raise ValueError(f"note() will not write {sorted(unknown)}")
        if not columns:
            return self.get(decision_id)
        self.get(decision_id)  # raises UnknownHold rather than silently updating nothing
        sets = ", ".join(f"{name} = ?" for name in columns)
        with self._tx() as db:
            db.execute(
                f"UPDATE holds SET {sets}, updated_at = ? WHERE decision_id = ?",  # noqa: S608 - column names come from `allowed`; values are bound
                (*columns.values(), _iso(_now()), decision_id),
            )
        return self.get(decision_id)

    def issue_approval_token(
        self, decision_id: str, *, ttl: timedelta = timedelta(minutes=15), at: datetime | None = None
    ) -> str:
        """Mint a single-use token for the SMS link.

        Returned once, to the caller that is about to send the SMS. Only its
        digest is stored, so this database cannot be used to approve anything.
        """
        token = secrets.token_urlsafe(32)
        expires = (at or _now()) + ttl
        with self._tx() as db:
            db.execute(
                "UPDATE holds SET approval_token_sha256 = ?, approval_token_expires_at = ?, "
                "updated_at = ? WHERE decision_id = ?",
                (token_digest(token), _iso(expires), _iso(at or _now()), decision_id),
            )
        return token

    def peek_approval_token(self, token: str) -> sqlite3.Row | None:
        """Resolve a token without burning it.

        The approval page needs to render what is being asked before the approver
        decides. A GET that consumed the token would let a link preview or a mail
        scanner silently destroy an approval request.
        """
        return self._query_one(
            "SELECT decision_id, approval_token_expires_at FROM holds "
            "WHERE approval_token_sha256 = ?",
            (token_digest(token),),
        )

    def consume_approval_token(self, token: str, *, at: datetime | None = None) -> Hold | None:
        """Resolve a token to its hold and burn it. None if unknown or expired.

        The token is cleared whether the approver says yes or no, so a link
        cannot be replayed and an intercepted SMS is useful exactly once.
        """
        moment = at or _now()
        row = self.peek_approval_token(token)
        if row is None:
            return None
        expires = _dt(row["approval_token_expires_at"])
        if expires is None or expires < moment:
            return None
        hold = self.get(row["decision_id"])
        if hold.state is not HoldState.AWAITING_HUMAN:
            return None
        with self._tx() as db:
            db.execute(
                "UPDATE holds SET approval_token_sha256 = NULL, approval_token_expires_at = NULL "
                "WHERE decision_id = ?",
                (hold.decision_id,),
            )
        return hold

    def remember_webhook(self, event_id: str, event_type: str, *, at: datetime | None = None) -> bool:
        """True the first time an event id is seen, False on every replay.

        PayPal retries deliveries. A retried PAYMENT.CAPTURE.COMPLETED that is
        processed twice would look like a second capture.
        """
        try:
            with self._tx() as db:
                db.execute(
                    "INSERT INTO seen_webhooks (event_id, event_type, seen_at) VALUES (?,?,?)",
                    (event_id, event_type, _iso(at or _now())),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    # -- reads -----------------------------------------------------------

    def get(self, decision_id: str) -> Hold:
        row = self._query_one("SELECT * FROM holds WHERE decision_id = ?", (decision_id,))
        if row is None:
            raise UnknownHold(decision_id)
        return _hold(row)

    def find_by_authorization(self, authorization_id: str) -> Hold | None:
        row = self._query_one(
            "SELECT * FROM holds WHERE authorization_id = ?", (authorization_id,)
        )
        return _hold(row) if row else None

    def find_by_order(self, order_id: str) -> Hold | None:
        row = self._query_one("SELECT * FROM holds WHERE paypal_order_id = ?", (order_id,))
        return _hold(row) if row else None

    def list(self, *, states: frozenset[HoldState] | None = None, limit: int = 500) -> list[Hold]:
        if states:
            marks = ",".join("?" * len(states))
            rows = self._query(
                f"SELECT * FROM holds WHERE state IN ({marks}) "  # noqa: S608 - `marks` is only `?` placeholders
                "ORDER BY requested_at DESC LIMIT ?",
                (*[s.value for s in states], limit),
            )
        else:
            rows = self._query(
                "SELECT * FROM holds ORDER BY requested_at DESC LIMIT ?", (limit,)
            )
        return [_hold(r) for r in rows]

    def events(self, decision_id: str) -> list[dict[str, object]]:
        rows = self._query(
            "SELECT at, from_state, to_state, detail FROM hold_events "
            "WHERE decision_id = ? ORDER BY id",
            (decision_id,),
        )
        return [dict(r) for r in rows]

    def expiring_before(self, cutoff: datetime) -> list[Hold]:
        """Holds whose authorization lapses before `cutoff`.

        Drives the job that reauthorizes or voids rather than letting a hold sit
        on a buyer's funds until PayPal quietly drops it.
        """
        rows = self._query(
            "SELECT * FROM holds WHERE state = ? AND authorization_expires_at IS NOT NULL "
            "AND authorization_expires_at <= ? ORDER BY authorization_expires_at",
            (HoldState.HELD.value, _iso(cutoff)),
        )
        return [_hold(r) for r in rows]

    # -- the window the engine needs -------------------------------------

    def ledger_window(self, *, since: datetime, currency: str | None = None) -> LedgerWindow:
        """Build the engine's view of prior authorizations.

        `reserved` is derived from the hold's state rather than stored, so it
        cannot drift: voiding a hold releases its envelope slot by virtue of the
        state change alone, with nothing else to remember to update.
        """
        rows = self._query(
            "SELECT state, currency, amount_minor, merchant_id, fingerprint, categories, "
            "       requested_at, placed_at "
            "FROM holds WHERE requested_at >= ? ORDER BY requested_at",
            (_iso(since),),
        )
        entries: list[PriorAuthorization] = []
        for row in rows:
            state = HoldState(row["state"])
            if not counts_as_placement(state):
                # Refused, waiting on a human, or declined: no order was ever
                # created, so this consumed nothing and reached for nothing.
                continue
            if currency and row["currency"] != currency:
                continue
            entries.append(
                PriorAuthorization(
                    at=_dt(row["placed_at"]) or _dt(row["requested_at"]),
                    merchant_id=row["merchant_id"],
                    amount=Money(int(row["amount_minor"]), row["currency"]),
                    fingerprint=row["fingerprint"],
                    categories=frozenset(
                        Category(c) for c in row["categories"].split(",") if c
                    ),
                    reserved=consumes_budget(state),
                )
            )
        return LedgerWindow(tuple(entries))


def _hold(row: sqlite3.Row) -> Hold:
    captured_minor = row["captured_minor"]
    return Hold(
        decision_id=row["decision_id"],
        state=HoldState(row["state"]),
        policy_id=row["policy_id"],
        merchant_id=row["merchant_id"],
        merchant_name=row["merchant_name"],
        quote_id=row["quote_id"],
        amount=Money(int(row["amount_minor"]), row["currency"]),
        fingerprint=row["fingerprint"],
        categories=frozenset(Category(c) for c in row["categories"].split(",") if c),
        engine_outcome=row["engine_outcome"],
        requested_at=_dt(row["requested_at"]),
        updated_at=_dt(row["updated_at"]),
        paypal_order_id=row["paypal_order_id"],
        authorization_id=row["authorization_id"],
        capture_id=row["capture_id"],
        captured=(
            Money(int(captured_minor), row["currency"]) if captured_minor is not None else None
        ),
        approval_url=row["approval_url"],
        authorization_expires_at=_dt(row["authorization_expires_at"]),
        approval_token_expires_at=_dt(row["approval_token_expires_at"]),
        approved_by=row["approved_by"],
        placed_at=_dt(row["placed_at"]),
        last_error=row["last_error"],
    )
