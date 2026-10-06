"""PayPal's view of the money, reconciled against ours.

Why this module exists at all. Everything the gateway knows about a hold so far
came from its own outbound calls. That is not enough: a buyer approves in their
own browser, an authorization expires on PayPal's clock, a capture is reversed by
someone in a dispute console, and none of those events pass through this process.
A ledger that only records what we did is a ledger that drifts.

Three rules shape it, and each one is a deliberate refusal of an easier design.

**Nothing is believed before it is verified.** The route hands the raw bytes to
`PayPalClient.verify_webhook`, which asks PayPal's own endpoint whether those exact
bytes carry a valid signature. An unverified delivery is dropped and recorded as
dropped. This is the single place where a stranger can post JSON at a system that
moves money, and the plan for this project named a trusting webhook endpoint as the
worst thing that could be in the repository.

**A replay is not a second event.** PayPal retries deliveries, and a retried
`PAYMENT.CAPTURE.COMPLETED` applied twice reads as two captures. `event_id` is
remembered, and a second sighting is answered 200 and ignored -- 200 because a
non-2xx asks PayPal to retry, and retrying is the one thing this situation does not
need.

**An unknown event is reported, not silently dropped.** Unrecognised types are
recorded with their type and acknowledged. A webhook subscription changes under
you, and "we never saw it" is a much worse answer than "we saw it and did not
understand it".

The transition table is deliberately narrow. These events say *what happened at
PayPal*, and that is all they are allowed to say: no event here can create a hold,
change an amount, or move money. The worst a forged-but-somehow-verified delivery
could do is mark a hold's outcome wrongly, which the decision record would
contradict on the next replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..engine.money import Money
from .state import HoldState, IllegalTransition
from .store import Hold, Store, UnknownHold

#: The events this gateway acts on, and the state each one implies.
#:
#: `CHECKOUT.ORDER.APPROVED` is absent on purpose: it is handled separately,
#: because the response to a buyer approving is to *do* something (authorize the
#: order) rather than to record something, and an outbound call does not belong in
#: a lookup table.
TERMINAL_EVENTS: dict[str, HoldState] = {
    "PAYMENT.AUTHORIZATION.CREATED": HoldState.HELD,
    "PAYMENT.AUTHORIZATION.VOIDED": HoldState.VOIDED,
    "PAYMENT.CAPTURE.COMPLETED": HoldState.CAPTURED,
    "PAYMENT.CAPTURE.DENIED": HoldState.FAILED,
    "PAYMENT.CAPTURE.REFUNDED": HoldState.REFUNDED,
    "PAYMENT.CAPTURE.REVERSED": HoldState.REFUNDED,
}

#: Events worth acknowledging and recording, but which imply no state change.
#: Listed rather than lumped in with the unknown, so that a genuinely unknown
#: event stays visible in the logs.
NOTED_EVENTS = frozenset(
    {
        "PAYMENT.AUTHORIZATION.REAUTHORIZED",
        "CHECKOUT.ORDER.COMPLETED",
        "CHECKOUT.PAYMENT-APPROVAL.REVERSED",
    }
)


@dataclass(frozen=True)
class WebhookOutcome:
    """What was done about one delivery. Returned so the route can say so, and so
    a test can assert on the decision rather than on a side effect."""

    event_id: str
    event_type: str
    action: str
    decision_id: str | None = None
    from_state: str | None = None
    to_state: str | None = None
    detail: str = ""

    @property
    def applied(self) -> bool:
        return self.action == "applied"


def event_id_of(event: dict[str, Any]) -> str:
    return str(event.get("id") or "")


def locate(store: Store, event: dict[str, Any]) -> Hold | None:
    """Find the hold a delivery is about.

    Tried in order of how much PayPal could have got wrong. `custom_id` is first
    because the gateway put it there itself -- it is the decision id, set on the
    purchase unit at order creation and propagated by PayPal onto authorizations
    and captures. Matching on it is matching on our own identifier rather than on
    one of PayPal's, which is the difference between a lookup and a guess.

    The id-shaped fallbacks exist because `custom_id` does not survive every event
    shape, and a hold that cannot be found is reported as not found rather than
    assumed to be the most recent one.
    """
    resource = event.get("resource") or {}
    related = ((resource.get("supplementary_data") or {}).get("related_ids")) or {}

    candidates: list[str] = []
    for value in (resource.get("custom_id"), resource.get("invoice_id")):
        if value:
            candidates.append(str(value))
    for unit in resource.get("purchase_units") or []:
        if isinstance(unit, dict) and unit.get("custom_id"):
            candidates.append(str(unit["custom_id"]))
    for decision_id in candidates:
        try:
            return store.get(decision_id)
        except UnknownHold:
            continue

    for authorization_id in (related.get("authorization_id"), resource.get("id")):
        if authorization_id:
            found = store.find_by_authorization(str(authorization_id))
            if found is not None:
                return found

    for order_id in (related.get("order_id"), resource.get("id")):
        if order_id:
            found = store.find_by_order(str(order_id))
            if found is not None:
                return found

    return None


def _captured(resource: dict[str, Any], hold: Hold) -> Money | None:
    """The captured amount, in the hold's own currency.

    A capture in a different currency than the hold is not arithmetic to be
    attempted -- `Money` refuses cross-currency operations by design -- so it is
    left unset and the detail line says so. Guessing here would put a wrong number
    in a financial record.
    """
    amount = resource.get("amount") or {}
    value, currency = amount.get("value"), amount.get("currency_code")
    if not value or currency != hold.amount.currency:
        return None
    try:
        return Money.from_paypal(str(value), str(currency))
    except (TypeError, ValueError):
        return None


def apply(
    store: Store, event: dict[str, Any], *, at: datetime | None = None
) -> WebhookOutcome:
    """Record what PayPal says happened. No network, no money, no new holds.

    Pure in the sense that matters: it reads an already-verified event and writes a
    transition. Verification lives at the route because it needs a PayPal client,
    and keeping it out of here is what lets the whole table be tested without one.
    """
    moment = at or datetime.now(timezone.utc)
    event_type = str(event.get("event_type") or "")
    event_id = event_id_of(event)
    resource = event.get("resource") or {}

    if event_type not in TERMINAL_EVENTS and event_type not in NOTED_EVENTS:
        return WebhookOutcome(
            event_id,
            event_type,
            "unhandled_event_type",
            detail="acknowledged; this gateway has no rule for this event type",
        )

    hold = locate(store, event)
    if hold is None:
        # Not an error on our side. The webhook subscription is per-application,
        # so a delivery can legitimately describe an order some other process
        # created. Recorded and acknowledged.
        return WebhookOutcome(
            event_id, event_type, "no_matching_hold", detail="no hold matches this resource"
        )

    if event_type in NOTED_EVENTS:
        return WebhookOutcome(
            event_id,
            event_type,
            "noted",
            decision_id=hold.decision_id,
            from_state=hold.state.value,
            to_state=hold.state.value,
            detail="recorded; no state change implied",
        )

    target = TERMINAL_EVENTS[event_type]
    if hold.state is target:
        # Already there, usually because the gateway's own call did this and the
        # webhook is the echo. Not a duplicate delivery and not a conflict.
        return WebhookOutcome(
            event_id,
            event_type,
            "already_in_state",
            decision_id=hold.decision_id,
            from_state=hold.state.value,
            to_state=target.value,
        )

    columns: dict[str, object] = {}
    detail = f"PayPal says {event_type}"
    if target is HoldState.HELD:
        if resource.get("id"):
            columns["authorization_id"] = str(resource["id"])
        expiry = resource.get("expiration_time")
        if expiry:
            columns["authorization_expires_at"] = str(expiry)
    elif target is HoldState.CAPTURED:
        if resource.get("id"):
            columns["capture_id"] = str(resource["id"])
        captured = _captured(resource, hold)
        if captured is not None:
            columns["captured_minor"] = captured.minor
        else:
            detail += " (capture amount not recorded: absent or a different currency)"

    try:
        moved = store.transition(hold.decision_id, target, detail=detail, at=moment, **columns)
    except IllegalTransition as exc:
        # Deliberately not forced: PayPal telling us a
        # captured hold is now held would mean our model of the money is wrong,
        # and overwriting the state would destroy the evidence of that. Reported
        # and acknowledged, so it shows up in the ledger as a disagreement rather
        # than being resolved in PayPal's favour by default.
        return WebhookOutcome(
            event_id,
            event_type,
            "illegal_transition",
            decision_id=hold.decision_id,
            from_state=hold.state.value,
            to_state=target.value,
            detail=str(exc),
        )

    return WebhookOutcome(
        event_id,
        event_type,
        "applied",
        decision_id=moved.decision_id,
        from_state=hold.state.value,
        to_state=moved.state.value,
        detail=detail,
    )
