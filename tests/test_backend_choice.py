"""Which provider a model name goes to.

The bug this covers was silent and only appeared on well-configured machines. The
gateway shortlists with `claude-haiku-4-5` whenever ANTHROPIC_API_KEY is set, and
`choose` picked the provider from whichever key happened to be first -- so on a
box with both keys, or with MANDATE_PROVIDER pinned to gemini for the agent
scenes, that Claude model name was sent to Gemini. Gemini answers 404, the
shortlist treats any model failure as "no model" and falls back to keyword
matching, and the person sees a worse list with no indication why.

It worked in production purely because Render has one key set.
"""

from __future__ import annotations

import pytest

from mandate.agent.backends import ClaudeBackend, GeminiBackend, choose, provider_of


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for var in (
        "MANDATE_PROVIDER", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY", "GEMINI_MODEL", "ANTHROPIC_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


def both(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")


# -- the name decides -------------------------------------------------------


def test_a_claude_model_name_goes_to_claude_even_when_gemini_is_first(monkeypatch):
    """The exact case that was broken. Gemini sorts first in configured_providers."""
    both(monkeypatch)
    backend = choose(model="claude-haiku-4-5-20251001")
    assert isinstance(backend, ClaudeBackend)
    assert backend.model == "claude-haiku-4-5-20251001"


def test_a_gemini_model_name_goes_to_gemini_even_when_only_claude_is_pinned(monkeypatch):
    both(monkeypatch)
    monkeypatch.setenv("MANDATE_PROVIDER", "claude")
    assert isinstance(choose(model="gemini-3.6-flash"), GeminiBackend)


def test_the_model_name_outranks_mandate_provider(monkeypatch):
    """A pinned provider for the agent scenes must not misroute the shortlist."""
    both(monkeypatch)
    monkeypatch.setenv("MANDATE_PROVIDER", "gemini")
    assert isinstance(choose(model="claude-haiku-4-5-20251001"), ClaudeBackend)


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-opus-5", "claude"),
        ("claude-haiku-4-5-20251001", "claude"),
        ("gemini-3.6-flash", "gemini"),
        ("GEMINI-3.6-FLASH", "gemini"),
        ("", None),
        ("some-local-model", None),
        ("llama-3", None),
    ],
)
def test_provider_of_reads_the_prefix(model, expected):
    assert provider_of(model) == expected


# -- what still wins --------------------------------------------------------


def test_an_explicit_provider_argument_still_wins(monkeypatch):
    """A caller naming a provider means it, even against the model name.

    Kept deliberately: this is the escape hatch for a gateway or proxy serving one
    vendor's model names on another's endpoint.
    """
    both(monkeypatch)
    assert isinstance(choose("gemini", model="claude-haiku-4-5-20251001"), GeminiBackend)


def test_mandate_provider_still_decides_when_the_name_says_nothing(monkeypatch):
    both(monkeypatch)
    monkeypatch.setenv("MANDATE_PROVIDER", "claude")
    assert isinstance(choose(), ClaudeBackend)


def test_with_no_model_and_no_pin_the_first_configured_provider_is_used(monkeypatch):
    both(monkeypatch)
    assert isinstance(choose(), GeminiBackend)


# -- the errors are actionable ----------------------------------------------


def test_a_model_whose_provider_has_no_key_names_the_variable(monkeypatch):
    """Asking for Claude with only a Gemini key must say so, not fall through to
    Gemini and 404 later."""
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        choose(model="claude-haiku-4-5-20251001")


def test_no_keys_at_all_lists_every_variable_that_would_work(monkeypatch):
    with pytest.raises(RuntimeError) as caught:
        choose()
    message = str(caught.value)
    assert "GEMINI_API_KEY" in message
    assert "ANTHROPIC_API_KEY" in message
