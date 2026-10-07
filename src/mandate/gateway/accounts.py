"""Who is asking, and what they are allowed to do.

Two roles, because the project only has two kinds of person in it.

A **requester** asks for things and sees what happened to their own requests. They
cannot approve their own spending, edit the rules, capture money or void a hold --
not by convention but because the routes that do those things are not reachable
with their session. A requester who could approve their own request would make the
approval threshold decorative in exactly the way an agent approving its own request
would.

An **approver** can do everything a requester can, plus decide the queue and edit the
policy.

Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user salt and a deliberately
large iteration count, verified with a constant-time comparison. Not bcrypt or argon2
only because those are dependencies and this is stdlib; the parameters here are chosen
to be defensible rather than fast. The plaintext is never stored, never logged, and
never returned by any route.

Sessions are opaque random tokens kept server-side, not signed cookies carrying
claims. The difference matters on the one axis that counts here: a server-side
session can be revoked. A signed cookie asserting `role=approver` stays valid until
it expires no matter what the operator does, and "we cannot lock out a compromised
account until Tuesday" is not an acceptable property for the thing that approves
payments.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

#: Cost parameter. High enough to hurt an offline attacker, low enough that a login
#: is not noticeable. Stored per hash so it can be raised later without invalidating
#: every existing password.
ITERATIONS = 600_000
SESSION_TTL = timedelta(hours=12)
COOKIE = "mandate_session"


class Role(StrEnum):
    REQUESTER = "requester"
    APPROVER = "approver"


class AuthError(RuntimeError):
    """Wrong credentials, or none. Deliberately one exception for both.

    Distinguishing "no such user" from "wrong password" tells an attacker which
    usernames exist, and the only person helped by that distinction is them.
    """


@dataclass(frozen=True)
class User:
    username: str
    role: Role
    created_at: datetime

    @property
    def can_approve(self) -> bool:
        return self.role is Role.APPROVER


def hash_password(password: str, *, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    """`pbkdf2_sha256$iterations$salt$hash`, which carries its own parameters.

    Stored with the cost in it so the cost can be raised later: an existing hash
    still verifies under the number it was made with, and is rewritten on the next
    successful login.
    """
    if len(password) < 8:
        raise ValueError("a password needs at least 8 characters")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, expected = encoded.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except ValueError:
        return False
    # Constant time: a comparison that returns early leaks how much of the hash
    # matched, one byte at a time.
    return hmac.compare_digest(digest.hex(), expected)


class Accounts:
    """Users and sessions, in the same database and under the same lock."""

    def __init__(self, store: Any) -> None:
        self._store = store

    # -- users -----------------------------------------------------------

    def create(self, username: str, password: str, *, role: Role = Role.REQUESTER) -> User:
        name = username.strip().lower()
        if not name:
            raise ValueError("a username is required")
        encoded = hash_password(password)
        at = datetime.now(UTC)
        with self._store.transaction() as db:
            existing = db.execute(
                "SELECT 1 FROM users WHERE username = ?", (name,)
            ).fetchone()
            if existing:
                raise ValueError(f"{name} already exists")
            db.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
                (name, encoded, role.value, at.isoformat()),
            )
        return User(name, role, at)

    def set_password(self, username: str, password: str) -> None:
        encoded = hash_password(password)
        with self._store.transaction() as db:
            db.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (encoded, username.strip().lower()),
            )

    def get(self, username: str) -> User | None:
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT username, role, created_at FROM users WHERE username = ?",
                (username.strip().lower(),),
            ).fetchone()
        return self._user(row) if row else None

    def list(self) -> list[User]:
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT username, role, created_at FROM users ORDER BY username"
            ).fetchall()
        return [self._user(r) for r in rows]

    def count(self) -> int:
        with self._store.transaction() as db:
            return int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    # -- sessions --------------------------------------------------------

    def login(self, username: str, password: str, *, ttl: timedelta = SESSION_TTL) -> str:
        """Returns an opaque session token, or raises. Never says which half was wrong."""
        name = username.strip().lower()
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT password_hash FROM users WHERE username = ?", (name,)
            ).fetchone()
        # The hash is computed either way, so a missing user and a wrong password
        # take the same time. Without this, response timing is a user enumeration
        # oracle for anyone with a stopwatch.
        encoded = row[0] if row else hash_password("not-a-real-password")
        if not verify_password(password, encoded) or row is None:
            raise AuthError("that username and password do not match")

        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        with self._store.transaction() as db:
            db.execute(
                "INSERT INTO sessions (token_sha256, username, created_at, expires_at) "
                "VALUES (?,?,?,?)",
                (_digest(token), name, now.isoformat(), (now + ttl).isoformat()),
            )
        return token

    def whoami(self, token: str | None) -> User | None:
        """Resolve a session. None for absent, unknown or expired."""
        if not token:
            return None
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT u.username, u.role, u.created_at, s.expires_at "
                "FROM sessions s JOIN users u ON u.username = s.username "
                "WHERE s.token_sha256 = ?",
                (_digest(token),),
            ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row[3]) <= datetime.now(UTC):
            return None
        return self._user(row)

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._store.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token_sha256 = ?", (_digest(token),))

    def revoke_all(self, username: str) -> None:
        """Every session for one account. The reason sessions are server-side."""
        with self._store.transaction() as db:
            db.execute("DELETE FROM sessions WHERE username = ?", (username.strip().lower(),))

    def purge_expired(self, *, now: datetime | None = None) -> int:
        moment = (now or datetime.now(UTC)).isoformat()
        with self._store.transaction() as db:
            cur = db.execute("DELETE FROM sessions WHERE expires_at <= ?", (moment,))
            return cur.rowcount or 0

    @staticmethod
    def _user(row: Any) -> User:
        return User(row[0], Role(row[1]), datetime.fromisoformat(row[2]))


def _digest(token: str) -> str:
    """Only the digest is stored, so the session table cannot be used to log in."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
