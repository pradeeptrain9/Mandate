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
import random
import re
import time
import uuid
from collections import deque
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

#: The free tier counts requests over a trailing minute.
_WINDOW_SECONDS = 62.0


def _is_daily_quota(body: str) -> bool:
    """Whether a 429 means the day is spent rather than the minute.

    Both arrive as 429 on the same metric name, and the difference decides whether
    retrying is sensible or futile. A per-minute window refills in a minute; a
    daily one does not refill today, and four 62-second waits against it is four
    minutes of a demo spent learning nothing. Observed:

      Quota exceeded for metric: generate_content_free_tier_requests, limit: 5
      Quota exceeded for metric: generate_content_free_tier_requests, limit: 20

    The metric is identical. Google's quotaId is what separates them --
    `...PerDayPerProjectPerModel-FreeTier` against `...PerMinute...` -- so that is
    what is matched, with the prose form as a fallback for when the structured
    field is absent.
    """
    lowered = body.lower()
    return "perday" in lowered or "per day" in lowered


def _retry_delay(body: str) -> float | None:
    """Google's RetryInfo, when the 429 carries one.

    Preferred over a guess: it is the only thing that knows whether the limit that
    was hit was the per-minute one or the daily one, and a daily quota should not
    be retried in a minute.
    """
    match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body)
    return float(match.group(1)) if match else None


#: Preference order, best first. **A name here is not a promise**, and the list is
#: shorter than it was for a measured reason.
#:
#: `gemini-2.5-flash` and `gemini-2.5-flash-lite` were in it. Both are listed by
#: the models endpoint with `generateContent` among their supported methods, and
#: both answer `404 ... is no longer available to new users`. Metadata is not
#: capability, so they are gone: a preference list whose tail cannot be reached by
#: any new key is not a fallback, it is three wasted requests and a misleading
#: error message.
#:
#: The live ones move around. A probe at one moment had 3.8 returning 503 "high
#: demand" while 3.7, 3.6 and 3.5 all answered, which is why `working_model()`
#: exists and why nothing here assumes the first entry is available.
FREE_TIER_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
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
                part: dict[str, Any] = {
                    "functionCall": {"name": call.name, "args": call.arguments}
                }
                # Gemini 3 signs each function call and refuses the next request
                # unless the signature comes back with it. Dropping it is a 400 on
                # the *second* turn, which is why a single-turn probe passed while
                # every real agent run died in the middle of a basket.
                signature = call.echo.get("thoughtSignature")
                if signature:
                    part["thoughtSignature"] = signature
                parts.append(part)
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
        # Retries and pacing multiply, and that is the trap here. Eight attempts
        # was tried first, on the reasoning that three turns had each succeeded on
        # their second attempt. What it actually produced was a single turn
        # grinding for thirteen minutes: every retry waits for a rate-limit slot
        # *and then* backs off, so eight attempts cost 8 x (62s + 30s).
        #
        # Worse, retrying is self-defeating on this tier. Each retry spends one of
        # the five requests a minute, so a turn that retries hard guarantees the
        # next turn waits. Hence a deadline on the whole request rather than a
        # count alone: fail in two and a half minutes with a message that explains
        # the arithmetic, instead of hanging.
        attempts: int = 5,
        backoff: float = 3.0,
        backoff_cap: float = 20.0,
        # Bounds the whole request including its retries. Each attempt is also
        # given no more than the time left, because a deadline checked only between
        # attempts does not bound anything: the first version of this let a 240s
        # read timeout sail straight past a 150s deadline, and a turn sat silent
        # for minutes with nothing in the log to say why.
        deadline: float = 300.0,
        # The free tier allows five generateContent calls a minute per model, and
        # an agent turn is one call. A five-step basket therefore hits the limit
        # on its last step -- which is exactly what happened: four tools, then
        # `429 Quota exceeded for metric: generate_content_free_tier_requests,
        # limit: 5`. Retrying into a per-minute window does not help, because each
        # retry spends another request from the same window. So the requests are
        # paced instead, and a scene takes the time it takes.
        rpm: int = 5,
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
        self._backoff_cap = backoff_cap
        self._deadline = deadline
        self._rpm = max(0, rpm)
        self._sent: deque[float] = deque(maxlen=max(1, self._rpm))
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
        started = time.monotonic()
        for attempt in range(1, self._attempts + 1):
            remaining = self._deadline - (time.monotonic() - started)
            if attempt > 1 and remaining <= 0:
                last = f"{last} (gave up after {time.monotonic() - started:.0f}s)"
                break
            await self._wait_for_a_slot()
            # Pacing counts against the deadline too, so this is recomputed after
            # the wait rather than before it.
            remaining = self._deadline - (time.monotonic() - started)
            try:
                response = await self._http.post(
                    f"{BASE_URL}/{self.model}:generateContent",
                    params={"key": self.api_key},
                    json=body,
                    timeout=min(self._timeout, max(5.0, remaining)),
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
                if response.status_code == 429 and _is_daily_quota(detail):
                    # Nothing to wait for. This one is not transient today.
                    raise GeminiUnavailable(
                        f"Gemini {self.model}: the free tier's *daily* quota for this "
                        f"model is spent, so retrying will not help today.\n{last}\n\n"
                        f"Each model has its own daily allowance, so another one very "
                        f"likely still works: --model "
                        f"{' | '.join(m for m in FREE_TIER_MODELS if m != self.model)}\n"
                        f"Or run a scene that needs no model at all: "
                        f"stolen-credentials, duplicate."
                    )

            if attempt < self._attempts:
                pause = min(self._backoff_cap, self._backoff * (2 ** (attempt - 1)))
                # Jitter, because every client retrying a shared free tier on the
                # same doubling schedule arrives back in lockstep and re-creates
                # the spike it is backing off from.
                pause *= 1.0 + random.random() * 0.3
                if "429" in last:
                    # A spent per-minute window needs the window to pass, and
                    # four seconds does not. Google's own retryDelay is used when
                    # it sends one, because it knows which window was spent.
                    pause = max(pause, _retry_delay(last) or _WINDOW_SECONDS)
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
            f"Gemini {self.model} did not answer in {time.monotonic() - started:.0f}s. "
            f"Last: {last}\n\n"
            f"The free tier allows {self._rpm or 'unlimited'} requests a minute and is "
            f"answering 503 under load, and a retry spends one of those requests -- so "
            f"retrying harder makes an agent run slower, not more likely to finish. "
            f"Options, in order of how well they work:\n"
            f"  * try another model now, since capacity moves: "
            f"--model {' | '.join(FREE_TIER_MODELS)} (names this code prefers, not a "
            f"promise any works -- the models endpoint lists some that 404)\n"
            f"  * run a scene that needs no model at all: stolen-credentials, duplicate\n"
            f"  * use a paid key, where rpm is high enough for a tool loop "
            f"(pass rpm=0 to stop pacing)"
        )

    async def _wait_for_a_slot(self) -> None:
        """Hold the next request until the free tier's window has room.

        A sliding window rather than a fixed one: the limit is counted over the
        trailing minute, and a fixed bucket would let five requests at 0:59 and
        five more at 1:01 through, which is the shape that produces the 429 this
        exists to avoid.
        """
        if not self._rpm:
            return
        now = time.monotonic()
        while len(self._sent) == self._sent.maxlen:
            oldest = self._sent[0]
            wait = (oldest + _WINDOW_SECONDS) - now
            if wait <= 0:
                self._sent.popleft()
                break
            logger.info(
                "pacing gemini: %d requests in the last minute, waiting %.0fs",
                len(self._sent),
                wait,
            )
            await asyncio.sleep(wait)
            now = time.monotonic()
            self._sent.popleft()
        self._sent.append(now)

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
                        # Carried, not interpreted. See ToolCall.echo.
                        echo=(
                            {"thoughtSignature": part["thoughtSignature"]}
                            if part.get("thoughtSignature")
                            else {}
                        ),
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

    async def working_model(self) -> tuple[str, list[str]]:
        """The first preferred model that actually answers, and why the others did not.

        One token each, the smallest request the API accepts, asked in preference
        order and stopped at the first success. That costs real requests, and the
        alternative was worse: the default model has been found returning 503
        "high demand" while three others answered, so a run that trusted the
        default failed for a reason that had nothing to do with this project.

        The chosen model is returned to the caller to *announce*, not to swap in
        quietly. `_post` never changes model on failure: the injection scene
        measures whether a named model resisted an instruction, and an answer that
        might have come from a different model than the one printed would make that
        finding worthless. Choosing up front, out loud, keeps one model for the
        whole run.
        """
        body = {
            "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
            "generationConfig": {"maxOutputTokens": 1, "temperature": 0},
        }
        notes: list[str] = []
        for candidate in FREE_TIER_MODELS:
            await self._wait_for_a_slot()
            try:
                response = await self._http.post(
                    f"{BASE_URL}/{candidate}:generateContent",
                    params={"key": self.api_key},
                    json=body,
                )
            except httpx.HTTPError as exc:
                notes.append(f"{candidate}: {type(exc).__name__}")
                continue
            if response.status_code == 200:
                return candidate, notes
            detail = ""
            try:
                detail = ((response.json() or {}).get("error") or {}).get("message", "")
            except ValueError:
                detail = response.text[:120]
            notes.append(f"{candidate}: {response.status_code} {detail[:70]}")
        raise GeminiUnavailable(
            "no preferred Gemini model answered generateContent. " + "; ".join(notes)
        )

    async def discover_model(self) -> str:
        """The best free-tier model this key is *listed* as able to reach.

        Not a health check, and the distinction has already cost a run. The models
        endpoint lists `gemini-2.5-flash` with `generateContent` among its
        supported methods, and `generateContent` then answers 404 "no longer
        available to new users". A GET is free and tells you about metadata; only a
        generate call tells you about capability, and this method deliberately does
        not spend one.
        """
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
