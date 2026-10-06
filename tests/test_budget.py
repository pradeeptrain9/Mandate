"""Model spend: priced from reported usage, capped before the call."""

from __future__ import annotations

import pytest

from mandate.agent.budget import (
    FALLBACK_RATE,
    RATES,
    BudgetReached,
    SpendLedger,
    Usage,
    price,
    rate_for,
)


def test_known_models_are_priced_from_the_table():
    usage = Usage(input_tokens=1_000_000, output_tokens=0)
    assert price(usage, "claude-opus-5") == pytest.approx(5.00)
    assert price(Usage(output_tokens=1_000_000), "claude-opus-5") == pytest.approx(25.00)
    assert price(usage, "claude-haiku-4-5") == pytest.approx(1.00)


def test_a_dated_snapshot_prefix_matches_its_base_model():
    assert rate_for("claude-opus-5-20260401") is RATES["claude-opus-5"]


def test_an_unknown_model_is_priced_at_the_dearest_known_rate():
    """Pricing an unlisted model at zero would mean the cap never binds. Over-
    pricing makes it bind early, which is the safe direction."""
    assert rate_for("claude-something-nobody-added") is FALLBACK_RATE
    assert FALLBACK_RATE.output_usd == max(r.output_usd for r in RATES.values())


def test_cache_tokens_are_priced_at_their_own_multipliers():
    rate = RATES["claude-opus-5"]
    assert rate.cache_write_usd == pytest.approx(rate.input_usd * 1.25)
    assert rate.cache_read_usd == pytest.approx(rate.input_usd * 0.10)
    usage = Usage(cache_read_input_tokens=1_000_000)
    assert price(usage, "claude-opus-5") == pytest.approx(0.50)


def test_usage_is_read_from_an_sdk_object_or_a_dict():
    class SDKUsage:
        input_tokens = 100
        output_tokens = 20
        cache_read_input_tokens = 7

    from_object = Usage.from_response(SDKUsage())
    from_dict = Usage.from_response(
        {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 7}
    )
    assert from_object == from_dict
    assert (from_object.input_tokens, from_object.output_tokens) == (100, 20)
    # A field the SDK object never mentioned, read as zero rather than guessed.
    assert from_object.cache_creation_input_tokens == 0


def test_absent_usage_fields_are_read_as_zero_not_guessed():
    usage = Usage.from_response({"input_tokens": 50})
    assert usage.output_tokens == 0
    assert usage.cache_creation_input_tokens == 0


# -- the ledger -------------------------------------------------------------


def test_spend_accumulates_and_persists(tmp_path):
    path = tmp_path / "spend.jsonl"
    first = SpendLedger(path=path, cap_usd=5.0)
    first.record(model="claude-opus-5", usage={"input_tokens": 100_000}, label="turn-1")
    first.record(model="claude-opus-5", usage={"output_tokens": 10_000}, label="turn-2")
    assert first.calls == 2
    assert first.spent_usd == pytest.approx(0.5 + 0.25)

    # A fresh ledger over the same file sees the history.
    reopened = SpendLedger(path=path, cap_usd=5.0)
    assert reopened.calls == 2
    assert reopened.spent_usd == pytest.approx(first.spent_usd)


def test_check_raises_once_the_cap_is_reached(tmp_path):
    ledger = SpendLedger(path=tmp_path / "spend.jsonl", cap_usd=0.40)
    ledger.check(model="claude-opus-5")  # nothing spent yet
    ledger.record(model="claude-opus-5", usage={"input_tokens": 100_000})
    with pytest.raises(BudgetReached, match=r"\$0.5000 of \$0.40"):
        ledger.check(model="claude-opus-5")


def test_the_error_says_how_to_proceed(tmp_path):
    ledger = SpendLedger(path=tmp_path / "spend.jsonl", cap_usd=0.0)
    with pytest.raises(BudgetReached) as excinfo:
        ledger.check(model="claude-opus-5")
    message = str(excinfo.value)
    assert "MANDATE_LLM_CAP_USD" in message
    assert "spend.jsonl" in message


def test_summary_reports_what_is_left(tmp_path):
    ledger = SpendLedger(path=tmp_path / "spend.jsonl", cap_usd=1.0)
    ledger.record(model="claude-opus-5", usage={"input_tokens": 100_000, "output_tokens": 1_000})
    summary = ledger.summary()
    assert summary["calls"] == 1
    assert summary["tokens"] == 101_000
    assert summary["remaining_usd"] == pytest.approx(1.0 - summary["spent_usd"])


def test_remaining_never_goes_negative(tmp_path):
    ledger = SpendLedger(path=tmp_path / "spend.jsonl", cap_usd=0.01)
    ledger.record(model="claude-opus-5", usage={"input_tokens": 1_000_000})
    assert ledger.remaining_usd == 0.0
