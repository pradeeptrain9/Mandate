"""The decision. Pure functions, integer arithmetic, no model, no network.

Everything in this module is a function of its arguments. There is no clock
call, no database read, no HTTP, and no language model -- `now` and the prior
authorizations arrive as parameters because a rule that reads a clock cannot be
replayed, and a decision that cannot be replayed cannot be audited.

The engine evaluates every rule even after one has already refused, and returns
the whole trace. Short-circuiting would be cheaper and would also throw away
the most useful thing the system produces: "this was refused by three
independent rules" is a different fact from "this was refused", and a human
reading the ledger six weeks later needs the first one.

Severity combines by worst-wins: any DENY denies, otherwise any
HOLD_FOR_APPROVAL holds, otherwise allow. A rule may also report itself
inapplicable -- a ceiling denominated in USD has nothing to say about a quote in
EUR -- and an inapplicable rule never contributes to the outcome. It still
appears in the trace, with the reason, because a silently skipped ceiling is how
a ceiling stops binding without anyone noticing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from .money import Money, total
from .quote import Category, PolicyInput

ENGINE_VERSION = "1.0.0"


class Outcome(StrEnum):
    ALLOW = "allow"
    HOLD_FOR_APPROVAL = "hold_for_approval"
    DENY = "deny"


#: Worst wins. Index into this to compare two outcomes.
_SEVERITY: dict[Outcome, int] = {Outcome.ALLOW: 0, Outcome.HOLD_FOR_APPROVAL: 1, Outcome.DENY: 2}


def worst(outcomes: list[Outcome]) -> Outcome:
    return max(outcomes, key=lambda o: _SEVERITY[o], default=Outcome.ALLOW)


# ---------------------------------------------------------------------------
# Policy: the configuration a decision is made against
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Envelope:
    """A rolling spend ceiling: at most `cap` within the last `duration`.

    Rolling rather than calendar-aligned. A calendar-month cap resets at
    midnight on the first, which hands an agent a predictable moment at which
    its budget doubles; a rolling window has no such edge.
    """

    label: str
    duration: timedelta
    cap: Money


@dataclass(frozen=True)
class MerchantRule:
    merchant_id: str
    enabled: bool = True
    per_transaction_cap: Money | None = None


@dataclass(frozen=True)
class Policy:
    """Caps and allowlists. Declared in one currency; see `applicable` below.

    `hard_per_transaction_cap` and `approval_threshold` differ in kind, not just
    degree. The threshold is "a human decides"; the hard cap is "nobody
    decides, not even a human holding the phone" -- because the phone is the
    thing an attacker has social-engineered when it matters. A policy whose hard
    cap merely equals its approval threshold has no refuse-outright tier, which
    is a legitimate configuration but should be a deliberate one.
    """

    policy_id: str
    currency: str
    hard_per_transaction_cap: Money
    approval_threshold: Money
    allowed_currencies: frozenset[str] = frozenset()
    allowed_categories: frozenset[Category] = frozenset()
    denied_categories: frozenset[Category] = frozenset()
    category_caps: tuple[tuple[Category, Money], ...] = ()
    merchants: tuple[MerchantRule, ...] = ()
    envelopes: tuple[Envelope, ...] = ()
    velocity_limit: int = 0  # 0 disables
    velocity_window: timedelta = timedelta(hours=1)
    duplicate_window: timedelta = timedelta(minutes=30)
    quote_max_age: timedelta = timedelta(minutes=10)
    version: str = ENGINE_VERSION

    def merchant(self, merchant_id: str) -> MerchantRule | None:
        for rule in self.merchants:
            if rule.merchant_id == merchant_id:
                return rule
        return None

    def category_cap(self, category: Category) -> Money | None:
        for candidate, cap in self.category_caps:
            if candidate is category:
                return cap
        return None


# ---------------------------------------------------------------------------
# Ledger view: prior authorizations, supplied by the caller
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorAuthorization:
    """One authorization this policy already placed.

    Only authorizations that were actually placed belong here. A refused request
    consumed no budget and must not count against the envelope, or a single
    attack that trips every rule would also exhaust the day's allowance and deny
    the legitimate purchase behind it.

    `reserved` distinguishes money that is still committed from money that came
    back. A hold that was voided or left to expire no longer occupies the
    envelope -- the funds are demonstrably back with the buyer, and continuing to
    count them would let one cancelled order shrink the day's allowance for no
    reason. It does still count towards the velocity limit, because the thing
    velocity measures is how often the agent is reaching for the card, and a
    buy-then-void loop is exactly the pattern worth rate-limiting. So the two
    rules read this list differently and deliberately: envelopes sum the reserved
    entries, velocity counts all of them.
    """

    at: datetime
    merchant_id: str
    amount: Money
    fingerprint: str
    categories: frozenset[Category]
    reserved: bool = True


@dataclass(frozen=True)
class LedgerWindow:
    entries: tuple[PriorAuthorization, ...] = ()

    def since(self, cutoff: datetime) -> list[PriorAuthorization]:
        return [e for e in self.entries if e.at >= cutoff]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleResult:
    """One rule's verdict.

    `facts` holds the integers behind the verdict. The narration guard builds its
    allowed-number set from these, so a figure that is not here cannot appear in
    the explanation a human reads. `message` is engine-authored -- the only prose
    in the decision path that the engine itself wrote.
    """

    rule_id: str
    outcome: Outcome
    message: str
    facts: dict[str, int | str] = field(default_factory=dict)
    applicable: bool = True

    @property
    def counts(self) -> bool:
        return self.applicable


@dataclass(frozen=True)
class Evaluation:
    outcome: Outcome
    results: tuple[RuleResult, ...]
    engine_version: str = ENGINE_VERSION

    @property
    def refusals(self) -> tuple[RuleResult, ...]:
        return tuple(r for r in self.results if r.counts and r.outcome is not Outcome.ALLOW)

    @property
    def reason_ids(self) -> tuple[str, ...]:
        return tuple(r.rule_id for r in self.refusals)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

_NOT_IN_POLICY_CURRENCY = "quote currency differs from policy currency; cap not comparable"


def _currency_allowed(request: PolicyInput, policy: Policy) -> RuleResult:
    allowed = policy.allowed_currencies or frozenset({policy.currency})
    if request.currency in allowed:
        return RuleResult(
            "currency_allowed", Outcome.ALLOW, f"{request.currency} is an accepted currency"
        )
    return RuleResult(
        "currency_allowed",
        Outcome.DENY,
        f"{request.currency} is not an accepted currency",
        {"currency": request.currency},
    )


def _merchant_known(request: PolicyInput, policy: Policy) -> RuleResult:
    rule = policy.merchant(request.merchant_id)
    if rule is None:
        return RuleResult(
            "merchant_known",
            Outcome.DENY,
            "merchant is not in the registry",
            {"merchant_id": request.merchant_id},
        )
    if not rule.enabled:
        return RuleResult(
            "merchant_known",
            Outcome.DENY,
            "merchant is registered but disabled",
            {"merchant_id": request.merchant_id},
        )
    return RuleResult("merchant_known", Outcome.ALLOW, "merchant is registered and enabled")


def _category_allowed(request: PolicyInput, policy: Policy) -> RuleResult:
    denied = sorted(c.value for c in (request.categories & policy.denied_categories))
    if denied:
        return RuleResult(
            "category_allowed",
            Outcome.DENY,
            f"category is on the deny list: {', '.join(denied)}",
            {"denied_categories": ", ".join(denied)},
        )
    if policy.allowed_categories:
        outside = sorted(c.value for c in (request.categories - policy.allowed_categories))
        if outside:
            return RuleResult(
                "category_allowed",
                Outcome.DENY,
                f"category is not on the allow list: {', '.join(outside)}",
                {"unlisted_categories": ", ".join(outside)},
            )
    return RuleResult("category_allowed", Outcome.ALLOW, "every category is permitted")


def _hard_cap(request: PolicyInput, policy: Policy) -> RuleResult:
    if request.currency != policy.currency:
        return RuleResult(
            "hard_per_transaction_cap", Outcome.ALLOW, _NOT_IN_POLICY_CURRENCY, {}, applicable=False
        )
    cap = policy.hard_per_transaction_cap
    if request.amount > cap:
        return RuleResult(
            "hard_per_transaction_cap",
            Outcome.DENY,
            f"{request.amount} exceeds the hard per-transaction cap of {cap}; "
            "this tier cannot be approved by a human",
            {"amount_minor": request.amount.minor, "cap_minor": cap.minor},
        )
    return RuleResult(
        "hard_per_transaction_cap",
        Outcome.ALLOW,
        f"{request.amount} is within the hard cap of {cap}",
        {"amount_minor": request.amount.minor, "cap_minor": cap.minor},
    )


def _merchant_cap(request: PolicyInput, policy: Policy) -> RuleResult:
    rule = policy.merchant(request.merchant_id)
    if rule is None or rule.per_transaction_cap is None:
        return RuleResult(
            "merchant_cap", Outcome.ALLOW, "no per-merchant cap configured", {}, applicable=False
        )
    cap = rule.per_transaction_cap
    if request.currency != cap.currency:
        return RuleResult("merchant_cap", Outcome.ALLOW, _NOT_IN_POLICY_CURRENCY, {}, applicable=False)
    if request.amount > cap:
        return RuleResult(
            "merchant_cap",
            Outcome.DENY,
            f"{request.amount} exceeds the {cap} cap for this merchant",
            {"amount_minor": request.amount.minor, "cap_minor": cap.minor},
        )
    return RuleResult(
        "merchant_cap",
        Outcome.ALLOW,
        f"{request.amount} is within the merchant cap of {cap}",
        {"amount_minor": request.amount.minor, "cap_minor": cap.minor},
    )


def _category_caps(request: PolicyInput, policy: Policy) -> list[RuleResult]:
    out: list[RuleResult] = []
    for category, amount in request.category_subtotals:
        cap = policy.category_cap(category)
        if cap is None:
            continue
        rule_id = f"category_cap:{category.value}"
        if amount.currency != cap.currency:
            out.append(RuleResult(rule_id, Outcome.ALLOW, _NOT_IN_POLICY_CURRENCY, {}, applicable=False))
        elif amount > cap:
            out.append(
                RuleResult(
                    rule_id,
                    Outcome.DENY,
                    f"{amount} of {category.value} exceeds its {cap} cap",
                    {"subtotal_minor": amount.minor, "cap_minor": cap.minor},
                )
            )
        else:
            out.append(
                RuleResult(
                    rule_id,
                    Outcome.ALLOW,
                    f"{amount} of {category.value} is within its {cap} cap",
                    {"subtotal_minor": amount.minor, "cap_minor": cap.minor},
                )
            )
    return out


def _envelopes(
    request: PolicyInput, policy: Policy, ledger: LedgerWindow, now: datetime
) -> list[RuleResult]:
    out: list[RuleResult] = []
    for envelope in policy.envelopes:
        rule_id = f"envelope:{envelope.label}"
        if request.currency != envelope.cap.currency:
            out.append(RuleResult(rule_id, Outcome.ALLOW, _NOT_IN_POLICY_CURRENCY, {}, applicable=False))
            continue
        prior = [
            e.amount
            for e in ledger.since(now - envelope.duration)
            if e.reserved and e.amount.currency == envelope.cap.currency
        ]
        spent = total(prior, envelope.cap.currency)
        projected = spent + request.amount
        facts = {
            "spent_minor": spent.minor,
            "amount_minor": request.amount.minor,
            "projected_minor": projected.minor,
            "cap_minor": envelope.cap.minor,
        }
        if projected > envelope.cap:
            out.append(
                RuleResult(
                    rule_id,
                    Outcome.DENY,
                    f"{spent} already authorized in the last {envelope.label}; "
                    f"{request.amount} more would reach {projected} against a {envelope.cap} cap",
                    facts,
                )
            )
        else:
            out.append(
                RuleResult(
                    rule_id,
                    Outcome.ALLOW,
                    f"{projected} of the {envelope.cap} {envelope.label} cap would be used",
                    facts,
                )
            )
    return out


def _velocity(policy: Policy, ledger: LedgerWindow, now: datetime) -> RuleResult:
    if policy.velocity_limit <= 0:
        return RuleResult("velocity", Outcome.ALLOW, "no velocity limit configured", {}, applicable=False)
    recent = ledger.since(now - policy.velocity_window)
    facts = {"recent_count": len(recent), "limit": policy.velocity_limit}
    if len(recent) >= policy.velocity_limit:
        return RuleResult(
            "velocity",
            Outcome.DENY,
            f"{len(recent)} authorizations already placed in this window; "
            f"the limit is {policy.velocity_limit}",
            facts,
        )
    return RuleResult(
        "velocity",
        Outcome.ALLOW,
        f"{len(recent)} of {policy.velocity_limit} authorizations used in this window",
        facts,
    )


def _duplicate(request: PolicyInput, policy: Policy, ledger: LedgerWindow, now: datetime) -> RuleResult:
    matches = [
        e
        for e in ledger.since(now - policy.duplicate_window)
        if e.fingerprint == request.fingerprint
    ]
    facts = {"matches": len(matches), "fingerprint": request.fingerprint}
    if matches:
        return RuleResult(
            "duplicate_intent",
            Outcome.HOLD_FOR_APPROVAL,
            f"an identical basket was authorized {len(matches)} time(s) recently; "
            "a human should confirm this is not a retry loop",
            facts,
        )
    return RuleResult("duplicate_intent", Outcome.ALLOW, "no recent identical basket", facts)


def _approval_threshold(request: PolicyInput, policy: Policy) -> RuleResult:
    if request.currency != policy.currency:
        return RuleResult(
            "approval_threshold", Outcome.ALLOW, _NOT_IN_POLICY_CURRENCY, {}, applicable=False
        )
    threshold = policy.approval_threshold
    facts = {"amount_minor": request.amount.minor, "threshold_minor": threshold.minor}
    if request.amount > threshold:
        return RuleResult(
            "approval_threshold",
            Outcome.HOLD_FOR_APPROVAL,
            f"{request.amount} is over the {threshold} threshold for unattended spending",
            facts,
        )
    return RuleResult(
        "approval_threshold",
        Outcome.ALLOW,
        f"{request.amount} is under the {threshold} unattended threshold",
        facts,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def evaluate(
    request: PolicyInput,
    policy: Policy,
    ledger: LedgerWindow,
    now: datetime,
) -> Evaluation:
    """Decide. Deterministic in all four arguments and nothing else.

    Call this twice with the same arguments and you get the same answer, which
    is what makes `mandate replay` possible and what makes the ledger an
    executable record rather than a diary.
    """
    results: list[RuleResult] = [
        _currency_allowed(request, policy),
        _merchant_known(request, policy),
        _category_allowed(request, policy),
        _hard_cap(request, policy),
        _merchant_cap(request, policy),
    ]
    results += _category_caps(request, policy)
    results += _envelopes(request, policy, ledger, now)
    results += [
        _velocity(policy, ledger, now),
        _duplicate(request, policy, ledger, now),
        _approval_threshold(request, policy),
    ]

    outcome = worst([r.outcome for r in results if r.counts])
    return Evaluation(outcome=outcome, results=tuple(results))
