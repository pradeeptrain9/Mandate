"""The PayPal Agent Toolkit, for the merchant-side work it is good at.

Division of labour, stated once so it is not mistaken for indecision:

  * **The hold lifecycle speaks raw REST** (`providers/paypal.py`). The toolkit
    exposes `create_order`, `pay_order`, `get_order_details` and refunds, but not
    `authorize`, `void` or `reauthorize` -- and those three *are* the mechanism by
    which Mandate holds funds without taking them.
  * **Everything else speaks the toolkit** (here). Shipment tracking for the
    delivery oracle, disputes for the ledger's last column, transaction search
    for the dashboard. Forty-two tools, each carrying a Pydantic schema, which is
    strictly better than hand-rolling those calls.

This also replaced a dead end. PayPal runs a remote MCP server at
`mcp.sandbox.paypal.com`, and its OAuth metadata advertises
`grant_types_supported: ["authorization_code", "refresh_token"]` -- no
`client_credentials`. It wants an interactive browser consent flow with dynamic
client registration, which a headless gateway cannot do. The toolkit takes plain
client credentials, so it is the right local answer and nothing structural
changes.

Two sharp edges worth knowing before relying on this:

  * `get_merchant_insights` **raises in sandbox** -- the toolkit refuses it
    outright rather than returning empty data. The dashboard must not depend on
    it. `SANDBOX_UNAVAILABLE` names it so callers fail early and loudly.
  * `PayPalAPI.run` is **synchronous**. Called directly from an async request
    handler it would block the event loop, so every call here goes through
    `asyncio.to_thread`.
  * `PayPalAPI` is a Pydantic model with assignment forbidden, so it cannot be
    monkeypatched in a test. Hence the `runner` seam on `Toolkit.__init__`:
    injecting a callable is cleaner than reaching into a third-party model, and it
    means the tests need no credentials and no network.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from paypal_agent_toolkit.shared.api import PayPalAPI
from paypal_agent_toolkit.shared.configuration import Context
from paypal_agent_toolkit.shared.tools import tools as TOOLKIT_TOOLS

#: Methods the toolkit refuses in sandbox. Named rather than discovered at call
#: time so a caller gets a clear error instead of a surprising one.
SANDBOX_UNAVAILABLE: frozenset[str] = frozenset({"get_merchant_insights"})

#: What Mandate actually uses. Deliberately narrow: an agent-facing surface that
#: offered all 42 would hand an agent invoice creation and subscription
#: management it has no business with.
DELIVERY_METHODS = ("create_shipment_tracking", "get_shipment_tracking", "update_shipment_tracking")
DISPUTE_METHODS = ("list_disputes", "get_dispute")
REPORTING_METHODS = ("list_transactions",)


class ToolkitError(RuntimeError):
    pass


class ToolkitUnavailableInSandbox(ToolkitError):
    """A method the toolkit refuses when `sandbox=True`."""


@dataclass(frozen=True)
class ToolSpec:
    """One toolkit tool, reshaped for Claude's `tools` parameter."""

    method: str
    name: str
    description: str
    input_schema: dict[str, Any]

    def as_anthropic_tool(self) -> dict[str, Any]:
        return {
            "name": self.method,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class Toolkit:
    def __init__(
        self,
        client_id: str,
        secret: str,
        *,
        sandbox: bool = True,
        runner: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        if not client_id or not secret:
            raise ValueError("PayPal client id and secret are both required")
        self.sandbox = sandbox
        self._api = PayPalAPI(
            client_id=client_id, secret=secret, context=Context(sandbox=sandbox)
        )
        #: The one place a call leaves this process. Overridden in tests; see the
        #: module docstring for why patching `_api` directly is not an option.
        self._run: Callable[[str, dict[str, Any]], Any] = runner or self._api.run

    # -- calling ---------------------------------------------------------

    async def call(self, method: str, **params: Any) -> Any:
        """Run a toolkit method off the event loop and parse its result.

        The toolkit returns JSON as a string for most methods and occasionally a
        plain message. Both are returned as-is rather than coerced, because a
        caller that cannot tell "no disputes" from "the call failed" is worse off
        than one handed the raw answer.
        """
        if self.sandbox and method in SANDBOX_UNAVAILABLE:
            raise ToolkitUnavailableInSandbox(
                f"{method} is not available in sandbox; do not build on it"
            )
        if method not in self.methods():
            raise ToolkitError(f"{method} is not a toolkit method")
        try:
            raw = await asyncio.to_thread(self._run, method, params)
        except Exception as exc:  # the toolkit raises bare ValueErrors and worse
            raise ToolkitError(f"{method} failed: {exc}") from exc
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
        return raw

    # -- the narrow surface Mandate uses ---------------------------------

    async def track_shipment(
        self, *, transaction_id: str, tracking_number: str, carrier: str, status: str = "SHIPPED"
    ) -> Any:
        """Attach carrier tracking to a captured PayPal transaction."""
        return await self.call(
            "create_shipment_tracking",
            transaction_id=transaction_id,
            tracking_number=tracking_number,
            carrier=carrier,
            status=status,
        )

    async def shipment_status(
        self, *, transaction_id: str | None = None, order_id: str | None = None
    ) -> Any:
        """Read tracking back. Keyed on the transaction or the order, not the
        tracking number -- the toolkit's schema takes those two and nothing else."""
        if not transaction_id and not order_id:
            raise ToolkitError("one of transaction_id or order_id is required")
        params = {k: v for k, v in (("transaction_id", transaction_id), ("order_id", order_id)) if v}
        return await self.call("get_shipment_tracking", **params)

    async def disputes(self) -> Any:
        return await self.call("list_disputes")

    async def dispute(self, dispute_id: str) -> Any:
        return await self.call("get_dispute", dispute_id=dispute_id)

    async def transactions(self, **params: Any) -> Any:
        return await self.call("list_transactions", **params)

    # -- introspection ---------------------------------------------------

    @staticmethod
    def methods() -> frozenset[str]:
        return frozenset(str(tool["method"]) for tool in TOOLKIT_TOOLS)

    @staticmethod
    def specs(methods: tuple[str, ...] | None = None) -> list[ToolSpec]:
        """Toolkit tools as Claude tool definitions.

        Used where an agent genuinely should reach PayPal directly -- a
        merchant-side operations agent, not the buying agent, which never touches
        PayPal at all.
        """
        wanted = set(methods) if methods else None
        out: list[ToolSpec] = []
        for tool in TOOLKIT_TOOLS:
            method = str(tool["method"])
            if wanted is not None and method not in wanted:
                continue
            schema_model = tool.get("args_schema")
            schema = (
                schema_model.model_json_schema()
                if schema_model is not None and hasattr(schema_model, "model_json_schema")
                else {"type": "object", "properties": {}}
            )
            # Claude requires an object schema with `properties` present.
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            out.append(
                ToolSpec(
                    method=method,
                    name=str(tool.get("name", method)),
                    description=str(tool.get("description", "")).strip(),
                    input_schema=schema,
                )
            )
        return sorted(out, key=lambda s: s.method)
