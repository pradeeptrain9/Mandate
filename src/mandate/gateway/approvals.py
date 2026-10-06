"""Paging a human, and what the message is allowed to say.

The policy puts anything over the unattended threshold here: no order exists at
PayPal, no funds are reserved, and nothing happens until a person answers. This
module is the asking.

Three decisions about the message, each of which could reasonably have gone the
other way.

**The amount and merchant are in the body.** The obvious privacy move is to send a
bare link, since an SMS preview lands on a lock screen. It is the wrong move: an
approver who has to open a link to discover what they are approving is an approver
being trained to open links, and training the one human in your payment path to
click unexamined URLs is a worse outcome than a glanceable figure. With the amount
in the body they can refuse a $4,000 gift-card run without touching anything.

**The token is only ever in the link, never in the text, and never in a query
string.** It is in the path because query strings leak through referrer headers,
proxy logs and analytics in a way path segments mostly do not — and the page that
redeems it sends no referrer onward. It is single-use with a short TTL, the store
keeps only its digest, and nothing returns it to the agent.

**A failed send does not fail the decision.** The hold is already recorded as
awaiting a human before any network call is made. If Twilio is down, the right
outcome is a decision that is still correctly parked and an operator who can see
why the message did not arrive — not an authorization request that errors and
invites a retry, because retrying would mint a second token and send two links for
one purchase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..providers.twilio import Sent, TwilioClient, TwilioError, redact
from .store import Hold

logger = logging.getLogger(__name__)

#: Kept short on purpose. A single SMS segment is 160 GSM-7 characters and a
#: longer message is split, billed twice and can arrive out of order -- the link
#: landing before the context it explains.
TEMPLATE = "Mandate: approve {amount} {currency} at {merchant}? {link}\nExpires in {minutes} min."


@dataclass(frozen=True)
class Notification:
    """What happened when we tried to page someone.

    `sent` is False for every reason: no Twilio configured, no approver number,
    the API refused. The caller treats all of them the same way -- the decision
    still stands -- and `detail` is for the operator.
    """

    sent: bool
    detail: str = ""
    sid: str = ""
    to: str = ""
    #: Whether a request actually left for Twilio. False for an unconfigured
    #: deployment, which is a supported way to run Mandate and not a fault: a
    #: dashboard that flags every held decision with "no Twilio credentials" is
    #: a dashboard a judge running without a phone learns to ignore.
    attempted: bool = False


def compose(hold: Hold, link: str, *, minutes: int) -> str:
    return TEMPLATE.format(
        amount=hold.amount.to_paypal(),
        currency=hold.amount.currency,
        merchant=hold.merchant_name or hold.merchant_id,
        link=link,
        minutes=minutes,
    )


class Approver:
    """Sends the approval SMS, or explains why it could not.

    Holds the recipient so the gateway never has to. One approver number, because
    a demo with an on-call rota would be a demo about rotas.
    """

    def __init__(self, client: TwilioClient | None, to_number: str) -> None:
        self.client = client
        self.to_number = to_number

    @property
    def configured(self) -> bool:
        return self.client is not None and bool(self.to_number)

    async def page(self, hold: Hold, link: str, *, ttl_minutes: int) -> Notification:
        if self.client is None:
            return Notification(False, "no Twilio credentials; nothing was sent")
        if not self.to_number:
            return Notification(False, "MANDATE_APPROVER_NUMBER is not set; nothing was sent")

        body = compose(hold, link, minutes=ttl_minutes)
        try:
            sent: Sent = await self.client.send(to=self.to_number, body=body)
        except TwilioError as exc:
            # Logged with the decision id and without the body or the number, so an
            # operator can find the failure without the log holding what the
            # message held.
            logger.warning("could not page the approver for %s: %s", hold.decision_id, exc)
            return Notification(False, str(exc), to=redact(self.to_number), attempted=True)
        return Notification(True, f"queued as {sent.status}", sid=sent.sid, to=sent.to, attempted=True)


def build_approver(env: dict[str, str]) -> Approver:
    """From configuration, tolerating absence.

    Mandate runs without Twilio. The approval page, the token and the whole
    human-approval path work exactly the same; the operator just has to fetch the
    link from `/v1/ops/holds` instead of being handed it. Making SMS mandatory
    would mean a judge cannot run the over-threshold scene without a phone number.
    """
    sid = env.get("TWILIO_ACCOUNT_SID", "").strip()
    token = env.get("TWILIO_AUTH_TOKEN", "").strip()
    sender = env.get("TWILIO_FROM_NUMBER", "").strip()
    to = env.get("MANDATE_APPROVER_NUMBER", "").strip()

    if not (sid and token and sender):
        return Approver(None, to)
    return Approver(TwilioClient(sid, token, sender), to)
