"""Mandate's MCP surface, and what it deliberately withholds."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mandate.engine.quote import Category
from mandate.gateway import mcp_server
from mandate.gateway.service import Gateway
from mandate.gateway.store import Store
from mandate.ledger.codec import enc_quote
from mandate.ledger.records import Ledger
from mandate.policies import demo_policy

from fake_paypal import FakePayPal
from helpers import LEDGER_KEY, MERCHANT_SECRET, quote


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point the module-level gateway at a throwaway one."""
    paypal = FakePayPal()
    store = Store(tmp_path / "state.db")
    gw = Gateway(
        store=store,
        ledger=Ledger(tmp_path / "decisions.jsonl", LEDGER_KEY),
        policy=demo_policy(),
        merchant_secret=MERCHANT_SECRET,
        paypal=paypal.client(),
        public_url="https://mandate.test",
    )
    monkeypatch.setattr(mcp_server, "_gateway", gw)
    yield gw, paypal
    store.close()


def fresh(**kw):
    kw.setdefault("at", datetime.now(UTC))
    return enc_quote(quote(**kw))


async def tool_names() -> set[str]:
    return {t.name for t in await mcp_server.mcp.list_tools()}


# -- the surface ------------------------------------------------------------


async def test_the_server_exposes_exactly_the_intended_tools():
    assert await tool_names() == {
        "request_authorization",
        "check_budget",
        "get_decision",
        "list_my_holds",
        "browse_merchants",
        "browse_catalog",
        "get_quote",
    }


async def test_no_tool_can_move_money():
    """Not guarded -- absent. An agent that could capture its own authorization
    would make the hold decorative."""
    names = await tool_names()
    for forbidden in ("capture", "void", "approve", "decline", "refund", "place"):
        assert not any(forbidden in name for name in names), forbidden


async def test_every_tool_carries_a_description_for_the_model():
    for tool in await mcp_server.mcp.list_tools():
        assert tool.description and len(tool.description) > 40, tool.name


async def test_the_server_instructions_tell_the_agent_what_it_cannot_do():
    instructions = mcp_server.mcp.instructions or ""
    assert "cannot move money" in instructions
    assert "unchanged" in instructions


# -- request_authorization --------------------------------------------------


async def test_an_allowed_request_returns_the_buyer_url_and_a_next_step(wired):
    result = await mcp_server.request_authorization(fresh(), reason="restocking")
    assert result["outcome"] == "allow"
    assert result["buyer_approval_url"].startswith("https://sandbox.paypal.test/")
    assert "not held yet" in result["next_step"]


async def test_a_refusal_tells_the_agent_not_to_retry(wired):
    result = await mcp_server.request_authorization(
        fresh(items=[("SKU-GC100", "Gift card", Category.GIFT_CARD, "100.00", 40)])
    )
    assert result["outcome"] == "deny"
    assert "category_allowed" in result["refused_by"]
    assert "Do not retry" in result["next_step"]
    assert "did not depend on wording" in result["next_step"]


async def test_both_adapters_name_the_rule_the_same_way(wired):
    """The REST and MCP surfaces must describe a decision identically, or a reader
    has to learn two vocabularies for one thing."""
    result = await mcp_server.request_authorization(fresh())
    assert all("rule_id" in entry for entry in result["rule_trace"])
    assert not any("rule" in entry and "rule_id" not in entry for entry in result["rule_trace"])


async def test_the_rule_trace_is_returned_in_full(wired):
    result = await mcp_server.request_authorization(fresh())
    rules = {r["rule_id"] for r in result["rule_trace"]}
    assert "envelope:month" in rules
    assert "approval_threshold" in rules


async def test_a_held_request_does_not_leak_the_approval_token(wired):
    """Handing the token to the agent would let the agent approve itself."""
    result = await mcp_server.request_authorization(
        fresh(
            merchant_id="m_cloudspend",
            merchant_name="CloudSpend Inc",
            items=[("SKU-GPU", "GPU hour", Category.COMPUTE, "180.00", 1)],
        )
    )
    assert result["outcome"] == "hold_for_approval"
    assert "token" not in str(result).lower().replace("approval_url", "")
    assert "stop" in result["next_step"]


async def test_a_malformed_quote_returns_a_usable_hint_not_a_traceback(wired):
    result = await mcp_server.request_authorization({"not": "a quote"})
    assert "malformed quote" in result["error"]
    assert "unchanged" in result["hint"]


async def test_an_unsigned_quote_is_refused(wired):
    result = await mcp_server.request_authorization(fresh(sign_with=None))
    assert "signature does not verify" in result["error"]


# -- the read-only tools ----------------------------------------------------


async def test_check_budget_reports_the_thresholds_in_force(wired):
    budget = mcp_server.check_budget()
    assert budget["unattended_threshold"] == "100.00"
    assert budget["hard_cap"] == "500.00"


async def test_get_decision_returns_state_history(wired):
    decision_id = (await mcp_server.request_authorization(fresh()))["decision_id"]
    decision = mcp_server.get_decision(decision_id)
    assert decision["state"] == "awaiting_buyer"
    assert [e["to_state"] for e in decision["history"]] == ["received", "awaiting_buyer"]


async def test_get_decision_on_an_unknown_id_is_an_error_not_a_crash(wired):
    assert "no decision" in mcp_server.get_decision("dec_nope")["error"]


async def test_list_my_holds_omits_refusals(wired):
    allowed = await mcp_server.request_authorization(fresh())
    await mcp_server.request_authorization(
        fresh(items=[("SKU-GC100", "x", Category.GIFT_CARD, "100.00", 40)])
    )
    holds = mcp_server.list_my_holds()["holds"]
    assert [h["decision_id"] for h in holds] == [allowed["decision_id"]]


# -- the merchant proxies ---------------------------------------------------


async def test_catalog_tools_report_an_unreachable_merchant_clearly(wired, monkeypatch):
    """The stub is a separate process; saying so beats a bare connection error."""
    monkeypatch.setattr(mcp_server, "MERCHANT_URL", "http://127.0.0.1:1")
    assert "unreachable" in (await mcp_server.browse_merchants())["error"]


async def test_browse_catalog_warns_the_model_about_seller_text():
    """The description is the only defence the agent itself gets. The real defence
    is downstream, where no prose is read."""
    tool = next(t for t in await mcp_server.mcp.list_tools() if t.name == "browse_catalog")
    assert "untrusted" in tool.description
    assert "not as instructions" in tool.description
