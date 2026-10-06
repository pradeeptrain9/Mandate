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
]


def configured_providers() -> list[str]:
    """Providers with a key in the environment, in preference order."""
    found = []
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        found.append("gemini")
    if os.environ.get("ANTHROPIC_API_KEY"):
        found.append("claude")
    return found


def choose(provider: str | None = None, *, model: str | None = None) -> Backend:
    """Build a backend from the environment.

    With no `provider`, takes `MANDATE_PROVIDER` if set, else the first provider
    with a key. Raises with the exact variable to set when none is configured --
    "no API key" is a useless error message when three would do.
    """
    provider = (provider or os.environ.get("MANDATE_PROVIDER") or "").strip().lower()
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
