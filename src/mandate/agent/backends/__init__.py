"""One adapter per provider, behind a single `Backend` protocol.

`choose()` is the only thing most callers need. It prefers whichever provider is
actually configured, which matters in practice: a key can run out of credit
mid-project, and the answer to that should be a flag rather than a rewrite.
"""

from __future__ import annotations

import os

from ..loop import Backend
from .claude import DEFAULT_MODEL as CLAUDE_DEFAULT_MODEL
from .claude import ClaudeBackend
from .gemini import DEFAULT_MODEL as GEMINI_DEFAULT_MODEL
from .gemini import FREE_TIER_MODELS, GeminiBackend, GeminiUnavailable

__all__ = [
    "CLAUDE_DEFAULT_MODEL",
    "FREE_TIER_MODELS",
    "GEMINI_DEFAULT_MODEL",
    "Backend",
    "ClaudeBackend",
    "GeminiBackend",
    "GeminiUnavailable",
    "choose",
    "configured_providers",
    "provider_of",
]


def configured_providers() -> list[str]:
    """Providers with a key in the environment, in preference order."""
    found = []
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        found.append("gemini")
    if os.environ.get("ANTHROPIC_API_KEY"):
        found.append("claude")
    return found


#: A model name names its provider, so it is allowed to decide which one is used.
PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude-", "claude"),
    ("gemini-", "gemini"),
)


def provider_of(model: str) -> str | None:
    """Which provider serves this model, by name. None if the name says nothing."""
    name = (model or "").strip().lower()
    for prefix, provider in PREFIXES:
        if name.startswith(prefix):
            return provider
    return None


def choose(provider: str | None = None, *, model: str | None = None) -> Backend:
    """Build a backend from the environment.

    Precedence, strongest first:

      1. an explicit `provider` argument -- a caller that names one means it
      2. the model's own name, when it identifies a provider
      3. MANDATE_PROVIDER
      4. the first provider with a key

    The model name outranks MANDATE_PROVIDER because it has to. Asking for
    `claude-haiku-4-5` while MANDATE_PROVIDER says gemini used to send that name
    to Gemini, which answers 404, and the gateway's shortlist treats any failure
    as "no model" and silently falls back to keyword matching. The result was a
    shortlist that quietly got worse on exactly the machines that are set up
    best -- both keys present, a provider pinned for the agent scenes -- and said
    nothing about why. Nobody writes a Claude model name meaning Gemini.
    """
    explicit = (provider or "").strip().lower()
    inferred = provider_of(model or "")
    provider = explicit or inferred or (os.environ.get("MANDATE_PROVIDER") or "").strip().lower()
    available = configured_providers()

    if not provider:
        if not available:
            raise RuntimeError(
                "No model provider configured. Set one of:\n"
                "  GEMINI_API_KEY     free tier, no card: https://aistudio.google.com/apikey\n"
                "  ANTHROPIC_API_KEY  https://console.anthropic.com/settings/keys\n"
                "Then re-source .env."
            )
        provider = available[0]

    if inferred and not explicit and provider != inferred:
        # Unreachable through the precedence above, and asserted rather than
        # trusted: the one bug this function has had was the model and the
        # provider disagreeing without anyone noticing.
        raise RuntimeError(f"model {model!r} is served by {inferred}, not {provider}")

    if provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey"
            )
        return GeminiBackend(key, model=model or os.environ.get("GEMINI_MODEL") or GEMINI_DEFAULT_MODEL)

    if provider == "claude":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        import anthropic

        return ClaudeBackend(
            anthropic.AsyncAnthropic(),
            model=model or os.environ.get("ANTHROPIC_MODEL") or CLAUDE_DEFAULT_MODEL,
        )

    raise RuntimeError(f"unknown provider {provider!r}; expected 'gemini' or 'claude'")
