"""Gemini, over raw HTTP against generateContent.

No SDK. The call is one POST and the shape is stable, and AgriN already proves
this pattern works in anger (`agrin/backend/app/ai/gemini.py`), so a dependency
would buy nothing.

Three dialect differences cost real time if you meet them at runtime instead of
reading them here:

  * **Schema.** `functionDeclarations[].parameters` is an OpenAPI subset, not JSON
    Schema. `additionalProperties`, `$schema`, `$defs` and friends are rejected
    outright -- a 400 for the whole request, not a warning. `sanitise_schema`
    strips them. A type union like `["string", "null"]` is also refused, and
    becomes `nullable: true`.
  * **Roles.** There is no `assistant`; the model's turn is `model`. A role Gemini
    does not recognise is a 400.
  * **Tool results.** They go back as a `functionResponse` part keyed by function
    *name*, not by call id, and `response` must be a JSON object -- a bare string
    is refused. So a tool returning JSON text is wrapped.

`promptTokenCount` already includes cached tokens, so the cached half is
subtracted out rather than billed twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import httpx

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

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

#: Free-tier models that support function calling, best first. Google stopped
#: publishing per-model free-tier limits in September 2026, so the live quota in
#: AI Studio is the only authority; these are ordered by capability, and
#: `discover_model` picks the first one the key can actually reach.
logger = logging.getLogger(__name__)

#: Statuses that mean "the shared free tier is busy", not "your request is wrong".
#: 429 is in here because Google returns it for a per-minute rate limit as well as
#: for a spent daily quota, and the two are indistinguishable from the status
#: alone; a spent quota costs four pauses and then says so plainly.
_RETRY_STATUSES = frozenset({429, 500, 503, 504})

FREE_TIER_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)

DEFAULT_MODEL = FREE_TIER_MODELS[0]

#: Keys Gemini's schema dialect rejects. Taken from AgriN, which met each of them
#: the hard way.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "$schema",
        "additionalProperties",
        "definitions",
        "$defs",
        "$ref",
        "patternProperties",
        "const",
        "examples",
        "default",
        "title",
    }
)

_REFUSAL_REASONS = frozenset({"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"})


class GeminiUnavailable(RuntimeError):
    """Transport, auth or quota. Nothing was generated."""


def sanitise_schema(schema: Any) -> Any:
    """Strip what Gemini's dialect will reject, recursively."""
    if isinstance(schema, dict):
        out = {
            key: sanitise_schema(value)
            for key, value in schema.items()
            if key not in _UNSUPPORTED_SCHEMA_KEYS
        }
        kind = out.get("type")
        if isinstance(kind, list):
            concrete = [t for t in kind if t != "null"]
            out["type"] = concrete[0] if concrete else "string"
            if len(concrete) != len(kind):
                out["nullable"] = True
        return out
    if isinstance(schema, list):
        return [sanitise_schema(item) for item in schema]
    return schema


def _declarations(tools: list[AgentTool]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": sanitise_schema(tool.parameters),
        }
        for tool in tools
    ]


def _contents(turns: list[Turn]) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    for turn in turns:
        if isinstance(turn, UserTurn):
            contents.append({"role": "user", "parts": [{"text": turn.text}]})
        elif isinstance(turn, AssistantTurn):
            parts: list[dict[str, Any]] = []
            if turn.text:
                parts.append({"text": turn.text})
            for call in turn.tool_calls:
                parts.append({"functionCall": {"name": call.name, "args": call.arguments}})
            # A turn with no parts at all is a 400, so a silent assistant turn is
            # skipped rather than sent empty.
            if parts:
                contents.append({"role": "model", "parts": parts})
        elif isinstance(turn, ToolResultTurn):
            parts = []
            for call, result in turn.results:
                parts.append(
                    {
                        "functionResponse": {
                            "name": call.name,
                            # `response` must be an object. A tool that returned
                            # JSON text is unwrapped so the model sees structure;
                            # anything else is wrapped under a key.
                            "response": _as_object(result),
                        }
                    }
                )
            if parts:
                contents.append({"role": "user", "parts": parts})
    return contents


def _as_object(result: str) -> dict[str, Any]:
    try:
        parsed = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return {"result": result}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _usage(payload: dict[str, Any]) -> Usage:
    meta = payload.get("usageMetadata") or {}

    def count(name: str) -> int:
        try:
            return int(meta.get(name, 0) or 0)
        except (TypeError, ValueError):
            return 0

    cached = count("cachedContentTokenCount")
    prompt = count("promptTokenCount")
    return Usage(
        # promptTokenCount already includes the cached tokens; billing both would
        # charge the cheap half twice.
        input_tokens=max(0, prompt - cached),
        output_tokens=count("candidatesTokenCount") + count("thoughtsTokenCount"),
        cache_read_tokens=cached,
    )


class GeminiBackend:
    """One POST per turn. Holds no state beyond the key, model and HTTP client."""

    provider = "gemini"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.2,
        max_output_tokens: int = 4096,
        # Generous on purpose. The free tier queues, and a tool-calling turn on
        # gemini-3.8-flash has been measured past ninety seconds -- which is what
        # the first value here was, so every scene run died in the middle of a
        # basket. A slow answer is still an answer; the only thing a short
        # timeout bought was a misleading failure.
        timeout: float = 240.0,
        # The free tier answers 503 "high demand" and 429 "quota" often enough
        # that a single attempt is not a usable demo: a judge who runs a scene and
        # sees a stack trace learns nothing about the firewall. Retried here
        # rather than in the agent loop, because the loop's job is to be
        # provider-neutral and this is a property of one provider's free tier.
        attempts: int = 4,
        backoff: float = 4.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("a Gemini API key is required; get a free one at aistudio.google.com")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self._timeout = timeout
        self._attempts = max(1, attempts)
        self._backoff = backoff
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        """One generateContent call, retried on the failures that pass.

        Which failures are worth retrying is the whole content of this method:

          * **503 / 429 / 500 / 504, and read timeouts** -- the shared free tier
            being busy. Temporary by construction, and the message Google returns
            says so.
          * **400, 403, 404** -- a schema this dialect refuses, a key without the
            API enabled, a withdrawn model. Retrying these burns the clock and
            then reports the same thing, so they raise at once.

        The model is never silently swapped for another on failure. It is
        tempting -- `FREE_TIER_MODELS` is right there -- but the injection scene
        measures whether *a named model* resisted an instruction, and a fallback
        that quietly answered as a different model would corrupt the one finding
        this project reports honestly. A caller who wants a different model passes
        `--model`.
        """
        last = ""
        for attempt in range(1, self._attempts + 1):
            try:
                response = await self._http.post(
                    f"{BASE_URL}/{self.model}:generateContent",
                    params={"key": self.api_key},
                    json=body,
                )
            except httpx.HTTPError as exc:
                # `str(httpx.ReadTimeout())` is the empty string, so the obvious
                # f-string here produced "Gemini unreachable: " and told the
                # reader nothing at all -- the failure most likely to happen on a
                # free tier was the one the message could not name. The exception
                # type and the deadline are both always present.
                last = (
                    f"{type(exc).__name__} calling {self.model}"
                    f" (timeout {self._timeout:g}s): {exc or 'no detail'}"
                )
                if not isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
                    raise GeminiUnavailable(f"Gemini {last}") from exc
            else:
                if response.status_code == 200:
                    return response
                detail = response.text[:400]
                last = f"HTTP {response.status_code}: {detail}"
                if response.status_code not in _RETRY_STATUSES:
                    raise GeminiUnavailable(f"Gemini request failed ({last})")

            if attempt < self._attempts:
                pause = self._backoff * (2 ** (attempt - 1))
                logger.warning(
                    "gemini %s unavailable (%s); retrying in %.0fs (attempt %d/%d)",
                    self.model,
                    last.split(":")[0],
                    pause,
                    attempt + 1,
                    self._attempts,
                )
                await asyncio.sleep(pause)

        raise GeminiUnavailable(
            f"Gemini {self.model} still unavailable after {self._attempts} attempts. "
            f"Last: {last}. Free-tier capacity moves around; try another model with "
            f"--model (reachable ones: {', '.join(FREE_TIER_MODELS)})."
        )

    async def complete(
        self, *, system: str, turns: list[Turn], tools: list[AgentTool]
    ) -> Completion:
        body: dict[str, Any] = {
            "contents": _contents(turns),
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_output_tokens,
            },
        }
        if tools:
            body["tools"] = [{"functionDeclarations": _declarations(tools)}]

        response = await self._post(body)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GeminiUnavailable(f"Gemini returned non-JSON: {exc}") from exc

        candidates = payload.get("candidates") or []
        if not candidates:
            feedback = payload.get("promptFeedback") or {}
            return Completion(
                text="",
                tool_calls=(),
                usage=_usage(payload),
                model=payload.get("modelVersion") or self.model,
                stop_reason=str(feedback.get("blockReason") or "no_candidates"),
                refused=True,
            )

        candidate = candidates[0]
        finish = str(candidate.get("finishReason") or "")
        parts = (candidate.get("content") or {}).get("parts") or []

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if "text" in part and part["text"]:
                text_parts.append(str(part["text"]))
            call = part.get("functionCall")
            if isinstance(call, dict) and call.get("name"):
                calls.append(
                    ToolCall(
                        # Gemini pairs a result to a call by name, not id, so an
                        # id is minted locally to keep the neutral type honest.
                        id=f"gemini-{uuid.uuid4().hex[:12]}",
                        name=str(call["name"]),
                        arguments=dict(call.get("args") or {}),
                    )
                )

        return Completion(
            text="\n".join(text_parts).strip(),
            tool_calls=tuple(calls),
            usage=_usage(payload),
            model=payload.get("modelVersion") or self.model,
            stop_reason=finish,
            refused=finish in _REFUSAL_REASONS,
        )

    # -- discovery -------------------------------------------------------

    async def available_models(self) -> list[dict[str, Any]]:
        """Models this key can reach that support generateContent.

        Worth asking rather than assuming: Google withdraws model names, and a
        404 naming a replacement is more useful than a guess from a docs page
        that may be months stale.
        """
        response = await self._http.get(f"{BASE_URL}", params={"key": self.api_key})
        if response.status_code != 200:
            raise GeminiUnavailable(
                f"could not list models ({response.status_code}): {response.text[:300]}"
            )
        out = []
        for model in response.json().get("models") or []:
            methods = model.get("supportedGenerationMethods") or []
            if "generateContent" in methods:
                out.append(
                    {
                        "name": str(model.get("name", "")).removeprefix("models/"),
                        "display_name": model.get("displayName"),
                        "input_token_limit": model.get("inputTokenLimit"),
                        "output_token_limit": model.get("outputTokenLimit"),
                    }
                )
        return out

    async def discover_model(self) -> str:
        """The best free-tier model this key can actually reach."""
        reachable = {model["name"] for model in await self.available_models()}
        for candidate in FREE_TIER_MODELS:
            if candidate in reachable:
                return candidate
        # Nothing from the preference list: fall back to anything flash-shaped
        # rather than failing, and let the caller see what was chosen.
        flashes = sorted(name for name in reachable if "flash" in name)
        if flashes:
            return flashes[0]
        raise GeminiUnavailable(
            f"no generateContent model reachable with this key; saw {sorted(reachable)[:10]}"
        )
