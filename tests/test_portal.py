"""Signing in, asking for something, and seeing only your own spending.

The security property under test is the boring one and the important one: a
requester cannot approve their own purchase. The approval threshold exists to put a
second person in the loop, and one person wearing both hats is not two people.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from mandate.engine.quote import Category
from mandate.gateway.accounts import Accounts, AuthError, Role, hash_password, verify_password
from mandate.gateway.api import create_app
from mandate.gateway.service import AuthorizationRequest, Gateway
from mandate.gateway.store import Store
from mandate.ledger.records import Ledger
from mandate.policies import demo_policy

from fake_paypal import FakePayPal
from helpers import LEDGER_KEY, MERCHANT_SECRET, quote

GOOD = "a-long-enough-password"


@pytest.fixture
def paypal(now):
    return FakePayPal(now=now)


@pytest.fixture
def gw(tmp_path, paypal):
    store = Store(tmp_path / "state.db")
    g = Gateway(
        store=store,
        ledger=Ledger(tmp_path / "decisions.jsonl", LEDGER_KEY),
        policy=demo_policy(),
        merchant_secret=MERCHANT_SECRET,
        paypal=paypal.client(),
        public_url="https://mandate.test",
    )
    yield g
    store.close()


@pytest.fixture
def client(gw):
    with TestClient(create_app(gw)) as c:
        c.gateway = gw
        c.accounts = Accounts(gw.store)
        c.accounts.create("boss@example.com", GOOD, role=Role.APPROVER)
        c.accounts.create("priya@example.com", GOOD, role=Role.REQUESTER)
        yield c


def as_user(client, username: str):
    r = client.post("/app/login", json={"username": username, "password": GOOD})
    assert r.status_code == 200, r.text
    return r.json()


# -- passwords -------------------------------------------------------------


def test_a_stored_password_does_not_contain_the_password():
    encoded = hash_password("correct-horse-battery")
    assert "correct-horse-battery" not in encoded
    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password("correct-horse-battery", encoded)
    assert not verify_password("correct-horse-batterz", encoded)


def test_two_identical_passwords_hash_differently():
    """Per-user salt. Without it, identical passwords are visibly identical in the
    table, and one cracked hash cracks every account that shares it."""
    assert hash_password("same-password-here") != hash_password("same-password-here")


def test_a_short_password_is_refused():
    with pytest.raises(ValueError, match="at least 8"):
        hash_password("short")


def test_a_corrupt_hash_fails_closed():
    for broken in ("", "nonsense", "pbkdf2_sha256$notanumber$aa$bb", "md5$1$aa$bb"):
        assert verify_password("anything", broken) is False


# -- sessions --------------------------------------------------------------


def test_the_same_message_for_unknown_user_and_wrong_password(gw):
    """Telling them apart tells an attacker which usernames exist."""
    accounts = Accounts(gw.store)
    accounts.create("real@example.com", GOOD)
    with pytest.raises(AuthError) as wrong:
        accounts.login("real@example.com", "not-the-password")
    with pytest.raises(AuthError) as unknown:
        accounts.login("ghost@example.com", "not-the-password")
    assert str(wrong.value) == str(unknown.value)


def test_logging_out_kills_the_session_server_side(gw):
    """Clearing the cookie alone would leave a token that still works for whoever
    copied it -- the failure signed-cookie sessions cannot fix."""
    accounts = Accounts(gw.store)
    accounts.create("a@example.com", GOOD)
    token = accounts.login("a@example.com", GOOD)
    assert accounts.whoami(token).username == "a@example.com"
    accounts.logout(token)
    assert accounts.whoami(token) is None


def test_an_expired_session_does_not_resolve(gw):
    from datetime import timedelta

    accounts = Accounts(gw.store)
    accounts.create("a@example.com", GOOD)
    token = accounts.login("a@example.com", GOOD, ttl=timedelta(seconds=-1))
    assert accounts.whoami(token) is None


def test_every_session_for_an_account_can_be_revoked(gw):
    """The reason sessions are server-side at all."""
    accounts = Accounts(gw.store)
    accounts.create("a@example.com", GOOD)
    tokens = [accounts.login("a@example.com", GOOD) for _ in range(3)]
    accounts.revoke_all("a@example.com")
    assert all(accounts.whoami(t) is None for t in tokens)


def test_the_session_table_cannot_be_used_to_log_in(gw):
    """Only the digest is stored."""
    accounts = Accounts(gw.store)
    accounts.create("a@example.com", GOOD)
    token = accounts.login("a@example.com", GOOD)
    with gw.store.transaction() as db:
        stored = [r[0] for r in db.execute("SELECT token_sha256 FROM sessions").fetchall()]
    assert token not in stored
    assert all(accounts.whoami(s) is None for s in stored)


# -- the gate --------------------------------------------------------------


def test_the_operator_surface_needs_a_session(client):
    for path in ("/v1/ops/policy", "/v1/ops/approvals", "/v1/ops/holds", "/v1/ops/admin"):
        assert client.get(path).status_code == 401, path


def test_a_requester_is_refused_the_operator_surface(client):
    as_user(client, "priya@example.com")
    for path in ("/v1/ops/policy", "/v1/ops/approvals", "/v1/ops/admin"):
        assert client.get(path).status_code == 403, path


def test_a_requester_cannot_approve_anything(client):
    """The property the whole role split exists for."""
    as_user(client, "priya@example.com")
    r = client.post("/v1/ops/approvals/dec_x/approve", json={"approver": "priya@example.com"})
    assert r.status_code == 403


def test_a_requester_cannot_edit_the_rules(client):
    as_user(client, "priya@example.com")
    assert client.put("/v1/ops/policy", json={"policy": {}, "author": "p"}).status_code == 403


def test_an_approver_reaches_both(client):
    as_user(client, "boss@example.com")
    assert client.get("/v1/ops/policy").status_code == 200
    assert client.get("/app/requests").status_code == 200


def test_the_portal_redirects_rather_than_401s(client):
    """A browser address bar gets sent somewhere it can act; a fetch() gets JSON."""
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]
    assert client.get("/app/requests").status_code == 401


def test_the_session_cookie_is_not_readable_by_javascript(client):
    r = client.post("/app/login", json={"username": "priya@example.com", "password": GOOD})
    cookie = r.headers["set-cookie"].lower()
    # An XSS that can read the session cookie is an XSS that is a session.
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


# -- seeing only your own --------------------------------------------------


async def ask_for(gw, username: str, **kw):
    """These routes read the wall clock, so quotes are stamped with real time --
    the shared helper's fixed timestamp would be refused as stale."""
    kw.setdefault("at", datetime.now(UTC))
    return await gw.request_authorization(
        AuthorizationRequest(quote=quote(**kw), reason="a thing", agent_id=username)
    )


async def test_a_requester_sees_only_their_own_requests(client, gw):
    await ask_for(gw, "priya@example.com")
    await ask_for(gw, "someone.else@example.com")
    await ask_for(gw, "someone.else@example.com")

    as_user(client, "priya@example.com")
    mine = client.get("/app/requests").json()["requests"]
    assert len(mine) == 1
    # Filtered in SQL, not in the page: a view that fetches everything and hides
    # most of it is one careless edit away from showing someone else's spending.
    assert all(r["asked_for"] == "a thing" for r in mine)


async def test_a_requesters_approval_queue_is_empty_by_construction(client, gw):
    await gw.request_authorization(
        AuthorizationRequest(
            quote=quote(
                merchant_id="m_cloudspend",
                items=[("SKU-GPU", "GPU hour", Category.COMPUTE, "180.00", 1)],
                at=datetime.now(UTC),
            ),
            reason="training",
            agent_id="priya@example.com",
        )
    )
    as_user(client, "priya@example.com")
    body = client.get("/app/approvals").json()
    assert body["can_approve"] is False
    # Her own over-threshold request is waiting, and she is not the one who decides.
    assert body["approvals"] == []

    as_user(client, "boss@example.com")
    assert len(client.get("/app/approvals").json()["approvals"]) == 1


def test_states_are_explained_in_words_a_person_would_use():
    from mandate.gateway.portal import STATUS

    # "awaiting_buyer" tells a requester nothing about what to do next.
    assert STATUS["awaiting_buyer"][0] == "Ready to pay"
    assert STATUS["refused"][0] == "Refused"
    assert "Nothing was charged" in STATUS["refused"][1]
    assert "released" in STATUS["held"][1].lower()


# -- bootstrap -------------------------------------------------------------


def test_no_account_exists_unless_one_is_configured(tmp_path, paypal, monkeypatch):
    """An application that ships with a working username and password ships with a
    working username and password for everybody."""
    monkeypatch.delenv("MANDATE_ADMIN_USER", raising=False)
    monkeypatch.delenv("MANDATE_ADMIN_PASSWORD", raising=False)
    store = Store(tmp_path / "s.db")
    g = Gateway(
        store=store,
        ledger=Ledger(tmp_path / "d.jsonl", LEDGER_KEY),
        policy=demo_policy(),
        merchant_secret=MERCHANT_SECRET,
        paypal=paypal.client(),
    )
    with TestClient(create_app(g)):
        assert Accounts(store).count() == 0
    store.close()


def test_the_bootstrap_admin_is_not_recreated_after_a_password_change(gw, monkeypatch):
    """A restart must not quietly undo a password change."""
    monkeypatch.setenv("MANDATE_ADMIN_USER", "root@example.com")
    monkeypatch.setenv("MANDATE_ADMIN_PASSWORD", GOOD)
    accounts = Accounts(gw.store)
    with TestClient(create_app(gw)):
        assert accounts.get("root@example.com") is not None
    accounts.set_password("root@example.com", "a-different-password")
    with TestClient(create_app(gw)):
        pass
    assert accounts.login("root@example.com", "a-different-password")
    with pytest.raises(AuthError):
        accounts.login("root@example.com", GOOD)


# -- the shortlist ---------------------------------------------------------


class FakeBackend:
    model = "fake-model"
    provider = "fake"

    def __init__(self, text: str, raises: Exception | None = None):
        self.text = text
        self.raises = raises

    async def complete(self, *, system, turns, tools):
        if self.raises:
            raise self.raises
        return type("C", (), {"text": self.text, "calls": [], "usage": None})()


CATALOG = [
    {"sku": "SKU-A", "name": "Navy dress", "description": "midi", "unit_price": "89.00"},
    {"sku": "SKU-B", "name": "Black shift dress", "description": "knee length", "unit_price": "72.00"},
]


async def test_a_model_naming_a_product_that_does_not_exist_is_dropped(monkeypatch):
    """The ordinary failure, not an exotic one. It never reaches a screen."""
    from mandate import shopping

    async def fake_catalog(url, mid):
        return CATALOG

    monkeypatch.setattr(shopping, "catalog", fake_catalog)
    backend = FakeBackend(
        '{"options": [{"sku": "SKU-A", "why": "fits"}, '
        '{"sku": "SKU-INVENTED", "why": "does not exist"}]}'
    )
    options, how = await shopping.propose("a dress", merchant_url="x", backend=backend)
    assert [o.sku for o in options] == ["SKU-A"]
    assert "do not exist" in how


async def test_the_price_comes_from_the_merchant_not_the_model(monkeypatch):
    """A hallucinated figure cannot reach a screen, let alone a payment."""
    from mandate import shopping

    async def fake_catalog(url, mid):
        return CATALOG

    monkeypatch.setattr(shopping, "catalog", fake_catalog)
    backend = FakeBackend('{"options": [{"sku": "SKU-B", "why": "cheap", "price": "1.00"}]}')
    options, _ = await shopping.propose("a dress", merchant_url="x", backend=backend)
    assert options[0].price.to_paypal() == "72.00"


async def test_a_model_that_is_down_degrades_instead_of_erroring(monkeypatch):
    """A person who asked for a dress should get a worse list, not an error page."""
    from mandate import shopping

    async def fake_catalog(url, mid):
        return CATALOG

    monkeypatch.setattr(shopping, "catalog", fake_catalog)
    backend = FakeBackend("", raises=RuntimeError("credit balance is too low"))
    options, how = await shopping.propose("dress", merchant_url="x", backend=backend)
    assert len(options) == 2
    # Said out loud, and actionable: "RuntimeError" alone sends an operator
    # looking for a network problem when the account is simply out of credit.
    assert "credit balance is too low" in how


async def test_nonsense_from_the_model_falls_back_rather_than_showing_nothing(monkeypatch):
    from mandate import shopping

    async def fake_catalog(url, mid):
        return CATALOG

    monkeypatch.setattr(shopping, "catalog", fake_catalog)
    options, how = await shopping.propose(
        "dress", merchant_url="x", backend=FakeBackend("I am afraid I cannot do that")
    )
    assert len(options) == 2
    assert "keyword" in how


async def test_the_shortlist_is_capped_like_everything_else(monkeypatch, tmp_path):
    """The portal is the one place a model can be invoked on every page load.
    Without a cap that is how a budget disappears with nobody deciding to spend it."""
    from mandate import shopping
    from mandate.agent.budget import SpendLedger

    async def fake_catalog(url, mid):
        return CATALOG

    monkeypatch.setattr(shopping, "catalog", fake_catalog)
    spent = SpendLedger(path=tmp_path / "spend.jsonl", cap_usd=0.0)
    backend = FakeBackend('{"options": [{"sku": "SKU-A", "why": "no"}]}')
    options, how = await shopping.propose(
        "dress", merchant_url="x", backend=backend, spend=spent
    )
    # Falls back rather than erroring, and says why.
    assert len(options) == 2
    assert "cap reached" in how


async def test_what_the_shortlist_cost_is_recorded(monkeypatch, tmp_path):
    from mandate import shopping
    from mandate.agent.budget import SpendLedger

    async def fake_catalog(url, mid):
        return CATALOG

    monkeypatch.setattr(shopping, "catalog", fake_catalog)

    class Metered(FakeBackend):
        model = "claude-haiku-4-5"

        async def complete(self, *, system, turns, tools):
            return type(
                "C",
                (),
                {
                    "text": self.text,
                    "calls": [],
                    "usage": {"input_tokens": 1000, "output_tokens": 200},
                },
            )()

    spent = SpendLedger(path=tmp_path / "spend.jsonl", cap_usd=5.0)
    await shopping.propose(
        "dress",
        merchant_url="x",
        backend=Metered('{"options": [{"sku": "SKU-A", "why": "fits"}]}'),
        spend=spent,
    )
    assert spent.calls == 1
    # 1000 in at $1/MTok + 200 out at $5/MTok.
    assert spent.spent_usd == pytest.approx(0.002, abs=1e-6)
