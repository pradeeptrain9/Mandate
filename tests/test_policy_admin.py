"""Editable rules, and the one property that makes editing them safe.

Everything else in this project decides *within* the rules. This decides what the
rules are, which makes it the most dangerous surface here: a mistake does not
produce a wrong decision, it produces a wrong decision procedure.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from mandate.engine.money import Money
from mandate.engine.quote import Category
from mandate.gateway.api import create_app
from mandate.gateway.policy_store import PolicyRejected, PolicyStore, seed, validate, warnings
from mandate.gateway.service import AuthorizationRequest, Gateway
from mandate.gateway.store import Store
from mandate.ledger.codec import enc_policy
from mandate.ledger.records import Ledger
from mandate.policies import demo_policy

from fake_paypal import FakePayPal
from helpers import LEDGER_KEY, MERCHANT_SECRET, quote, sign_in

usd = lambda v: Money.from_paypal(v, "USD")  # noqa: E731


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
    g.policies = PolicyStore(store)
    seed(g.policies, demo_policy())
    yield g
    store.close()


@pytest.fixture
def client(gw):
    with TestClient(create_app(gw)) as c:
        c.gateway = gw
        sign_in(c)
        yield c


# -- the property that makes this safe at all ------------------------------


async def test_a_decision_still_replays_after_the_rules_change(gw, now):
    """The whole reason editable rules are not a catastrophe.

    A record carries the policy it ran against, not a reference to one. Edit the
    policy afterwards and the old decision must still reproduce its own answer --
    otherwise "the ledger replays" would quietly mean "replays until someone
    changes a cap".
    """
    result = await gw.request_authorization(
        AuthorizationRequest(quote=quote(), reason="paper", agent_id="a"), now=now
    )
    record = next(r for r in gw.ledger if r.decision_id == result.decision_id)

    # Both, because a hard cap below the approval threshold is itself refused --
    # which is the validation doing its job on a policy this test wrote carelessly.
    gw.policies.save(
        replace(
            demo_policy(),
            hard_per_transaction_cap=usd("10.00"),
            approval_threshold=usd("5.00"),
        ),
        author="admin@example.com",
        note="much stricter",
    )
    assert gw.policy.hard_per_transaction_cap == usd("10.00")

    # Same answer, under the rules that produced it.
    record.assert_replays()
    assert record.policy.hard_per_transaction_cap == usd("500.00")


async def test_an_edit_takes_effect_on_the_very_next_decision(gw, now):
    first = await gw.request_authorization(
        AuthorizationRequest(quote=quote(), reason="paper", agent_id="a"), now=now
    )
    assert first.outcome.value == "allow"  # $34 under a $100 threshold

    gw.policies.save(
        replace(demo_policy(), approval_threshold=usd("5.00")),
        author="admin@example.com",
        note="tighten unattended spending",
    )

    later = await gw.request_authorization(
        AuthorizationRequest(quote=quote(), reason="paper", agent_id="a"), now=now
    )
    # No restart, no redeploy.
    assert later.outcome.value == "hold_for_approval"
    assert "approval_threshold" in later.evaluation.reason_ids


# -- refusing a policy that would not work ---------------------------------


def test_a_threshold_above_the_hard_cap_is_refused():
    problems = validate(replace(demo_policy(), approval_threshold=usd("9999.00")))
    assert any("no purchase is ever small enough to ask a human" in p for p in problems)


def test_a_policy_in_a_currency_it_does_not_allow_is_refused():
    bad = replace(demo_policy(), allowed_currencies=frozenset({"EUR"}))
    assert any("every request would be refused" in p for p in validate(bad))


def test_a_category_both_allowed_and_denied_is_refused():
    bad = replace(demo_policy(), denied_categories=frozenset({Category.OFFICE_SUPPLIES}))
    assert any("both allowed and denied" in p for p in validate(bad))


def test_every_problem_is_reported_not_just_the_first(gw):
    """An admin fixing one field at a time, reloading between each, stops reading."""
    bad = replace(
        demo_policy(),
        policy_id="  ",
        hard_per_transaction_cap=usd("0.00"),
        velocity_limit=-1,
    )
    assert len(validate(bad)) >= 3


def test_an_anonymous_change_is_refused(gw):
    with pytest.raises(PolicyRejected, match="who made it"):
        gw.policies.save(demo_policy(), author="   ")


# -- naming the dangerous but legitimate -----------------------------------


def test_raising_a_ceiling_is_allowed_and_recorded():
    loosened = replace(demo_policy(), hard_per_transaction_cap=usd("5000.00"))
    notes = warnings(demo_policy(), loosened)
    assert any("raised from 500.00 to 5000.00" in n for n in notes)


def test_dropping_a_denied_category_is_named():
    notes = warnings(demo_policy(), replace(demo_policy(), denied_categories=frozenset()))
    assert any("no longer refused outright" in n and "gift_card" in n for n in notes)


def test_switching_off_the_velocity_limit_is_named():
    notes = warnings(demo_policy(), replace(demo_policy(), velocity_limit=0))
    assert any("velocity limit was switched off" in n for n in notes)


def test_the_warning_travels_with_the_change(gw):
    """Stored in the note, so it shows up in every view that shows notes at all."""
    saved = gw.policies.save(
        replace(demo_policy(), hard_per_transaction_cap=usd("5000.00")),
        author="admin@example.com",
        note="Q4 capex",
    )
    assert "Q4 capex" in saved.note
    assert "raised from 500.00 to 5000.00" in saved.note


# -- versioning ------------------------------------------------------------


def test_editing_appends_and_the_old_version_stays_readable(gw):
    gw.policies.save(replace(demo_policy(), approval_threshold=usd("50.00")), author="a")
    gw.policies.save(replace(demo_policy(), approval_threshold=usd("75.00")), author="b")
    history = gw.policies.history()
    assert [h.version for h in history] == [3, 2, 1]
    # "What were the limits in March" is a question asked after something goes
    # wrong, and an UPDATE would have destroyed the answer.
    assert gw.policies.at_version(1).policy == demo_policy()
    assert gw.policies.at_version(2).policy.approval_threshold == usd("50.00")


def test_seeding_never_overwrites_an_edited_policy(gw):
    gw.policies.save(replace(demo_policy(), approval_threshold=usd("1.00")), author="a")
    # A restart must not quietly put the defaults back.
    seed(gw.policies, demo_policy())
    assert gw.policy.approval_threshold == usd("1.00")


# -- the HTTP surface ------------------------------------------------------


def test_the_rules_can_be_read_and_written_over_http(client):
    current = client.get("/v1/ops/policy").json()
    assert current["editable"] is True
    assert current["version"] == 1

    doc = current["policy"]
    doc["approval_threshold"] = {"minor": 2500, "currency": "USD"}
    saved = client.put(
        "/v1/ops/policy",
        json={"policy": doc, "author": "admin@example.com", "note": "tighten"},
    )
    assert saved.status_code == 200
    assert saved.json()["version"] == 2
    assert client.gateway.policy.approval_threshold == usd("25.00")


def test_an_unusable_policy_is_422_with_every_reason(client):
    doc = enc_policy(replace(demo_policy(), approval_threshold=usd("9999.00")))
    response = client.put("/v1/ops/policy", json={"policy": doc, "author": "a"})
    assert response.status_code == 422
    assert any("ask a human" in p for p in response.json()["detail"]["problems"])


def test_check_validates_without_saving(client):
    doc = enc_policy(replace(demo_policy(), hard_per_transaction_cap=usd("5000.00")))
    r = client.post("/v1/ops/policy/check", json={"policy": doc, "author": "preview"}).json()
    assert r["ok"] is True
    assert any("raised" in w for w in r["warnings"])
    # Nothing was written.
    assert client.get("/v1/ops/policy").json()["version"] == 1


def test_the_policy_routes_are_not_on_the_agent_surface(client):
    """An agent that could edit the policy would not need to defeat the policy."""
    assert client.get("/v1/agent/policy").status_code == 404
    assert client.put("/v1/agent/policy", json={}).status_code == 404


# -- the approvals queue ---------------------------------------------------


async def pending(client, gw, now):
    await gw.request_authorization(
        AuthorizationRequest(
            quote=quote(
                merchant_id="m_cloudspend",
                items=[("SKU-GPU", "GPU hour", Category.COMPUTE, "180.00", 1)],
            ),
            reason="training run",
            agent_id="ops-1",
        ),
        now=now,
    )


async def test_the_queue_shows_why_a_human_is_being_asked(client, gw, now):
    await pending(client, gw, now)
    queue = client.get("/v1/ops/approvals").json()["approvals"]
    assert len(queue) == 1
    item = queue[0]
    assert item["amount"] == "180.00"
    # An approver shown only an amount is an approver guessing.
    assert any("threshold for unattended spending" in r for r in item["reasons"])
    assert item["items"] == [{"sku": "SKU-GPU", "quantity": 1, "description": "GPU hour"}]
    assert item["agent_reason"] == "training run"


async def test_approving_from_the_console_records_who(client, gw, now):
    await pending(client, gw, now)
    decision_id = client.get("/v1/ops/approvals").json()["approvals"][0]["decision_id"]
    r = client.post(
        f"/v1/ops/approvals/{decision_id}/approve", json={"approver": "cfo@example.com"}
    )
    assert r.status_code == 200
    assert r.json()["hold"]["approved_by"] == "cfo@example.com"
    assert client.get("/v1/ops/approvals").json()["approvals"] == []


async def test_declining_from_the_console_charges_nothing(client, gw, now, paypal):
    await pending(client, gw, now)
    decision_id = client.get("/v1/ops/approvals").json()["approvals"][0]["decision_id"]
    r = client.post(
        f"/v1/ops/approvals/{decision_id}/decline", json={"approver": "cfo@example.com"}
    )
    assert r.status_code == 200
    assert r.json()["hold"]["state"] == "declined_by_human"
    assert paypal.orders == {}


async def test_a_second_verdict_on_the_same_decision_is_refused(client, gw, now):
    await pending(client, gw, now)
    decision_id = client.get("/v1/ops/approvals").json()["approvals"][0]["decision_id"]
    client.post(f"/v1/ops/approvals/{decision_id}/approve", json={"approver": "a"})
    again = client.post(f"/v1/ops/approvals/{decision_id}/approve", json={"approver": "b"})
    assert again.status_code == 409


def test_the_admin_console_is_not_on_the_agent_surface(client):
    assert client.get("/v1/ops/admin").status_code == 200
    assert client.get("/v1/agent/admin").status_code == 404
