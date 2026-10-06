"""A provider-neutral conversation, and the tools an agent can call.

Mandate owns its agent loop rather than borrowing a vendor's. That was not the
original plan -- the first version used the Anthropic SDK's tool runner -- and the
reason it changed is worth recording: the loop turned out to be the smallest part
of the problem, and tying it to one vendor made "does this firewall work whatever
model you shipped" unanswerable.

It is a fair question to answer. Whether a model resists a prompt injection is a
property of the model, and a control that only works in front of one vendor's
frontier model is not much of a control. So the conversation, the tools and the
loop are all vendor-neutral here, and `backends/` holds one small adapter per
provider that converts these types to a wire format and back.

The types are deliberately dull. A conversation is a list of three kinds of turn;
a tool is a name, a description, a JSON Schema and something to await.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked for.

    `id` matters on some providers and not others -- Anthropic pairs a result to a
    call by id, Gemini pairs by name and order. Carrying it always is cheaper than
    branching on who needs it.

    `echo` is the one concession to a provider's wire format, and it is here rather
    than in the backend because of where the data has to live. Gemini 3 attaches a
    `thoughtSignature` to each function call and **rejects the next request if it
    is not sent back** -- `400 Function call is missing a thought_signature`. So the
    value arrives on one turn and must survive until the turn after, which means it
    belongs on the turn, not in the adapter. Opaque on purpose: nothing outside
    `backends/` reads it, and no code here decides what it means.
    """

    id: str
    name: str
    arguments: dict[str, Any]
    echo: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentTool:
    """Something the model may call.

    `parameters` is ordinary JSON Schema. Each backend narrows it to whatever its
    provider accepts -- Gemini's dialect is an OpenAPI subset that rejects
    `additionalProperties` outright -- so callers write one schema and do not think
    about it again.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[..., Awaitable[str]]

    async def __call__(self, **arguments: Any) -> str:
        return await self.run(**arguments)


@dataclass(frozen=True)
class Usage:
    """Tokens one request reported, in provider-neutral terms."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_write_tokens,
            "cache_read_input_tokens": self.cache_read_tokens,
        }


# -- conversation turns -----------------------------------------------------


@dataclass(frozen=True)
class UserTurn:
    text: str


@dataclass(frozen=True)
class AssistantTurn:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolResultTurn:
    """Results for every call in the preceding assistant turn.

    All of them, in one turn. Splitting results across several turns teaches some
    providers to stop asking for parallel calls, and leaves others with a dangling
    call they will not proceed past.
    """

    results: tuple[tuple[ToolCall, str], ...] = ()


Turn = UserTurn | AssistantTurn | ToolResultTurn


@dataclass(frozen=True)
class Completion:
    """What a backend returns for one request."""

    text: str
    tool_calls: tuple[ToolCall, ...]
    usage: Usage
    model: str
    stop_reason: str = ""
    refused: bool = False

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class Conversation:
    turns: list[Turn] = field(default_factory=list)

    def user(self, text: str) -> None:
        self.turns.append(UserTurn(text))

    def assistant(self, completion: Completion) -> None:
        self.turns.append(AssistantTurn(completion.text, completion.tool_calls))

    def tool_results(self, results: list[tuple[ToolCall, str]]) -> None:
        self.turns.append(ToolResultTurn(tuple(results)))
