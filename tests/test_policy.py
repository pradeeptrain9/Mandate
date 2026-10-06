"""Every rule, one at a time, plus how they combine."""

from __future__ import annotations

from datetime import timedelta

from mandate.engine.policy import (
    LedgerWindow,
    Outcome,
    PriorAuthorization,
    evaluate,
    worst,
)
from mandate.engine.quote import Category

from helpers import quote, usd

EMPTY = LedgerWindow()


def verdict(q, policy, now, ledger=EMPTY):
    return evaluate(q.to_policy_input(), policy, ledger, now)


def rule(evaluation, rule_id):
    return next(r for r in evaluation.results if r.rule_id == rule_id)


def prior(at, amount: str, *, merchant="m_acme", fingerprint="fp", categories=()):
    return PriorAuthorization(
        at=at,
        merchant_id=merchant,
        amount=usd(amount),
        fingerprint=fingerprint,
        categories=frozenset(categories or {Category.OFFICE_SUPPLIES}),
    )


# -- severity combination ---------------------------------------------------


def test_worst_wins():
    assert worst([Outcome.ALLOW, Outcome.ALLOW]) is Outcome.ALLOW
    assert worst([Outcome.ALLOW, Outcome.HOLD_FOR_APPROVAL]) is Outcome.HOLD_FOR_APPROVAL
    assert worst([Outcome.DENY, Outcome.HOLD_FOR_APPROVAL]) is Outcome.DENY
    assert worst([]) is Outcome.ALLOW


# -- happy path -------------------------------------------------------------


def test_small_in_policy_purchase_is_allowed(policy, now):
    result = verdict(quote(), policy, now)
    assert result.outcome is Outcome.ALLOW
    assert result.refusals == ()


# -- currency ---------------------------------------------------------------


def test_currency_outside_the_allowlist_is_denied(policy, now):
    q = quote(currency="EUR")
    result = verdict(q, policy, now)
    assert result.outcome is Outcome.DENY
    assert rule(result, "currency_allowed").outcome is Outcome.DENY


def test_amount_rules_mark_themselves_inapplicable_in_another_currency(policy, now):
    """A USD ceiling has nothing to say about a EUR quote, and must say so
    rather than silently passing."""
    result = verdict(quote(currency="EUR"), policy, now)
    assert rule(result, "hard_per_transaction_cap").applicable is False
    assert rule(result, "approval_threshold").applicable is False


# -- merchant ---------------------------------------------------------------


def test_unknown_merchant_is_denied(policy, now):
    result = verdict(quote(merchant_id="m_nobody"), policy, now)
    assert result.outcome is Outcome.DENY
    assert rule(result, "merchant_known").outcome is Outcome.DENY


def test_disabled_merchant_is_denied(policy, now):
    result = verdict(quote(merchant_id="m_disabled"), policy, now)
    assert rule(result, "merchant_known").outcome is Outcome.DENY


def test_merchant_cap_binds_below_the_global_cap(policy, now):
    # $260 at m_acme: under the $500 hard cap, over the merchant's own $250.
    q = quote(items=[("SKU-BULK", "Bulk paper", Category.HARDWARE, "260.00", 1)])
    result = verdict(q, policy, now)
    assert rule(result, "hard_per_transaction_cap").outcome is Outcome.ALLOW
    assert rule(result, "merchant_cap").outcome is Outcome.DENY
    assert result.outcome is Outcome.DENY


def test_merchant_cap_is_inapplicable_when_none_configured(policy, now):
    result = verdict(quote(merchant_id="m_disabled"), policy, now)
    assert rule(result, "merchant_cap").applicable is False


# -- categories -------------------------------------------------------------


def test_irreversible_category_is_denied(policy, now):
    q = quote(items=[("SKU-GC", "Gift card", Category.GIFT_CARD, "25.00", 1)])
    result = verdict(q, policy, now)
    assert rule(result, "category_allowed").outcome is Outcome.DENY
    assert "gift_card" in rule(result, "category_allowed").message


def test_category_outside_the_allowlist_is_denied(policy, now):
    q = quote(items=[("SKU-TRIP", "Flight", Category.TRAVEL, "20.00", 1)])
    assert rule(verdict(q, policy, now), "category_allowed").outcome is Outcome.DENY


def test_category_cap_binds_on_the_subtotal_not_the_total(policy, now):
    """$70 of food plus $10 of paper: the total is fine, the food subtotal is not."""
    q = quote(
        items=[
            ("SKU-LUNCH", "Team lunch", Category.FOOD, "70.00", 1),
            ("SKU-PEN", "Pens", Category.OFFICE_SUPPLIES, "10.00", 1),
        ]
    )
    result = verdict(q, policy, now)
    assert rule(result, "category_cap:food").outcome is Outcome.DENY
    assert rule(result, "category_cap:office_supplies").outcome is Outcome.ALLOW


# -- ceilings and thresholds ------------------------------------------------


def test_over_the_approval_threshold_holds_rather_than_denies(policy, now):
    q = quote(
        merchant_id="m_cloudspend",
        items=[("SKU-GPU", "GPU hour", Category.COMPUTE, "180.00", 1)],
    )
    result = verdict(q, policy, now)
    assert result.outcome is Outcome.HOLD_FOR_APPROVAL
    assert rule(result, "approval_threshold").outcome is Outcome.HOLD_FOR_APPROVAL


def test_over_the_hard_cap_is_denied_and_cannot_be_held(policy, now):
    q = quote(
        merchant_id="m_cloudspend",
        items=[("SKU-GPU", "GPU cluster", Category.COMPUTE, "900.00", 1)],
    )
    result = verdict(q, policy, now)
    assert result.outcome is Outcome.DENY
    assert rule(result, "hard_per_transaction_cap").outcome is Outcome.DENY


# -- envelopes --------------------------------------------------------------


def test_envelope_counts_only_authorizations_inside_the_window(policy, now):
    inside = prior(now - timedelta(minutes=30), "150.00")
    outside = prior(now - timedelta(hours=5), "150.00")
    ledger = LedgerWindow((inside, outside))
    # $60 more against a $200/hour cap: $150 inside the hour + $60 = $210.
    q = quote(items=[("SKU-PAPER", "Paper", Category.OFFICE_SUPPLIES, "60.00", 1)])
    result = verdict(q, policy, now, ledger)
    hour = rule(result, "envelope:hour")
    assert hour.outcome is Outcome.DENY
    assert hour.facts["spent_minor"] == 15000  # the 5-hour-old entry did not count
    assert rule(result, "envelope:day").outcome is Outcome.ALLOW


def test_envelope_allows_a_purchase_that_exactly_reaches_the_cap(policy, now):
    ledger = LedgerWindow((prior(now - timedelta(minutes=5), "150.00"),))
    q = quote(items=[("SKU-PAPER", "Paper", Category.OFFICE_SUPPLIES, "50.00", 1)])
    result = verdict(q, policy, now, ledger)
    assert rule(result, "envelope:hour").outcome is Outcome.ALLOW
    assert rule(result, "envelope:hour").facts["projected_minor"] == 20000


# -- velocity and duplicates ------------------------------------------------


def test_velocity_limit_denies_the_sixth_authorization_in_an_hour(policy, now):
    ledger = LedgerWindow(
        tuple(prior(now - timedelta(minutes=i + 1), "1.00", fingerprint=f"fp{i}") for i in range(5))
    )
    result = verdict(quote(), policy, now, ledger)
    assert rule(result, "velocity").outcome is Outcome.DENY
    assert rule(result, "velocity").facts == {"recent_count": 5, "limit": 5}


def test_an_identical_basket_holds_for_a_human_rather_than_denying(policy, now):
    q = quote()
    ledger = LedgerWindow((prior(now - timedelta(minutes=2), "34.00", fingerprint=q.fingerprint()),))
    result = verdict(q, policy, now, ledger)
    assert rule(result, "duplicate_intent").outcome is Outcome.HOLD_FOR_APPROVAL
    assert result.outcome is Outcome.HOLD_FOR_APPROVAL


def test_an_old_identical_basket_is_not_a_duplicate(policy, now):
    q = quote()
    ledger = LedgerWindow((prior(now - timedelta(hours=3), "34.00", fingerprint=q.fingerprint()),))
    assert rule(verdict(q, policy, now, ledger), "duplicate_intent").outcome is Outcome.ALLOW


# -- determinism ------------------------------------------------------------


def test_the_same_inputs_always_produce_the_same_trace(policy, now):
    q = quote()
    ledger = LedgerWindow((prior(now - timedelta(minutes=10), "25.00"),))
    first = verdict(q, policy, now, ledger)
    second = verdict(q, policy, now, ledger)
    assert first == second


def test_every_rule_appears_in_the_trace_even_when_one_has_already_denied(policy, now):
    """No short-circuiting: 'refused by four rules' is a different fact from
    'refused', and the ledger needs the first one."""
    q = quote(merchant_id="m_nobody", items=[("SKU-GC", "x", Category.GIFT_CARD, "900.00", 1)])
    result = verdict(q, policy, now)
    ids = {r.rule_id for r in result.results}
    assert {
        "currency_allowed",
        "merchant_known",
        "category_allowed",
        "hard_per_transaction_cap",
        "merchant_cap",
        "envelope:hour",
        "envelope:day",
        "envelope:month",
        "velocity",
        "duplicate_intent",
        "approval_threshold",
    } <= ids
