"""Claude, on the Messages API.

Deliberately the plain `messages.create` endpoint and not the SDK's tool runner.
The runner is good, but the loop is the smallest part of this problem and owning
it is what lets the same loop drive Gemini -- which matters, because a firewall
that only works in front of one vendor's frontier model is not much of a
firewall.

One dialect note: `thinking` and `output_config.effort` are accepted by the 4.6+
family and rejected by Haiku 4.5 and older, which take the earlier fixed
`budget_tokens` form and no effort parameter at all. `request_config` picks per
model, because comparing models is part of evaluating this system.
"""

from __future__ import annotations

from typing import Any

from ..conversation import (
    AgentTool,
    AssistantTurn,
    Completion,
    ToolCall,
    ToolResultTurn,
    Turn,
    Usage,
    UserTurn,
)

DEFAULT_MODEL = "claude-opus-5"

#: Models taking `thinking: {"type": "adaptive"}` and `output_config.effort`.
#: Haiku 4.5 returns `400 adaptive thinking is not supported on this model` and
#: rejects `effort` separately.
ADAPTIVE_THINKING_MODELS = (
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-fable-5",
)


def request_config(model: str) -> dict[str, Any]:
    """Thinking and effort parameters this model will actually accept."""
    if any(model.startswith(prefix) for prefix in ADAPTIVE_THINKING_MODELS):
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}
    return {"thinking": {"type": "enabled", "budget_tokens": 2048}}


def _tool_params(tools: list[AgentTool]) -> list[dict[str, Any]]:
    return [
        {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}
        for tool in tools
    ]


def _messages(turns: list[Turn]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for turn in turns:
        if isinstance(turn, UserTurn):
            out.append({"role": "user", "content": turn.text})
        elif isinstance(turn, AssistantTurn):
            blocks: list[dict[str, Any]] = []
            if turn.text:
                blocks.append({"type": "text", "text": turn.text})
            for call in turn.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif isinstance(turn, ToolResultTurn):
            # Every result in one user message. Splitting them across messages
            # teaches the model to stop asking for parallel calls.
            out.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": call.id, "content": result}
                        for call, result in turn.results
                    ],
                }
            )
    return out


def _usage(usage: Any) -> Usage:
    def count(name: str) -> int:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return Usage(
        input_tokens=count("input_tokens"),
        output_tokens=count("output_tokens"),
        cache_write_tokens=count("cache_creation_input_tokens"),
        cache_read_tokens=count("cache_read_input_tokens"),
    )


class ClaudeBackend:
    provider = "claude"

    def __init__(
        self,
        client: Any,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 8192,
    ) -> None:
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    async def aclose(self) -> None:
        return None

    async def complete(
        self, *, system: str, turns: list[Turn], tools: list[AgentTool]
    ) -> Completion:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=_messages(turns),
            tools=_tool_params(tools),
            **request_config(self.model),
        )

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in getattr(response, "content", []) or []:
            kind = getattr(block, "type", None)
            if kind == "text" and getattr(block, "text", ""):
                text_parts.append(block.text)
            elif kind == "tool_use":
                calls.append(
                    ToolCall(
                        id=str(getattr(block, "id", "")),
                        name=str(getattr(block, "name", "")),
                        arguments=dict(getattr(block, "input", {}) or {}),
                    )
                )

        stop_reason = str(getattr(response, "stop_reason", "") or "")
        return Completion(
            text="\n".join(text_parts).strip(),
            tool_calls=tuple(calls),
            usage=_usage(getattr(response, "usage", None)),
            model=str(getattr(response, "model", self.model)),
            stop_reason=stop_reason,
            # Always check `stop_reason` before trusting content: a safety
            # classifier can decline with HTTP 200.
            refused=stop_reason == "refusal",
        )
