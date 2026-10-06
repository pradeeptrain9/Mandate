"""A scripted backend, standing in for any provider.

Only the model's *choice* of tool is scripted. The tools themselves really run,
over real HTTP, against the real merchant stub and the real gateway -- so these
tests exercise the whole path a demo would, minus the model.

An earlier version of this fake looked tools up by name and called them directly,
which meant it never noticed that the Anthropic runner refused to register async
functions decorated with the synchronous `beta_tool`. The tests passed and the live
run did not. Owning the loop removed that class of mismatch: the fake now
implements the same `Backend` protocol the real providers do, so anything the loop
requires of a backend is required of this one too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mandate.agent.conversation import AgentTool, Completion, ToolCall, Turn, Usage


@dataclass
class Step:
    """One scripted model turn.

    `tool` names a tool to ask for; `text` ends the run. `arguments` may be a
    callable taking the results so far, for a turn that depends on an earlier
    tool's output -- which is how a signed quote reaches request_authorization
    without a test hard-coding a signature.
    """

    tool: str | None = None
    arguments: Any = field(default_factory=dict)
    text: str = ""
    refused: bool = False


class FakeBackend:
    provider = "fake"

    def __init__(self, steps: list[Step], *, model: str = "fake-model-1") -> None:
        self.model = model
        self._steps = list(steps)
        self.requests: list[dict[str, Any]] = []
        self.results: list[Any] = []
        self._counter = 0

    async def complete(
        self, *, system: str, turns: list[Turn], tools: list[AgentTool]
    ) -> Completion:
        self.requests.append({"system": system, "turns": list(turns), "tools": list(tools)})
        # Results are read back out of the conversation the loop built, not fed in
        # through a side channel. That keeps the fake honest: it sees exactly what
        # a real backend would see, so a script that depends on an earlier tool's
        # output depends on that output having genuinely reached the model.
        self.results = _results_in(turns)
        usage = Usage(input_tokens=1200, output_tokens=150)

        if not self._steps:
            return Completion("(script exhausted)", (), usage, self.model, "end_turn")

        step = self._steps.pop(0)
        if step.refused:
            return Completion("", (), usage, self.model, "refusal", refused=True)
        if step.tool is None:
            return Completion(step.text, (), usage, self.model, "end_turn")

        names = {tool.name for tool in tools}
        if step.tool not in names:
            raise AssertionError(
                f"script asked for {step.tool!r}, which the agent does not expose. "
                f"Available: {sorted(names)}"
            )
        arguments = step.arguments(self.results) if callable(step.arguments) else step.arguments
        self._counter += 1
        return Completion(
            "",
            (ToolCall(id=f"call-{self._counter}", name=step.tool, arguments=arguments),),
            usage,
            self.model,
            "tool_use",
        )


def _results_in(turns: list[Turn]) -> list[Any]:
    import json

    from mandate.agent.conversation import ToolResultTurn

    out: list[Any] = []
    for turn in turns:
        if isinstance(turn, ToolResultTurn):
            for _call, raw in turn.results:
                try:
                    out.append(json.loads(raw))
                except (TypeError, json.JSONDecodeError):
                    out.append(raw)
    return out


def quote_from(results: list[Any]) -> dict[str, Any]:
    """Pull the signed quote out of the most recent get_quote result."""
    for result in reversed(results):
        if isinstance(result, dict) and "quote" in result:
            return result["quote"]
    raise AssertionError("no quote in the results so far")
