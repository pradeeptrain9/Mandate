"""The lifecycle of one request for money, as an explicit state machine.

Two different approvals happen in this flow and conflating them would be a
mistake, so they are named apart:

  * **human approval** -- an over-threshold request waiting on the person who
    holds the phone. This is Mandate's own gate.
  * **buyer approval** -- PayPal will not reserve a buyer's funds without the
    buyer saying so. This is PayPal's gate and it exists even for a request the
    policy waved straight through.

`consumes_budget` is the subtle one. A request occupies the rolling spend
envelope from the moment an order exists -- not from the moment funds are
actually held -- because otherwise an agent could have a hundred policy-approved
orders sitting unapproved and then settle them all at once, each one having been
measured against an empty envelope. When a hold is voided or expires the money is
demonstrably back and the reservation is released.

`counts_as_placement` is a different question with a different answer: it stays
true after a void, because the velocity limit measures how often the agent
reaches for the card, and buy-then-void is precisely the loop worth rate
limiting.
"""

from __future__ import annotations

from enum import StrEnum


class HoldState(StrEnum):
    # Every hold enters here, for exactly as long as one request takes. It exists
    # so that the first thing recorded about a hold is a transition like any
    # other, rather than a row that springs into being already decided.
    RECEIVED = "received"
    # Terminal, no PayPal object was ever created.
    REFUSED = "refused"
    # Mandate's gate.
    AWAITING_HUMAN = "awaiting_human"
    DECLINED_BY_HUMAN = "declined_by_human"
    # PayPal's gate.
    AWAITING_BUYER = "awaiting_buyer"
    BUYER_CANCELLED = "buyer_cancelled"
    # Funds reserved on the buyer's instrument.
    HELD = "held"
    # Terminal money outcomes.
    CAPTURED = "captured"
    VOIDED = "voided"
    EXPIRED = "expired"
    REFUNDED = "refunded"
    # Something went wrong at PayPal and a human needs to look.
    FAILED = "failed"


#: Which transitions are legal. Anything absent raises rather than being
#: tolerated: a hold that goes from CAPTURED back to HELD is a bug, and the
#: moment to find out is the attempt, not the reconciliation.
TRANSITIONS: dict[HoldState, frozenset[HoldState]] = {
    HoldState.RECEIVED: frozenset(
        {
            HoldState.REFUSED,
            HoldState.AWAITING_HUMAN,
            HoldState.AWAITING_BUYER,
            HoldState.FAILED,
        }
    ),
    HoldState.AWAITING_HUMAN: frozenset(
        {HoldState.AWAITING_BUYER, HoldState.DECLINED_BY_HUMAN, HoldState.FAILED}
    ),
    HoldState.AWAITING_BUYER: frozenset(
        {HoldState.HELD, HoldState.BUYER_CANCELLED, HoldState.EXPIRED, HoldState.FAILED}
    ),
    HoldState.HELD: frozenset(
        {HoldState.CAPTURED, HoldState.VOIDED, HoldState.EXPIRED, HoldState.HELD, HoldState.FAILED}
    ),
    HoldState.CAPTURED: frozenset({HoldState.REFUNDED}),
    # Terminal.
    HoldState.REFUSED: frozenset(),
    HoldState.DECLINED_BY_HUMAN: frozenset(),
    HoldState.BUYER_CANCELLED: frozenset(),
    HoldState.VOIDED: frozenset(),
    HoldState.EXPIRED: frozenset(),
    HoldState.REFUNDED: frozenset(),
    HoldState.FAILED: frozenset({HoldState.VOIDED, HoldState.EXPIRED}),
}

#: HELD -> HELD is legal because reauthorization replaces the authorization id
#: and restarts the honor period without changing the state.
SELF_TRANSITIONS = frozenset({HoldState.HELD})

#: States in which money is still committed and the rolling envelope should
#: carry it.
RESERVING: frozenset[HoldState] = frozenset(
    {HoldState.AWAITING_BUYER, HoldState.HELD, HoldState.CAPTURED}
)

#: States in which an authorization request actually reached PayPal, for the
#: velocity limit. A void does not undo the fact that the agent asked.
PLACED: frozenset[HoldState] = frozenset(
    {
        HoldState.AWAITING_BUYER,
        HoldState.HELD,
        HoldState.CAPTURED,
        HoldState.VOIDED,
        HoldState.EXPIRED,
        HoldState.REFUNDED,
        HoldState.BUYER_CANCELLED,
        HoldState.FAILED,
    }
)

TERMINAL: frozenset[HoldState] = frozenset(
    state for state, onward in TRANSITIONS.items() if not onward
)


class IllegalTransition(RuntimeError):
    def __init__(self, current: HoldState, target: HoldState) -> None:
        allowed = ", ".join(sorted(s.value for s in TRANSITIONS.get(current, frozenset())))
        super().__init__(
            f"cannot move from {current.value} to {target.value}; "
            f"allowed: {allowed or 'nothing, this state is terminal'}"
        )
        self.current = current
        self.target = target


def check_transition(current: HoldState, target: HoldState) -> None:
    if target not in TRANSITIONS.get(current, frozenset()):
        raise IllegalTransition(current, target)


def consumes_budget(state: HoldState) -> bool:
    return state in RESERVING


def counts_as_placement(state: HoldState) -> bool:
    return state in PLACED
