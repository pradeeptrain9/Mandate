"""Both backends against the same protocol, and the dialect traps each one has.

These matter because the point of a provider-neutral loop is that the firewall's
behaviour does not depend on which model you shipped. That claim is only worth
anything if both adapters genuinely work, and each provider has at least one
difference that is a 400 rather than a warning.
"""

from __future__ import annotations

import asyncio
import json
import time

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
    assert "did not answer" in message
    # The remedy belongs in the error, not in a reader's memory of a docs page --
    # including the remedy that needs no model at all, since two scenes do not.
    assert "--model" in message
    assert "stolen-credentials" in message
    assert "paid key" in message


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


async def test_requests_are_paced_to_stay_inside_the_free_tier_window(monkeypatch):
    """Five requests a minute is the free tier's limit, and a turn is a request.

    A five-step basket hit it on the last step, and retrying made it worse: each
    retry spends another request from the same window. So the sixth request waits
    rather than being refused.
    """
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", record)
    backend = gemini(lambda r: reply([{"text": "ok"}]), rpm=3)
    for _ in range(5):
        await backend.complete(system="s", turns=[UserTurn(text="hi")], tools=[])

    # Three fit in the window; the fourth and fifth each wait for one to age out.
    assert len(slept) == 2
    assert all(0 < pause <= 62.0 for pause in slept)


async def test_rpm_zero_disables_pacing():
    """A paid key has a different limit, and the pacer should not be the thing
    that makes it slow."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return reply([{"text": "ok"}])

    backend = gemini(handler, rpm=0)
    for _ in range(8):
        await backend.complete(system="s", turns=[UserTurn(text="hi")], tools=[])
    assert len(calls) == 8


async def test_a_429_waits_for_the_window_not_the_backoff(monkeypatch):
    """Four seconds does not refill a per-minute quota."""
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", record)
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        if len(sent) == 1:
            return httpx.Response(
                429,
                json={
                    "error": {
                        "code": 429,
                        "message": "Quota exceeded",
                        "details": [{"retryDelay": "41s"}],
                    }
                },
            )
        return reply([{"text": "ok"}])

    backend = gemini(handler, backoff=4.0, rpm=0)
    completion = await backend.complete(system="s", turns=[UserTurn(text="hi")], tools=[])
    assert completion.text == "ok"
    # Google's own RetryInfo wins over the backoff schedule: it is the only thing
    # that knows whether the spent window was the minute's or the day's.
    assert slept == [41.0]


async def test_a_429_without_retry_info_waits_a_full_window(monkeypatch):
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", record)
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        if len(sent) == 1:
            return httpx.Response(429, json={"error": {"message": "Quota exceeded"}})
        return reply([{"text": "ok"}])

    await gemini(handler, backoff=4.0, rpm=0).complete(
        system="s", turns=[UserTurn(text="hi")], tools=[]
    )
    assert slept == [62.0]


async def test_working_model_skips_the_ones_that_do_not_answer(monkeypatch):
    """The default has been found 503 while three others answered.

    So the runner asks rather than assuming -- and the result is announced, because
    a model chosen behind the reader's back makes the injection finding
    unverifiable.
    """
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        model = str(request.url.path).rsplit("/", 1)[-1].split(":")[0]
        if model == "gemini-3.8-flash":
            return httpx.Response(503, json={"error": {"message": "high demand"}})
        if model == "gemini-3.7-flash":
            return httpx.Response(404, json={"error": {"message": "no longer available"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "x"}]}}]})

    model, skipped = await gemini(handler, rpm=0).working_model()
    assert model == "gemini-3.6-flash"
    assert len(skipped) == 2
    assert "503" in skipped[0] and "high demand" in skipped[0]
    assert "404" in skipped[1]


async def test_working_model_reports_every_failure_when_none_answer(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "high demand"}})

    with pytest.raises(GeminiUnavailable) as caught:
        await gemini(handler, rpm=0).working_model()
    message = str(caught.value)
    for name in FREE_TIER_MODELS:
        assert name in message


def test_the_withdrawn_models_are_not_in_the_preference_list():
    """Both 2.5 models are listed by the models endpoint and both 404 for a new
    key. Keeping them cost three wasted requests and a misleading error."""
    assert "gemini-2.5-flash" not in FREE_TIER_MODELS
    assert "gemini-2.5-flash-lite" not in FREE_TIER_MODELS


async def test_the_backoff_is_capped_and_jittered(monkeypatch):
    """Eight attempts must not mean forty minutes, and must not be in lockstep.

    Capped because the point of more attempts is surviving a saturated free tier,
    not waiting out a doubling schedule. Jittered because every client retrying on
    the same schedule arrives back together and re-creates the spike it is backing
    off from.
    """
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", record)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "high demand"}})

    with pytest.raises(GeminiUnavailable):
        await gemini(handler, attempts=8, backoff=3.0, backoff_cap=30.0, rpm=0).complete(
            system="s", turns=[UserTurn(text="hi")], tools=[]
        )
    assert len(slept) == 7
    assert max(slept) <= 30.0 * 1.3
    # Jitter means no two pauses at the cap are identical.
    at_cap = [pause for pause in slept if pause > 30.0 * 0.9]
    assert len(set(at_cap)) == len(at_cap)
    # Eight attempts is minutes, not an afternoon.
    assert sum(slept) < 180.0


async def test_one_request_gives_up_on_a_deadline(monkeypatch):
    """Retries and pacing multiply, and that is what made this necessary.

    Eight attempts against a saturated free tier produced a single turn grinding
    for thirteen minutes: each retry waits for a rate-limit slot *and then* backs
    off. A count alone cannot bound that, because the pacing is not in the count.
    """
    elapsed = [0.0]

    async def advance(seconds: float) -> None:
        elapsed[0] += seconds

    monkeypatch.setattr(asyncio, "sleep", advance)
    monkeypatch.setattr(time, "monotonic", lambda: elapsed[0])

    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        return httpx.Response(503, json={"error": {"message": "high demand"}})

    backend = gemini(handler, attempts=99, backoff=20.0, backoff_cap=20.0, deadline=60.0, rpm=0)
    with pytest.raises(GeminiUnavailable, match="gave up after"):
        await backend.complete(system="s", turns=[UserTurn(text="hi")], tools=[])
    # Stopped on the clock, not on the count: 99 attempts were allowed.
    assert len(sent) < 10


async def test_an_attempt_cannot_outlive_the_deadline(monkeypatch):
    """A deadline checked only between attempts bounds nothing.

    The first version of this let a 240s read timeout sail past a 150s deadline:
    two attempts cost eight minutes, and a turn sat silent with nothing in the log
    to say why. Each attempt now gets at most the time that is left.
    """
    timeouts: list[float] = []
    elapsed = [0.0]

    async def advance(seconds: float) -> None:
        elapsed[0] += seconds

    monkeypatch.setattr(asyncio, "sleep", advance)
    monkeypatch.setattr(time, "monotonic", lambda: elapsed[0])

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        # Spend the whole allowance, the way a read timeout would.
        elapsed[0] += timeouts[-1]
        raise httpx.ReadTimeout("", request=request)

    backend = gemini(handler, attempts=9, backoff=0.0, deadline=100.0, timeout=240.0, rpm=0)
    with pytest.raises(GeminiUnavailable):
        await backend.complete(system="s", turns=[UserTurn(text="hi")], tools=[])

    # First attempt is capped at the deadline, not at the 240s transport timeout.
    assert timeouts[0] == 100.0
    # And the whole thing stops near the deadline rather than at 9 x 240s.
    assert elapsed[0] <= 120.0
