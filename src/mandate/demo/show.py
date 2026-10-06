"""Terminal rendering for the demo scenes.

Separated out because the scenes are the argument and the printing is not. Every
scene reaches for the same three things -- a section heading, a tool trace, and a
decision with its rule trace -- and a scene that formatted its own output would
be a scene a reader has to check for honesty twice.

One rule here: nothing in this module decides anything. It prints what the
gateway said. If a column looks favourable it is because the decision was.
"""

from __future__ import annotations

import os
import sys

_FORCE = os.environ.get("MANDATE_COLOUR", "").lower()
_COLOUR = _FORCE in {"1", "yes", "always"} or (_FORCE not in {"0", "no", "never"} and sys.stdout.isatty())


def _c(code: str) -> str:
    return code if _COLOUR else ""


BOLD = _c("\033[1m")
DIM = _c("\033[2m")
RED = _c("\033[31m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
CYAN = _c("\033[36m")
RESET = _c("\033[0m")

OUTCOME_COLOUR = {"allow": GREEN, "deny": RED, "hold_for_approval": YELLOW}


def money(encoded: dict | None) -> str:
    """Render an encoded Money. Integer minor units, so no float ever renders.

    The wire form is `{"minor": 400000, "currency": "USD"}` -- deliberately not a
    decimal string, because a decimal string is a thing a reader can mistake for a
    float and a thing a parser can round.
    """
    if not encoded:
        return "?"
    minor = int(encoded.get("minor", 0))
    currency = str(encoded.get("currency", ""))
    # Two places is right for every currency this demo accepts; `Money` owns the
    # general table, and this is display only.
    return f"{minor // 100}.{minor % 100:02d} {currency}".strip()


def quote_total(quote: dict) -> str:
    return money(quote.get("declared_total"))


def denials(trace: list[dict]) -> list[str]:
    """Rule ids that actually said DENY.

    `refused_by` from the gateway also carries the approval-threshold hold, and
    counting a "ask a human" as a refusal would overstate the result by one every
    single time.
    """
    return [e.get("rule_id", "?") for e in trace if e.get("outcome") == "deny"]


def rule(title: str) -> None:
    print(f"\n{BOLD}{'─' * 74}\n{title}\n{'─' * 74}{RESET}", flush=True)


def note(text: str) -> None:
    for line in text.strip("\n").splitlines():
        print(f"{DIM}  {line}{RESET}")


def finding(verdict: str, text: str, *, good: bool) -> None:
    """A claim about what happened, marked as a measurement rather than a boast."""
    colour = GREEN if good else RED
    print(f"\n  {colour}{BOLD}{verdict}{RESET} {text}")


def show_trace(trace: list[dict]) -> None:
    for entry in trace:
        mark = {
            "allow": f"{GREEN} ok {RESET}",
            "deny": f"{RED}DENY{RESET}",
            "hold_for_approval": f"{YELLOW}hold{RESET}",
        }.get(entry.get("outcome", ""), "  ? ")
        if not entry.get("applicable", True):
            # Reported, not skipped. A rule that did not apply is information:
            # it says the engine looked and found the rule irrelevant, which is
            # different from the engine never having had the rule at all.
            mark = f"{DIM}n/a {RESET}"
        print(f"    [{mark}] {entry.get('rule_id', '?'):<30} {entry.get('message', '')}")


def show_decision(decision: dict, *, label: str = "") -> None:
    if decision.get("refused") or decision.get("error"):
        detail = decision.get("detail") or decision.get("error")
        print(f"  {RED}rejected at the boundary:{RESET} {detail}")
        return

    outcome = str(decision.get("outcome", "?"))
    colour = OUTCOME_COLOUR.get(outcome, "")
    head = f"{label} " if label else ""
    print(f"\n  {head}outcome: {colour}{BOLD}{outcome.upper()}{RESET}")
    hold = decision.get("hold") or {}
    if hold:
        print(f"  amount:   {hold.get('amount')} {hold.get('currency')}  at {hold.get('merchant_name')}")
        print(f"  state:    {hold.get('state')}")
    print(f"  decision: {decision.get('decision_id')}")
    print("\n  rule trace:")
    show_trace(decision.get("rule_trace") or [])
    if decision.get("buyer_approval_url"):
        print(f"\n  {GREEN}Buyer approves at:{RESET}\n    {decision['buyer_approval_url']}")
    if decision.get("awaiting"):
        print(f"\n  {YELLOW}{decision['awaiting']}{RESET}")


def show_cost(run_usd: float, summary: dict, provider: str, model: str) -> None:
    print(
        f"  this run: ${run_usd:.4f}   session total: ${summary['spent_usd']:.4f} "
        f"of ${summary['cap_usd']:.2f} over {summary['calls']} call(s)  "
        f"{DIM}[{provider}/{model}]{RESET}"
    )


def progress(kind: str, detail: dict) -> None:
    """Stream what the agent is doing, as it does it.

    Written because of a measurement, not a preference: a turn on a free-tier
    model has taken over a minute, and a scene that printed its trace only at the
    end left three minutes of blank terminal. On a recording that is
    indistinguishable from a hang, and the first thing a viewer concludes is that
    the project is broken.
    """
    if kind == "thinking":
        print(f"  {DIM}… turn {detail.get('iteration')} ({detail.get('model')}){RESET}", flush=True)
    elif kind == "tool":
        name = detail.get("name")
        arguments = detail.get("arguments") or {}
        extra = ""
        if name == "get_quote":
            lines = arguments.get("lines") or []
            extra = "  " + ", ".join(
                f"{line.get('quantity', 1)}×{line.get('sku')}" for line in lines
            )
        elif name == "browse_catalog":
            extra = f"  {arguments.get('merchant_id')}"
        print(f"  {CYAN}→{RESET} {name}{extra}", flush=True)
    elif kind == "tool_done" and detail.get("failed"):
        print(f"    {RED}failed{RESET}", flush=True)
