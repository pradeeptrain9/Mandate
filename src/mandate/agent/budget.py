"""What the agent costs to run, and the cap that stops it running away.

Two jobs, kept apart because they fail differently:

  * **Record what each call actually cost**, from the `usage` the API returns --
    not an estimate. Without this the only cost signal is the invoice, and an
    invoice arrives late and says nothing about which loop spent it.
  * **Refuse the call when the cap is reached, before spending anything.** An
    agent in a tool loop is exactly the shape that bills a surprise: one bad
    prompt and it circles twenty times.

A demo is the worst case for this. It gets re-run dozens of times while the video
is shot, often with a tool loop that has just been changed, and nobody is watching
the console. So the cap is checked *before* each request, not after.

Prices are a table rather than a lookup, and rounded in the direction that makes
the cap bind early: an unknown model is priced at the most expensive known rate
rather than zero, because a model priced at zero would never reach the cap at all.
Check the figures against https://docs.claude.com/en/docs/about-claude/pricing
when changing models.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class BudgetReached(RuntimeError):
    """Raised before a request is made, never after money is spent."""


@dataclass(frozen=True)
class Rate:
    """USD per million tokens.

    `cache_write` and `cache_read` are the standard multipliers on input --
    1.25x to write a cache entry, 0.1x to read one. They are written out rather
    than computed so a model that prices caching differently can override them.
    """

    input_usd: float
    output_usd: float
    cache_write_usd: float
    cache_read_usd: float

    @classmethod
    def from_input(cls, input_usd: float, output_usd: float) -> "Rate":
        return cls(
            input_usd=input_usd,
            output_usd=output_usd,
            cache_write_usd=input_usd * 1.25,
            cache_read_usd=input_usd * 0.10,
        )


#: Checked against the published pricing on 2026-10-06.
#:
#: Gemini Flash models are listed at their paid rates even though the demo runs on
#: the free tier. Pricing free usage at zero would make the cap meaningless, and
#: the point of the ledger is to show what the thing would cost to run, not what
#: this month's invoice happens to say. Google's free tier is rate-limited rather
#: than billed, so the figure here is an honest cost-to-operate.
RATES: dict[str, Rate] = {
    "claude-opus-5": Rate.from_input(5.00, 25.00),
    "claude-opus-4-8": Rate.from_input(5.00, 25.00),
    "claude-sonnet-5": Rate.from_input(2.00, 10.00),
    "claude-haiku-4-5": Rate.from_input(1.00, 5.00),
    "claude-fable-5-1": Rate.from_input(10.00, 50.00),
    # Flash tier. Rounded up where a model prices by context length, because
    # over-pricing makes the cap bind early and that is the safe direction.
    "gemini-3.8-flash": Rate.from_input(0.30, 2.50),
    "gemini-3.7-flash": Rate.from_input(0.30, 2.50),
    "gemini-3.6-flash": Rate.from_input(0.30, 2.50),
    "gemini-3.5-flash": Rate.from_input(0.30, 2.50),
    "gemini-2.5-flash": Rate.from_input(0.30, 2.50),
    "gemini-2.5-flash-lite": Rate.from_input(0.10, 0.40),
}

#: What an unlisted model is charged at. The most expensive rate known, so that a
#: model nobody updated the table for makes the cap bind early instead of never.
FALLBACK_RATE = max(RATES.values(), key=lambda r: r.output_usd)


def rate_for(model: str) -> Rate:
    if model in RATES:
        return RATES[model]
    # Prefix match, so a dated snapshot of a known model prices correctly.
    for known, rate in RATES.items():
        if model.startswith(known):
            return rate
    return FALLBACK_RATE


@dataclass(frozen=True)
class Usage:
    """The token counts one request reported."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @classmethod
    def from_response(cls, usage: Any) -> "Usage":
        """Read an SDK usage object or a plain dict, tolerating absent fields.

        A missing field is read as zero rather than guessed at. Under-counting
        here would under-report spend, so the fields that commonly go missing
        are the cache ones, which are the cheap ones.
        """

        def get(name: str) -> int:
            value = (
                usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            )
            return int(value or 0)

        return cls(
            input_tokens=get("input_tokens"),
            output_tokens=get("output_tokens"),
            cache_creation_input_tokens=get("cache_creation_input_tokens"),
            cache_read_input_tokens=get("cache_read_input_tokens"),
        )


def price(usage: Usage, model: str) -> float:
    """USD for one request. Exact arithmetic on the counts the API reported."""
    rate = rate_for(model)
    return (
        usage.input_tokens * rate.input_usd
        + usage.output_tokens * rate.output_usd
        + usage.cache_creation_input_tokens * rate.cache_write_usd
        + usage.cache_read_input_tokens * rate.cache_read_usd
    ) / 1_000_000


@dataclass
class SpendLedger:
    """Append-only record of model spend, with a hard cap checked before each call.

    Persisted as JSONL next to the decision ledger. Deliberately separate from it:
    one records what the policy engine decided and must be tamper-evident, the
    other records what the demo cost and is ordinary bookkeeping.
    """

    path: Path
    cap_usd: float = 5.00
    entries: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.entries.append(json.loads(line))

    @property
    def spent_usd(self) -> float:
        return sum(float(entry["usd"]) for entry in self.entries)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)

    @property
    def calls(self) -> int:
        return len(self.entries)

    def check(self, *, model: str) -> None:
        """Raise if the cap is already reached. Called before every request."""
        if self.spent_usd >= self.cap_usd:
            raise BudgetReached(
                f"model spend cap reached: ${self.spent_usd:.4f} of ${self.cap_usd:.2f} "
                f"over {self.calls} call(s). Raise MANDATE_LLM_CAP_USD or clear {self.path}."
            )

    def record(self, *, model: str, usage: Any, label: str = "") -> float:
        """Price a completed request from its own usage and append it."""
        parsed = Usage.from_response(usage)
        usd = price(parsed, model)
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": model,
            "label": label,
            "usd": round(usd, 6),
            "input_tokens": parsed.input_tokens,
            "output_tokens": parsed.output_tokens,
            "cache_creation_input_tokens": parsed.cache_creation_input_tokens,
            "cache_read_input_tokens": parsed.cache_read_input_tokens,
        }
        self.entries.append(entry)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        return usd

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "spent_usd": round(self.spent_usd, 6),
            "cap_usd": self.cap_usd,
            "remaining_usd": round(self.remaining_usd, 6),
            "tokens": sum(
                int(e.get("input_tokens", 0)) + int(e.get("output_tokens", 0))
                for e in self.entries
            ),
        }
