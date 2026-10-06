"""A scripted stand-in for the Anthropic client's tool runner.

Not a mock that records calls -- a small driver that plays a list of scripted
turns and actually executes the agent's tools, so the loop, the tool wiring and
the HTTP calls are all genuinely exercised. The only thing faked is the model's
choice of what to call.

That split is what makes these tests worth having. A fake that also stubbed the
tools would verify nothing but my own plumbing diagram.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeUsage:
    input_tokens: int = 1200
    output_tokens: int = 150
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeToolUseBlock:
    name: str
    input: dict[str, Any]
    id: str = "toolu_fake"
    type: str = "tool_use"


@dataclass
class FakeMessage:
    content: list[Any]
    stop_reason: str = "tool_use"
    usage: FakeUsage = field(default_factory=FakeUsage)


@dataclass
class Turn:
    """One scripted model turn.

    `tool` names a tool to call with `arguments`; `text` ends the run. `arguments`
    may be a callable taking the results so far, for a turn that depends on an
    earlier tool's output -- which is how a quote gets passed to
    request_authorization without the test hard-coding a signature.
    """

    tool: str | None = None
    arguments: Any = field(default_factory=dict)
    text: str = ""


class FakeToolRunner:
    def __init__(self, turns: list[Turn], tools: list[Any], recorder: list[Any]) -> None:
        # Enforce what the real runner enforces. An earlier version of this fake
        # looked tools up by name and called them directly, so it happily ran
        # async functions decorated with the synchronous `beta_tool` -- which the
        # real async runner refuses to register, warning "Available tools: []"
        # and failing every call. The tests passed and the live run did not.
        from anthropic.lib.tools import BetaAsyncFunctionTool

        for tool in tools:
            if not isinstance(tool, BetaAsyncFunctionTool):
                raise AssertionError(
                    f"tool {getattr(tool, 'name', tool)!r} is a "
                    f"{type(tool).__name__}; the async runner only registers "
                    "BetaAsyncFunctionTool. Use @beta_async_tool, not @beta_tool."
                )
        self._turns = list(turns)
        self._tools = {tool.name: tool for tool in tools}
        self._results: list[Any] = recorder

    def __aiter__(self) -> "FakeToolRunner":
        return self

    async def __anext__(self) -> FakeMessage:
        if not self._turns:
            raise StopAsyncIteration
        turn = self._turns.pop(0)

        if turn.tool is None:
            return FakeMessage(content=[FakeTextBlock(turn.text)], stop_reason="end_turn")

        if turn.tool not in self._tools:
            raise AssertionError(
                f"script called {turn.tool!r}, which the agent does not expose. "
                f"Available: {sorted(self._tools)}"
            )
        arguments = turn.arguments(self._results) if callable(turn.arguments) else turn.arguments

        # Actually run the tool. This is the part that must not be faked.
        raw = await self._tools[turn.tool](**arguments)
        try:
            self._results.append(json.loads(raw))
        except (TypeError, json.JSONDecodeError):
            self._results.append(raw)

        return FakeMessage(content=[FakeToolUseBlock(turn.tool, arguments)])


class FakeMessages:
    def __init__(self, owner: "FakeAnthropic") -> None:
        self._owner = owner

    def tool_runner(self, **kwargs: Any) -> FakeToolRunner:
        self._owner.requests.append(kwargs)
        return FakeToolRunner(self._owner.turns, list(kwargs["tools"]), self._owner.tool_results)


class FakeBeta:
    def __init__(self, owner: "FakeAnthropic") -> None:
        self.messages = FakeMessages(owner)


class FakeAnthropic:
    """Plays `turns` when the agent runs. `requests` records what was asked for."""

    def __init__(self, turns: list[Turn]) -> None:
        self.turns = turns
        self.requests: list[dict[str, Any]] = []
        self.tool_results: list[Any] = []
        self.beta = FakeBeta(self)


def quote_from_results(results: list[Any]) -> dict[str, Any]:
    """Pull the signed quote out of the most recent get_quote result."""
    for result in reversed(results):
        if isinstance(result, dict) and "quote" in result:
            return result["quote"]
    raise AssertionError("no quote in the results so far")
