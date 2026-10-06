"""The job that settles held money, and the rule it settles it by.

A hold is not a decision that has finished. It is a decision waiting on a fact
nobody has yet: did the thing arrive. This is the loop that asks, and then does one
of four things.

The rule, stated once so it can be argued with:

    delivered            -> capture
    never shipped        -> void
    still in transit     -> leave it, unless it is about to lapse
    about to lapse,      -> void
      not delivered
    cannot tell          -> never capture; void only if it is about to lapse

**Uncertainty resolves towards not taking the money.** That is the whole design and
it is not the obvious choice -- the obvious choice is to capture when the hold is
about to expire, because a merchant who shipped and does not get paid will be
upset. It is the wrong choice here for a reason that has nothing to do with being
cautious for its own sake: a capture is the irreversible half of this system. Void
the wrong hold and a merchant reauthorizes, which is an inconvenience. Capture the
wrong hold and the money is gone, and the only route back is a refund that depends
on the goodwill of whoever took it. An agent spending unsupervised must fail in the
direction that is recoverable.

Note what this job cannot do, by construction: it never consults the policy engine,
because the policy already decided when the authorization was created and
re-deciding at capture time would mean a basket could be refused *after* the buyer
had committed their funds. It never reads a product description. And it never
captures more than the hold, because the amount comes from the hold rather than
from the oracle -- an oracle is asked whether something arrived, never how much to
pay for it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..delivery import Delivered, Delivery, DeliveryOracle
from .state import HoldState
from .store import Hold

logger = logging.getLogger(__name__)

#: How close to lapsing counts as "about to lapse". A day, because PayPal's honor
#: period is three days and a window shorter than the gap between two sweeps would
#: let a hold expire on PayPal's clock instead of ours -- which ends in the same
#: place but leaves no record of a decision.
DEFAULT_GRACE = timedelta(days=1)


@dataclass(frozen=True)
class SweepAction:
    decision_id: str
    delivery: Delivered
    action: str
    detail: str = ""
    state: str = ""


@dataclass
class SweepReport:
    at: datetime
    checked: int = 0
    actions: list[SweepAction] = field(default_factory=list)

    def of(self, action: str) -> list[SweepAction]:
        return [a for a in self.actions if a.action == action]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.actions:
            counts[entry.action] = counts.get(entry.action, 0) + 1
        return counts


async def sweep(
    gateway,
    *,
    oracle: DeliveryOracle,
    now: datetime | None = None,
    grace: timedelta = DEFAULT_GRACE,
    limit: int = 200,
) -> SweepReport:
    """Ask about every held authorization and settle what can be settled.

    Takes the gateway rather than a store so that capture and void go through the
    same methods an operator's HTTP call goes through -- including their state
    machine checks and their ledger events. A sweep that wrote states directly
    would be a second code path for moving money, and the project's claim is that
    there is one.
    """
    moment = now or datetime.now(timezone.utc)
    report = SweepReport(at=moment)

    held = gateway.store.list(states=frozenset({HoldState.HELD}), limit=limit)
    for hold in held:
        report.checked += 1
        lapsing = _lapsing(hold, moment, grace)
        try:
            delivery = await oracle.check(
                decision_id=hold.decision_id, merchant_id=hold.merchant_id
            )
        except Exception as exc:  # noqa: BLE001 - an oracle must not stop the sweep
            # One merchant's broken endpoint must not prevent every other hold from
            # being settled, and "the oracle raised" is exactly UNKNOWN.
            logger.warning("oracle %s raised for %s: %s", oracle.name, hold.decision_id, exc)
            delivery = Delivery(Delivered.UNKNOWN, f"oracle raised {type(exc).__name__}")

        report.actions.append(await _settle(gateway, hold, delivery, lapsing, moment))
    return report


def _lapsing(hold: Hold, now: datetime, grace: timedelta) -> bool:
    expires = hold.authorization_expires_at
    if expires is None:
        # No expiry recorded, so there is no deadline to act on. Reported as not
        # lapsing rather than treated as urgent: guessing an expiry would make the
        # sweep void holds it has no evidence about.
        return False
    return expires - now <= grace


async def _settle(
    gateway, hold: Hold, delivery: Delivery, lapsing: bool, now: datetime
) -> SweepAction:
    def done(action: str, detail: str = "") -> SweepAction:
        return SweepAction(
            hold.decision_id,
            delivery.status,
            action,
            detail or delivery.detail,
            gateway.store.get(hold.decision_id).state.value,
        )

    if delivery.status is Delivered.YES:
        try:
            await gateway.capture(
                hold.decision_id,
                reason=f"delivery confirmed by {delivery.detail or 'the oracle'}",
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - reported per hold, never fatal
            return done("capture_failed", str(exc))
        return done("captured")

    if delivery.status is Delivered.NEVER:
        try:
            await gateway.void(hold.decision_id, reason=delivery.detail or "never delivered", now=now)
        except Exception as exc:  # noqa: BLE001
            return done("void_failed", str(exc))
        return done("voided")

    if not lapsing:
        return done("waiting")

    # Lapsing, and either in transit or unaccounted for. This is the branch the
    # module docstring is about: the authorization is about to stop being worth
    # anything and nobody can confirm the goods arrived, so the money goes back.
    try:
        await gateway.void(
            hold.decision_id,
            reason=(
                "authorization is about to lapse and delivery is "
                f"{delivery.status.value}: releasing rather than capturing"
            ),
            now=now,
        )
    except Exception as exc:  # noqa: BLE001
        return done("void_failed", str(exc))
    return done("voided_on_expiry")
