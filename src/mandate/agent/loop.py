"""The agent loop. One implementation, any provider.

Request, run whatever tools came back, feed the results in, repeat until the model
stops asking. Three things are enforced here rather than left to each backend,
because getting them wrong is how a loop quietly misbehaves:

  * **Spend is checked before every request**, not after. A tool loop is the shape
    that bills a surprise, and a demo gets re-run dozens of times while a video is
    shot with nobody watching the console.
  * **A tool that raises becomes an error result**, not an exception that kills the
    run. The model can often recover, and a half-finished run that throws away
    what it learned is worse than one that is told the call failed.
  * **`max_iterations` is a hard stop.** A model that keeps asking for tools is
    stopped and said to have been stopped, rather than looping until the budget
    runs out.

`on_event` exists because of how the demo actually reads. A turn on a free-tier
model has been measured over a minute, and a scene that printed its trace only at
the end left a judge watching a blank terminal for three -- which looks exactly
like a hang. The callback is synchronous and its exceptions are deliberately
swallowed: a progress line is not allowed to be the thing that kills a run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .conversation import AgentTool, Completion, Conversation, ToolCall, Turn, Usage


class Backend(Protocol):
    model: str
    provider: str

    async def complete(
        self, *, system: str, turns: list[Turn], tools: list[AgentTool]
    ) -> Completion: ...


class SpendGuard(Protocol):
    def check(self, *, model: str) -> None: ...
    def record(self, *, model: str, usage: Any, label: str = "") -> float: ...


@dataclass
class ExecutedCall:
    name: str
    arguments: dict[str, Any]
    result: Any
    failed: bool = False


@dataclass
class AgentRun:
    """What the agent did, for a human and for the tests."""

    provider: str = ""
    model: str = ""
    calls: list[ExecutedCall] = field(default_factory=list)
    final_text: str = ""
    usage: Usage = field(default_factory=Usage)
    usd: float = 0.0
    iterations: int = 0
    stopped: str = ""
    refused: bool = False

    def tools_used(self) -> list[str]:
        return [call.name for call in self.calls]

    def decisions(self) -> list[dict[str, Any]]:
        return [
            call.result
            for call in self.calls
            if call.name == "request_authorization" and isinstance(call.result, dict)
        ]

    def asked_for(self) -> list[str]:
        """SKUs the agent put in a quote.

        Compliance with an injected instruction is read off this rather than off
        the model's prose -- an agent that says nothing about the injection while
        acting on it is the dangerous case.
        """
        skus: list[str] = []
        for call in self.calls:
            if call.name == "get_quote":
                for line in call.arguments.get("lines") or []:
                    if isinstance(line, dict) and line.get("sku"):
                        skus.append(str(line["sku"]))
        return skus


async def run_agent(
    *,
    backend: Backend,
    system: str,
    instruction: str,
    tools: list[AgentTool],
    spend: SpendGuard | None = None,
    max_iterations: int = 12,
    label: str = "agent",
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> AgentRun:
    def emit(kind: str, **detail: Any) -> None:
        if on_event is None:
            return
        try:
            on_event(kind, detail)
        except Exception:  # noqa: BLE001, S110 - a progress line must never fail a run
            # Deliberately not logged: a caller whose progress callback raises is
            # usually a caller whose logging raises too, and the run matters more
            # than the trace of why its cosmetics broke.
            pass

    by_name = {tool.name: tool for tool in tools}
    conversation = Conversation()
    conversation.user(instruction)
    run = AgentRun(provider=backend.provider, model=backend.model)

    for iteration in range(1, max_iterations + 1):
        run.iterations = iteration
        if spend is not None:
            spend.check(model=backend.model)

        emit("thinking", iteration=iteration, model=backend.model)
        completion = await backend.complete(
            system=system, turns=conversation.turns, tools=tools
        )
        run.usage = run.usage + completion.usage
        if spend is not None:
            run.usd += spend.record(
                model=backend.model, usage=completion.usage.as_dict(), label=label
            )

        if completion.text:
            run.final_text = completion.text
        if completion.refused:
            run.refused = True
            run.stopped = f"the model declined to answer ({completion.stop_reason})"
            return run

        if not completion.wants_tools:
            run.stopped = completion.stop_reason or "end_turn"
            return run

        conversation.assistant(completion)
        results: list[tuple[ToolCall, str]] = []
        for call in completion.tool_calls:
            emit("tool", name=call.name, arguments=call.arguments)
            result_text, parsed, failed = await _invoke(by_name, call)
            results.append((call, result_text))
            run.calls.append(ExecutedCall(call.name, call.arguments, parsed, failed))
            emit("tool_done", name=call.name, failed=failed, result=parsed)
        conversation.tool_results(results)

    run.stopped = f"stopped after {max_iterations} iterations without a final answer"
    return run


async def _invoke(
    by_name: dict[str, AgentTool], call: ToolCall
) -> tuple[str, Any, bool]:
    tool = by_name.get(call.name)
    if tool is None:
        # A name the model invented. Telling it so is more useful than failing the
        # run, and it lists what exists so the next turn can recover.
        message = json.dumps(
            {"error": f"no tool named {call.name!r}", "available": sorted(by_name)}
        )
        return message, json.loads(message), True
    try:
        raw = await tool(**call.arguments)
    except TypeError as exc:
        message = json.dumps({"error": f"wrong arguments for {call.name}: {exc}"})
        return message, json.loads(message), True
    except Exception as exc:  # noqa: BLE001 - reported to the model, not swallowed
        message = json.dumps({"error": f"{call.name} failed: {exc}"})
        return message, json.loads(message), True
    try:
        return raw, json.loads(raw), False
    except (TypeError, json.JSONDecodeError):
        return raw, raw, False
