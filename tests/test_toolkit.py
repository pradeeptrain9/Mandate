"""The toolkit wrapper: a narrow surface, a sandbox trap, and no event-loop blocking."""

from __future__ import annotations

import threading

import pytest

from mandate.providers.toolkit import (
    DELIVERY_METHODS,
    DISPUTE_METHODS,
    SANDBOX_UNAVAILABLE,
    Toolkit,
    ToolkitError,
    ToolkitUnavailableInSandbox,
)


def test_credentials_are_required():
    with pytest.raises(ValueError):
        Toolkit("", "secret")


def test_the_toolkit_exposes_the_methods_this_project_relies_on():
    methods = Toolkit.methods()
    for method in DELIVERY_METHODS + DISPUTE_METHODS + ("list_transactions",):
        assert method in methods


def test_the_toolkit_does_not_expose_the_hold_primitives():
    """The reason providers/paypal.py exists at all. If a future toolkit release
    adds these, this test fails and that file can shrink."""
    methods = Toolkit.methods()
    for absent in ("authorize_order", "void_authorization", "reauthorize"):
        assert absent not in methods
    # It does cover orders and refunds, which is why those are not duplicated.
    assert {"create_order", "get_order_details", "pay_order"} <= methods


async def test_merchant_insights_is_refused_in_sandbox_with_a_clear_error():
    """The toolkit raises rather than returning empty data, so the dashboard must
    not be built on it. Fail early and say why."""
    toolkit = Toolkit("cid", "secret", sandbox=True)
    with pytest.raises(ToolkitUnavailableInSandbox, match="do not build on it"):
        await toolkit.call("get_merchant_insights")
    assert "get_merchant_insights" in SANDBOX_UNAVAILABLE


async def test_an_unknown_method_is_refused_before_any_network_call():
    toolkit = Toolkit("cid", "secret")
    with pytest.raises(ToolkitError, match="is not a toolkit method"):
        await toolkit.call("drain_the_account")


async def test_shipment_status_requires_a_key():
    toolkit = Toolkit("cid", "secret")
    with pytest.raises(ToolkitError, match="transaction_id or order_id"):
        await toolkit.shipment_status()


async def test_calls_run_off_the_event_loop():
    """`PayPalAPI.run` is synchronous; called inline it would block the loop."""
    seen: dict[str, object] = {}

    def fake_run(method: str, params: dict) -> str:
        seen["thread"] = threading.current_thread().name
        seen["method"] = method
        seen["params"] = params
        return '{"disputes": []}'

    toolkit = Toolkit("cid", "secret", runner=fake_run)
    result = await toolkit.disputes()
    assert result == {"disputes": []}
    assert seen["method"] == "list_disputes"
    assert seen["thread"] != threading.main_thread().name


async def test_a_non_json_reply_is_returned_rather_than_coerced():
    """A caller that cannot tell 'no disputes' from 'the call failed' is worse off
    than one handed the raw answer."""
    toolkit = Toolkit("cid", "secret", runner=lambda m, p: "no disputes found")
    assert await toolkit.disputes() == "no disputes found"


async def test_toolkit_exceptions_are_wrapped_with_the_method_name():
    def boom(method: str, params: dict):
        raise ValueError("upstream exploded")

    toolkit = Toolkit("cid", "secret", runner=boom)
    with pytest.raises(ToolkitError, match="list_disputes failed: upstream exploded"):
        await toolkit.disputes()


def test_specs_are_valid_anthropic_tool_definitions():
    specs = Toolkit.specs(DELIVERY_METHODS + DISPUTE_METHODS)
    assert {s.method for s in specs} == set(DELIVERY_METHODS + DISPUTE_METHODS)
    for spec in specs:
        tool = spec.as_anthropic_tool()
        assert set(tool) == {"name", "description", "input_schema"}
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"
        assert "properties" in tool["input_schema"]


def test_specs_without_a_filter_cover_every_toolkit_method():
    assert {s.method for s in Toolkit.specs()} == Toolkit.methods()
