"""The surface a person uses, as distinct from the one an agent or an operator uses.

Three surfaces now, and the separation is the same argument each time: the
capability someone should not have is absent from the interface they can reach,
rather than guarded inside it.

  /v1/agent/*  an agent. Can ask for money. Cannot approve, capture, void or edit.
  /v1/ops/*    an approver. Can decide the queue and change the rules.
  /app/*       a person. Can ask for things and see what happened to their own
               requests. Cannot approve their own.

That last one is the point of this module. A requester who could approve their own
request would make the approval threshold decorative in exactly the way an agent
approving its own request would -- the threshold exists to put a second person in
the loop, and one person wearing both hats is not two people.

So `/app/requests` is scoped to the caller's own username, and the approve and
decline routes live on the operator surface behind a role check. A requester
following the approval link for their own purchase is refused there, not here.
"""

from __future__ import annotations

from typing import Any

from .accounts import User


def own_requests(store: Any, username: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """What this person asked for, and where each request got to.

    Filtered in SQL rather than in the template. A view that fetches everything and
    hides most of it is one careless edit away from showing a person someone else's
    spending, and the careless edit usually happens in the template.
    """
    with store.transaction() as db:
        rows = db.execute(
            "SELECT decision_id, state, merchant_name, currency, amount_minor, "
            "       engine_outcome, requested_at, updated_at, agent_reason, last_error "
            "FROM holds WHERE agent_id = ? ORDER BY requested_at DESC LIMIT ?",
            (username, limit),
        ).fetchall()
    return [
        {
            "decision_id": r[0],
            "state": r[1],
            "merchant_name": r[2],
            "currency": r[3],
            "amount": f"{r[4] // 100}.{r[4] % 100:02d}",
            "outcome": r[5],
            "requested_at": r[6],
            "updated_at": r[7],
            "asked_for": r[8],
            "last_error": r[9],
            "status": STATUS.get(r[1], (r[1].replace("_", " "), "")),
        }
        for r in rows
    ]


#: What each state means to the person who asked, rather than to the state machine.
#: A requester reading "awaiting_buyer" learns nothing; the words below are what
#: they would want someone to say out loud.
STATUS: dict[str, tuple[str, str]] = {
    "received": ("Being checked", "The rules are being applied to this request."),
    "refused": ("Refused", "This was outside the spending rules. Nothing was charged."),
    "awaiting_human": ("Waiting for approval", "Someone has to say yes before anything is reserved."),
    "declined_by_human": ("Declined", "A person declined this. Nothing was charged."),
    "awaiting_buyer": ("Ready to pay", "Approved. Confirm at PayPal to reserve the money."),
    "buyer_cancelled": ("Cancelled", "You cancelled at PayPal. Nothing was charged."),
    "held": ("Money held", "Reserved at PayPal, not taken. It is released if nothing arrives."),
    "captured": ("Paid", "The money has moved."),
    "voided": ("Released", "The hold was released. Nothing was taken."),
    "refunded": ("Refunded", "The money was returned."),
    "expired": ("Expired", "The hold lapsed before anything was delivered. Nothing was taken."),
    "failed": ("Could not complete", "Something went wrong on the payment side."),
}


def visible_to(user: User, hold_agent_id: str) -> bool:
    """An approver sees everything; a requester sees their own.

    Deliberately not "an approver sees everything except their own": a team where
    the only approver cannot see their own requests is a team that shares a login.
    """
    return user.can_approve or hold_agent_id == user.username
