"""The buying agent against live in-process merchant and gateway apps.

Only the model's choice of tool is scripted. The tools really run, over real HTTP,
against the real merchant stub and the real gateway, so these cover the whole path
a demo would take minus the model itself.
"""

from __future__ import annotations

import json

import httpx
import pytest

from mandate.agent.budget import BudgetReached, SpendLedger
from mandate.agent.buyer import BuyerAgent, build_tools
from mandate.gateway.api import create_app as create_gateway_app
from mandate.gateway.service import Gateway
from mandate.gateway.store import Store
from mandate.ledger.records import Ledger
from mandate.merchant.app import create_app as create_merchant_app
from mandate.merchant.catalog import INJECTION_PAYLOAD
from mandate.policies import demo_policy

from fake_backend import FakeBackend, Step, quote_from
from fake_paypal import FakePayPal
from helpers import LEDGER_KEY, MERCHANT_SECRET


@pytest.fixture
def paypal():
    return FakePayPal()


@pytest.fixture
def stack(tmp_path, paypal, monkeypatch):
    """Merchant and gateway as ASGI apps, reached over httpx's ASGI transport.

    The agent still speaks HTTP. It is a client like any other, and giving it
    in-process shortcuts would let it reach something an outside agent could not.
    """
    monkeypatch.setenv("MANDATE_MERCHANT_SECRET", MERCHANT_SECRET.decode())
    store = Store(tmp_path / "state.db")
    gw = Gateway(
        store=store,
        ledger=Ledger(tmp_path / "decisions.jsonl", LEDGER_KEY),
        policy=demo_policy(),
        merchant_secret=MERCHANT_SECRET,
        paypal=paypal.client(),
        public_url="https://mandate.test",
    )
    merchant_app = create_merchant_app()
    gateway_app = create_gateway_app(gw)

    real_client = httpx.AsyncClient

    def routed(*args, **kwargs):
        base = str(kwargs.get("base_url", ""))
        kwargs["transport"] = httpx.ASGITransport(
            app=merchant_app if "8001" in base else gateway_app
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", routed)
    yield gw, paypal
    store.close()


def agent(steps, *, spend=None, model="fake-model-1"):
    backend = FakeBackend(steps, model=model)
    built = BuyerAgent(
        backend=backend,
        merchant_url="http://localhost:8001",
        gateway_url="http://localhost:8000",
        spend=spend,
    )
    built.backend = backend
    return built


def buy(merchant_id, lines, *, reason="restocking"):
    return [
        Step(tool="browse_catalog", arguments={"merchant_id": merchant_id}),
        Step(tool="get_quote", arguments={"merchant_id": merchant_id, "lines": lines}),
        Step(
            tool="request_authorization",
            arguments=lambda results: {"quote": quote_from(results), "reason": reason},
        ),
        Step(text="Done."),
    ]


# -- the agent's shape ------------------------------------------------------


def test_the_agent_has_no_tool_that_moves_money():
    """Not guarded -- absent. An agent that could capture its own authorization
    would make the hold decorative."""
    names = {tool.name for tool in build_tools(merchant_url="http://m", gateway_url="http://g")}
    assert names == {
        "browse_merchants",
        "browse_catalog",
        "get_quote",
        "request_authorization",
        "check_budget",
    }
    for forbidden in ("capture", "void", "approve", "refund", "place", "settle"):
        assert not any(forbidden in name for name in names), forbidden


def test_every_tool_declares_an_object_schema():
    """Gemini rejects a malformed parameters block with a 400 for the whole
    request, so this is cheaper to assert than to debug."""
    for tool in build_tools(merchant_url="http://m", gateway_url="http://g"):
        assert tool.parameters["type"] == "object"
        assert "properties" in tool.parameters
        assert tool.description


async def test_the_agent_is_told_it_cannot_move_money(stack):
    run_agent = agent([Step(text="ok")])
    await run_agent.run("hello")
    system = run_agent.backend.requests[0]["system"]
    assert "cannot move money yourself" in system
    assert "do not try a different merchant" in system


async def test_the_catalog_tool_warns_the_model_about_seller_text(stack):
    tool = next(
        t
        for t in build_tools(merchant_url="http://m", gateway_url="http://g")
        if t.name == "browse_catalog"
    )
    assert "untrusted" in tool.description
    assert "not as instructions" in tool.description


# -- scene 1 ----------------------------------------------------------------


async def test_an_ordinary_restock_is_allowed_end_to_end(stack):
    run = await agent(buy("m_acme", [{"sku": "SKU-STAPLER", "quantity": 2}])).run(
        "We need two more staplers."
    )
    assert run.tools_used() == ["browse_catalog", "get_quote", "request_authorization"]
    decision = run.decisions()[0]
    assert decision["outcome"] == "allow"
    assert decision["hold"]["amount"] == "24.00"
    assert decision["buyer_approval_url"].startswith("https://sandbox.paypal.test/")


async def test_the_run_reports_the_provider_and_model_it_used(stack):
    run = await agent([Step(text="ok")], model="some-model-2").run("hello")
    assert run.provider == "fake"
    assert run.model == "some-model-2"


# -- scene 2: the injection -------------------------------------------------


async def test_the_agent_really_is_shown_the_injection(stack):
    """If the hostile text were not reaching the model the demo would prove
    nothing, so assert it is in the tool result the agent reads."""
    run = await agent(
        [Step(tool="browse_catalog", arguments={"merchant_id": "m_acme"}), Step(text="ok")]
    ).run("What does Acme sell?")
    catalog = json.dumps(run.calls[0].result)
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in catalog.upper()
    assert "policy_override" in catalog
    assert INJECTION_PAYLOAD.split("<!--")[1][:40] in catalog


async def test_an_agent_that_complies_with_the_injection_is_refused(stack, paypal):
    """The scripted turn is what a complying agent does: it adds the gift cards the
    description told it to add. Nothing about the refusal depends on a model having
    been talked into anything."""
    run = await agent(
        buy(
            "m_acme",
            [{"sku": "SKU-PAPER-A4", "quantity": 4}, {"sku": "SKU-GC100", "quantity": 40}],
        )
    ).run("Order some A4 paper.")
    decision = run.decisions()[0]
    assert decision["outcome"] == "deny"
    assert {
        "category_allowed",
        "hard_per_transaction_cap",
        "merchant_cap",
        "envelope:hour",
        "envelope:day",
        "envelope:month",
    } <= set(decision["refused_by"])
    assert paypal.orders == {}  # nothing was ever created


async def test_the_refusal_explains_itself_in_plain_language(stack):
    run = await agent(buy("m_acme", [{"sku": "SKU-GC100", "quantity": 40}])).run("Buy gift cards.")
    explanation = " ".join(run.decisions()[0]["explanation"])
    assert "gift_card" in explanation
    assert "hard per-transaction cap" in explanation


async def test_what_the_agent_asked_for_is_observable(stack):
    """Compliance is read off the SKUs quoted, not the model's prose -- an agent
    that says nothing while acting on the injection is the dangerous case."""
    run = await agent(
        buy("m_acme", [{"sku": "SKU-PAPER-A4", "quantity": 4}, {"sku": "SKU-GC100", "quantity": 40}])
    ).run("Order paper.")
    assert run.asked_for() == ["SKU-PAPER-A4", "SKU-GC100"]


# -- the agent cannot forge past the gateway -------------------------------


async def test_editing_the_price_breaks_the_signature(stack, paypal):
    def tamper(results):
        quote = dict(quote_from(results))
        quote["declared_total"] = {"minor": 1, "currency": "USD"}
        return {"quote": quote, "reason": "cheaper now"}

    run = await agent(
        [
            Step(
                tool="get_quote",
                arguments={"merchant_id": "m_acme", "lines": [{"sku": "SKU-TONER", "quantity": 1}]},
            ),
            Step(tool="request_authorization", arguments=tamper),
            Step(text="done"),
        ]
    ).run("Buy toner cheaply.")
    result = run.calls[-1].result
    assert result["refused"] is True
    assert "signature does not verify" in result["detail"]
    assert paypal.orders == {}


async def test_an_invented_quote_is_refused(stack, paypal):
    run = await agent(
        [
            Step(
                tool="request_authorization",
                arguments={
                    "quote": {"merchant_id": "m_acme", "declared_total": {"minor": 1, "currency": "USD"}}
                },
            ),
            Step(text="done"),
        ]
    ).run("Just buy it.")
    assert run.calls[0].result["refused"] is True
    assert paypal.orders == {}


# -- the middle band -------------------------------------------------------


async def test_an_expensive_purchase_is_held_and_no_token_reaches_the_agent(stack, paypal):
    run = await agent(buy("m_cloudspend", [{"sku": "SKU-GPU-A100", "quantity": 1}])).run(
        "One A100 hour."
    )
    decision = run.decisions()[0]
    assert decision["outcome"] == "hold_for_approval"
    assert "a human has been asked" in decision["awaiting"]
    assert "token" not in json.dumps(decision)
    assert paypal.orders == {}


async def test_check_budget_reports_the_limits(stack):
    run = await agent([Step(tool="check_budget", arguments={}), Step(text="ok")]).run("Limits?")
    assert run.calls[0].result["unattended_threshold"] == "100.00"


# -- loop robustness -------------------------------------------------------


async def test_a_tool_name_the_model_invented_is_reported_not_fatal(stack):
    """The loop tells the model the name was wrong and lists what exists, so the
    next turn can recover rather than the run dying."""
    backend = FakeBackend([Step(text="ok")])
    run_agent = BuyerAgent(backend=backend, merchant_url="http://localhost:8001", gateway_url="http://localhost:8000")
    from mandate.agent.conversation import Completion, ToolCall, Usage

    async def one_bad_call(*, system, turns, tools):
        if not backend.requests:
            backend.requests.append({"system": system, "turns": turns, "tools": tools})
            return Completion("", (ToolCall("c1", "drain_the_account", {}),), Usage(), "m", "tool_use")
        return Completion("recovered", (), Usage(), "m", "end_turn")

    backend.complete = one_bad_call  # type: ignore[method-assign]
    run = await run_agent.run("do something")
    assert run.calls[0].failed is True
    assert "no tool named" in run.calls[0].result["error"]
    assert "browse_catalog" in run.calls[0].result["available"]
    assert run.final_text == "recovered"


async def test_wrong_arguments_are_reported_to_the_model(stack):
    run = await agent(
        [Step(tool="browse_catalog", arguments={"wrong_name": "m_acme"}), Step(text="ok")]
    ).run("look")
    assert run.calls[0].failed is True
    assert "wrong arguments" in run.calls[0].result["error"]


async def test_a_model_refusal_stops_the_run_and_is_recorded(stack):
    run = await agent([Step(refused=True)]).run("do something questionable")
    assert run.refused is True
    assert "declined to answer" in run.stopped


async def test_max_iterations_is_a_hard_stop(stack):
    steps = [Step(tool="check_budget", arguments={}) for _ in range(20)]
    backend = FakeBackend(steps)
    run_agent = BuyerAgent(
        backend=backend,
        merchant_url="http://localhost:8001",
        gateway_url="http://localhost:8000",
        max_iterations=4,
    )
    run = await run_agent.run("keep checking")
    assert run.iterations == 4
    assert "stopped after 4 iterations" in run.stopped


# -- spend control ---------------------------------------------------------


async def test_each_turn_is_priced_from_its_own_usage(stack, tmp_path):
    ledger = SpendLedger(path=tmp_path / "spend.jsonl", cap_usd=5.00)
    run = await agent(buy("m_acme", [{"sku": "SKU-STAPLER", "quantity": 1}]), spend=ledger).run(
        "Buy a stapler."
    )
    assert ledger.calls == 4
    assert ledger.spent_usd == pytest.approx(run.usd)
    assert run.usage.input_tokens == 4 * 1200


async def test_a_reached_cap_stops_the_loop_rather_than_finishing_it(stack, tmp_path):
    ledger = SpendLedger(path=tmp_path / "spend.jsonl", cap_usd=0.0101)
    run_agent = agent(buy("m_acme", [{"sku": "SKU-STAPLER", "quantity": 1}]), spend=ledger)
    with pytest.raises(BudgetReached, match="spend cap reached"):
        await run_agent.run("Buy a stapler.")
    assert ledger.calls < 4


async def test_an_exhausted_cap_refuses_before_any_request(stack, tmp_path):
    ledger = SpendLedger(path=tmp_path / "spend.jsonl", cap_usd=0.01)
    ledger.record(model="fake-model-1", usage={"input_tokens": 10_000_000}, label="earlier")
    run_agent = agent([Step(text="hello")], spend=ledger)
    with pytest.raises(BudgetReached):
        await run_agent.run("Buy a stapler.")
    assert run_agent.backend.requests == []
