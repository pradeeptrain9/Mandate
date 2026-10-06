"""Both backends against the same protocol, and the dialect traps each one has.

These matter because the point of a provider-neutral loop is that the firewall's
behaviour does not depend on which model you shipped. That claim is only worth
anything if both adapters genuinely work, and each provider has at least one
difference that is a 400 rather than a warning.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from mandate.agent.backends.claude import ClaudeBackend, request_config
from mandate.agent.backends.gemini import (
    FREE_TIER_MODELS,
    GeminiBackend,
    GeminiUnavailable,
    sanitise_schema,
)
from mandate.agent.conversation import (
    AgentTool,
    AssistantTurn,
    ToolCall,
    ToolResultTurn,
    UserTurn,
)


async def noop(**_: object) -> str:
    return "{}"


TOOL = AgentTool(
    name="get_quote",
    description="Price a basket.",
    parameters={
        "type": "object",
        "additionalProperties": False,
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "properties": {"merchant_id": {"type": "string"}, "note": {"type": ["string", "null"]}},
        "required": ["merchant_id"],
    },
    run=noop,
)

CONVERSATION = [
    UserTurn("buy paper"),
    AssistantTurn("", (ToolCall("c1", "get_quote", {"merchant_id": "m_acme"}),)),
    ToolResultTurn(((ToolCall("c1", "get_quote", {"merchant_id": "m_acme"}), '{"total":"8.50"}'),)),
]


# -- Gemini ------------------------------------------------------------------


def test_gemini_strips_schema_keys_its_dialect_rejects():
    """`additionalProperties` and friends are a 400 for the whole request, not a
    warning, so this is cheaper to assert than to debug at runtime."""
    cleaned = sanitise_schema(TOOL.parameters)
    assert "additionalProperties" not in cleaned
    assert "$schema" not in cleaned
    assert cleaned["properties"]["merchant_id"] == {"type": "string"}


def test_gemini_rewrites_a_type_union_as_nullable():
    """There is no type union in Gemini's dialect; the array form is refused."""
    cleaned = sanitise_schema({"type": ["string", "null"]})
    assert cleaned == {"type": "string", "nullable": True}


def gemini(handler, **kwargs) -> GeminiBackend:
    return GeminiBackend("key", transport=httpx.MockTransport(handler), **kwargs)


def reply(parts, *, finish="STOP", usage=None):
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": parts}, "finishReason": finish}],
            "usageMetadata": usage or {"promptTokenCount": 100, "candidatesTokenCount": 20},
            "modelVersion": "gemini-3.8-flash",
        },
    )


async def test_gemini_sends_the_classic_generate_content_shape():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["url"] = str(request.url)
        return reply([{"text": "hello"}])

    backend = gemini(handler)
    await backend.complete(system="be brief", turns=CONVERSATION, tools=[TOOL])
    await backend.aclose()

    body = seen["body"]
    assert ":generateContent" in seen["url"]
    assert body["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    assert body["tools"][0]["functionDeclarations"][0]["name"] == "get_quote"
    # Roles: there is no "assistant" in Gemini's dialect.
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["contents"][1]["parts"][0]["functionCall"]["name"] == "get_quote"


async def test_gemini_returns_tool_results_as_an_object_keyed_by_name():
    """`functionResponse.response` must be an object; a bare string is refused,
    and results are paired by function name rather than call id."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return reply([{"text": "ok"}])

    backend = gemini(handler)
    await backend.complete(system="s", turns=CONVERSATION, tools=[TOOL])
    await backend.aclose()
    response_part = seen["body"]["contents"][2]["parts"][0]["functionResponse"]
    assert response_part["name"] == "get_quote"
    assert response_part["response"] == {"total": "8.50"}


async def test_gemini_wraps_a_non_json_tool_result():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return reply([{"text": "ok"}])

    call = ToolCall("c1", "get_quote", {})
    backend = gemini(handler)
    await backend.complete(
        system="s", turns=[UserTurn("x"), ToolResultTurn(((call, "plain text"),))], tools=[TOOL]
    )
    await backend.aclose()
    assert seen["body"]["contents"][1]["parts"][0]["functionResponse"]["response"] == {
        "result": "plain text"
    }


async def test_gemini_parses_a_function_call():
    backend = gemini(
        lambda r: reply([{"functionCall": {"name": "get_quote", "args": {"merchant_id": "m_acme"}}}])
    )
    completion = await backend.complete(system="s", turns=[UserTurn("buy")], tools=[TOOL])
    await backend.aclose()
    assert completion.wants_tools
    assert completion.tool_calls[0].name == "get_quote"
    assert completion.tool_calls[0].arguments == {"merchant_id": "m_acme"}
    assert completion.tool_calls[0].id  # minted locally; Gemini pairs by name


async def test_gemini_does_not_bill_cached_tokens_twice():
    """promptTokenCount already includes the cached half."""
    backend = gemini(
        lambda r: reply(
            [{"text": "ok"}],
            usage={
                "promptTokenCount": 1000,
                "cachedContentTokenCount": 400,
                "candidatesTokenCount": 50,
                "thoughtsTokenCount": 10,
            },
        )
    )
    completion = await backend.complete(system="s", turns=[UserTurn("x")], tools=[])
    await backend.aclose()
    assert completion.usage.input_tokens == 600
    assert completion.usage.cache_read_tokens == 400
    assert completion.usage.output_tokens == 60  # candidates plus thoughts


async def test_gemini_marks_a_safety_stop_as_a_refusal():
    backend = gemini(lambda r: reply([{"text": ""}], finish="SAFETY"))
    completion = await backend.complete(system="s", turns=[UserTurn("x")], tools=[])
    await backend.aclose()
    assert completion.refused is True


async def test_gemini_handles_a_response_with_no_candidates():
    backend = gemini(
        lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    )
    completion = await backend.complete(system="s", turns=[UserTurn("x")], tools=[])
    await backend.aclose()
    assert completion.refused is True
    assert completion.stop_reason == "SAFETY"


async def test_gemini_carries_the_error_body_because_the_status_alone_is_useless():
    """400 is usually a schema it refuses, 429 is quota, 403 is an unenabled key.
    Only the body says which."""
    backend = gemini(
        lambda r: httpx.Response(400, json={"error": {"message": "Invalid JSON payload"}})
    )
    with pytest.raises(GeminiUnavailable, match="Invalid JSON payload"):
        await backend.complete(system="s", turns=[UserTurn("x")], tools=[])
    await backend.aclose()


async def test_gemini_discovers_the_best_reachable_free_tier_model():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
                ]
            },
        )

    backend = gemini(handler)
    assert await backend.discover_model() == "gemini-3.6-flash"  # preferred over 2.5
    models = await backend.available_models()
    await backend.aclose()
    assert {m["name"] for m in models} == {"gemini-2.5-flash", "gemini-3.6-flash"}


def test_a_gemini_backend_needs_a_key():
    with pytest.raises(ValueError, match="aistudio.google.com"):
        GeminiBackend("")


def test_the_preferred_model_list_is_newest_first():
    assert FREE_TIER_MODELS[0].startswith("gemini-3")
    assert "flash" in FREE_TIER_MODELS[0]


# -- Claude ------------------------------------------------------------------


class FakeClaudeResponse:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.model = "claude-opus-5"
        self.usage = {"input_tokens": 90, "output_tokens": 10}


class FakeBlock:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeClaudeClient:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

        class Messages:
            async def create(inner, **kwargs):  # noqa: N805
                self.calls.append(kwargs)
                return self._response

        class Beta:
            pass

        self.messages = Messages()


async def test_claude_sends_the_messages_api_shape():
    client = FakeClaudeClient(FakeClaudeResponse([FakeBlock(type="text", text="hi")]))
    backend = ClaudeBackend(client)
    completion = await backend.complete(system="be brief", turns=CONVERSATION, tools=[TOOL])

    request = client.calls[0]
    assert request["model"] == "claude-opus-5"
    assert request["system"] == "be brief"
    assert request["tools"][0]["input_schema"] is TOOL.parameters  # full JSON Schema, unmodified
    roles = [m["role"] for m in request["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert request["messages"][2]["content"][0]["type"] == "tool_result"
    assert completion.text == "hi"


async def test_claude_pairs_tool_results_by_id():
    client = FakeClaudeClient(FakeClaudeResponse([FakeBlock(type="text", text="hi")]))
    await ClaudeBackend(client).complete(system="s", turns=CONVERSATION, tools=[TOOL])
    result_block = client.calls[0]["messages"][2]["content"][0]
    assert result_block["tool_use_id"] == "c1"


async def test_claude_parses_a_tool_use_block():
    client = FakeClaudeClient(
        FakeClaudeResponse(
            [FakeBlock(type="tool_use", id="toolu_1", name="get_quote", input={"merchant_id": "m"})],
            stop_reason="tool_use",
        )
    )
    completion = await ClaudeBackend(client).complete(system="s", turns=[UserTurn("x")], tools=[TOOL])
    assert completion.tool_calls[0].id == "toolu_1"
    assert completion.tool_calls[0].arguments == {"merchant_id": "m"}


async def test_claude_treats_a_refusal_stop_reason_as_a_refusal():
    """A safety classifier can decline with HTTP 200, so stop_reason is checked
    before the content is trusted."""
    client = FakeClaudeClient(FakeClaudeResponse([], stop_reason="refusal"))
    completion = await ClaudeBackend(client).complete(system="s", turns=[UserTurn("x")], tools=[])
    assert completion.refused is True


def test_current_claude_models_get_adaptive_thinking_and_effort():
    config = request_config("claude-opus-5")
    assert config["thinking"] == {"type": "adaptive"}
    assert config["output_config"] == {"effort": "high"}


def test_haiku_gets_the_older_budget_form_and_no_effort():
    """Haiku 4.5 returns 400 "adaptive thinking is not supported on this model"
    and rejects effort separately. A live run found this."""
    config = request_config("claude-haiku-4-5")
    assert config["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert "output_config" not in config


def test_a_dated_snapshot_is_treated_as_its_base_model():
    assert request_config("claude-opus-5-20260401")["thinking"] == {"type": "adaptive"}


# -- the free tier being busy ------------------------------------------------


async def _ok(**_arguments) -> str:
    return "{}"


async def _no_sleep(_seconds: float) -> None:
    """Retries are tested for their decisions, not for their patience."""
    return None


async def test_a_503_is_retried_and_then_succeeds(monkeypatch):
    """Google answers 503 "high demand" on the free tier routinely.

    Before this was retried, a scene run died with a stack trace in the middle of
    a basket -- which tells a reader nothing about the firewall, and is the kind of
    failure that reads as "the project is broken".
    """
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, json={"error": {"code": 503, "message": "high demand"}})
        return reply([{"text": "fine"}])

    backend = gemini(handler, backoff=0.0)
    completion = await backend.complete(system="s", turns=[UserTurn(text="hi")], tools=[])
    assert completion.text == "fine"
    assert len(calls) == 3


async def test_a_400_is_not_retried(monkeypatch):
    """A schema this dialect refuses will be refused four times in a row.

    Retrying it spends the clock and then reports the same thing, and a 400 during
    a demo is a bug in the request, which the message should say immediately.
    """
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "Invalid JSON payload"}})

    with pytest.raises(GeminiUnavailable, match="Invalid JSON payload"):
        await gemini(handler, backoff=0.0).complete(
            system="s", turns=[UserTurn(text="hi")], tools=[]
        )
    assert len(calls) == 1


async def test_exhausted_retries_name_the_model_and_the_alternatives(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "high demand"}})

    backend = gemini(handler, attempts=2, backoff=0.0, model="gemini-3.8-flash")
    with pytest.raises(GeminiUnavailable) as caught:
        await backend.complete(system="s", turns=[UserTurn(text="hi")], tools=[])
    message = str(caught.value)
    assert "gemini-3.8-flash" in message
    assert "after 2 attempts" in message
    # The remedy belongs in the error, not in a reader's memory of a docs page.
    assert "--model" in message


async def test_a_read_timeout_reports_its_type_and_deadline(monkeypatch):
    """`str(httpx.ReadTimeout())` is empty, so the message has to be built from
    the exception's type and the configured deadline or it says nothing at all."""
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    backend = gemini(handler, attempts=2, backoff=0.0, timeout=12.0)
    with pytest.raises(GeminiUnavailable) as caught:
        await backend.complete(system="s", turns=[UserTurn(text="hi")], tools=[])
    message = str(caught.value)
    assert "ReadTimeout" in message
    assert "12s" in message


async def test_the_model_is_never_silently_swapped(monkeypatch):
    """A fallback to another model would corrupt the injection finding.

    That scene reports whether *a named model* resisted an instruction. If a 503
    could quietly produce an answer from a different model, the one result this
    project reports honestly would be unverifiable.
    """
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url.path))
        return httpx.Response(503, json={"error": {"message": "high demand"}})

    with pytest.raises(GeminiUnavailable):
        await gemini(handler, attempts=3, backoff=0.0, model="gemini-3.8-flash").complete(
            system="s", turns=[UserTurn(text="hi")], tools=[]
        )
    assert {path.rsplit("/", 1)[-1] for path in asked} == {"gemini-3.8-flash:generateContent"}


async def test_a_thought_signature_is_carried_back_on_the_next_turn():
    """Gemini 3 signs each function call and 400s if the signature is not returned.

    This is the failure that made a single-turn probe look like a working
    integration: the first request has no prior function call in it, so nothing is
    missing yet. The agent loop died on turn two, in the middle of a basket, with
    `Function call is missing a thought_signature`.
    """
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {"name": "browse_merchants", "args": {}},
                                        "thoughtSignature": "sig-abc123",
                                    }
                                ]
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2},
                },
            )
        return reply([{"text": "done"}])

    tool = AgentTool(
        name="browse_merchants",
        description="List merchants.",
        parameters={"type": "object", "properties": {}},
        run=_ok,
    )
    backend = gemini(handler)
    first = await backend.complete(system="s", turns=[UserTurn(text="hi")], tools=[tool])
    call = first.tool_calls[0]
    assert call.echo == {"thoughtSignature": "sig-abc123"}

    await backend.complete(
        system="s",
        turns=[
            UserTurn(text="hi"),
            AssistantTurn(text="", tool_calls=first.tool_calls),
            ToolResultTurn(results=((call, '{"merchants": []}'),)),
        ],
        tools=[tool],
    )
    model_turn = [c for c in sent[1]["contents"] if c["role"] == "model"][0]
    assert model_turn["parts"][0]["thoughtSignature"] == "sig-abc123"


async def test_a_call_without_a_signature_sends_no_empty_field():
    """Older models do not sign calls, and sending `thoughtSignature: null` is its
    own 400. Absent means absent."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return reply([{"text": "done"}])

    call = ToolCall(id="c1", name="browse_merchants", arguments={})
    assert call.echo == {}
    await gemini(handler).complete(
        system="s",
        turns=[UserTurn(text="hi"), AssistantTurn(text="", tool_calls=(call,))],
        tools=[],
    )
    model_turn = [c for c in sent[0]["contents"] if c["role"] == "model"][0]
    assert "thoughtSignature" not in model_turn["parts"][0]
