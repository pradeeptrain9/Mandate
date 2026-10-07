"""The policy the demo runs against.

Real deployments would load this from configuration. It lives in code here so
that the numbers a reviewer sees in the tests are the numbers the demo uses, and
so a reader can find in one place every ceiling the three demo scenes bump into.

The shape of it is the argument: an unattended agent gets a small, boring
allowance, a human gets paged in the middle band, and the top band is refused
outright with nobody to overrule it.
"""

from __future__ import annotations

from datetime import timedelta

from .engine.money import Money
from .engine.policy import Envelope, MerchantRule, Policy
from .engine.quote import Category


def usd(value: str) -> Money:
    return Money.from_paypal(value, "USD")


#: Categories that do not reverse. A compromised agent is steered towards these
#: precisely because a captured gift card is gone in a way a captured stapler is
#: not, so they are denied outright rather than merely capped.
IRREVERSIBLE = frozenset({Category.GIFT_CARD, Category.CRYPTO, Category.CASH_EQUIVALENT})


def demo_policy() -> Policy:
    return Policy(
        policy_id="demo-ops-agent",
        currency="USD",
        # Three tiers. Under $100 the agent acts alone; $100-$500 pages a human;
        # over $500 nothing and nobody gets it through.
        approval_threshold=usd("100.00"),
        hard_per_transaction_cap=usd("500.00"),
        allowed_currencies=frozenset({"USD"}),
        allowed_categories=frozenset(
            {
                Category.OFFICE_SUPPLIES,
                Category.COMPUTE,
                Category.SOFTWARE_SUBSCRIPTION,
                Category.SHIPPING,
                Category.HARDWARE,
                Category.PROFESSIONAL_SERVICES,
                Category.FOOD,
                Category.CLOTHING,
            }
        ),
        denied_categories=IRREVERSIBLE,
        category_caps=(
            (Category.COMPUTE, usd("300.00")),
            (Category.OFFICE_SUPPLIES, usd("150.00")),
            (Category.FOOD, usd("60.00")),
            # Retail: one outfit, not a wardrobe.
            (Category.CLOTHING, usd("200.00")),
        ),
        merchants=(
            MerchantRule("m_acme", per_transaction_cap=usd("250.00")),
            MerchantRule("m_cloudspend", per_transaction_cap=usd("400.00")),
            # Ships nothing, ever. Scene 3 depends on it being a *registered,
            # allowed* merchant: the point is that a hold expires safely even
            # when the policy had no reason to refuse in the first place.
            MerchantRule("m_ghost", per_transaction_cap=usd("200.00")),
            MerchantRule("m_thread", per_transaction_cap=usd("300.00")),
            MerchantRule("m_disabled", enabled=False),
        ),
        envelopes=(
            Envelope("hour", timedelta(hours=1), usd("200.00")),
            Envelope("day", timedelta(days=1), usd("600.00")),
            Envelope("month", timedelta(days=30), usd("3000.00")),
        ),
        velocity_limit=5,
        velocity_window=timedelta(hours=1),
        duplicate_window=timedelta(minutes=30),
        quote_max_age=timedelta(minutes=10),
    )
