"""Twilio, for the one message that matters: asking a human about money.

Raw REST over httpx rather than the vendor SDK, for the same reason the PayPal
provider is: `httpx` is already a dependency and already carries this project's
timeout and error conventions, and the surface used here is one POST.

Two things are handled with more care than a demo strictly needs, because both
are ways this could leak something.

**Numbers are redacted in every log line and every error.** A phone number is
personal data and an approver's number is the one piece of personal data this
system holds. It appears in the request and nowhere else.

**The message body is never logged.** It names a merchant and an amount, which is
exactly the information an approver is being asked to keep private, and logs are
the least private place in any deployment.

Setup failures are classified rather than passed through raw. Twilio's trial
restrictions produce two different errors with two different fixes, and an
operator who is told "21608" has to go and look it up -- which is the same
mistake as telling someone to tick a dashboard setting for a malformed request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.twilio.com"

#: Twilio errors an operator can actually act on, and what to do. The code is
#: carried too, so the message never replaces the fact.
HINTS: dict[int, str] = {
    21608: (
        "this is a trial account and the recipient is not verified. Add the "
        "approver's number under Verified Caller IDs in the Twilio console -- you "
        "need the handset to receive the code."
    ),
    21607: (
        "a trial account can only send from its own trial number. Set "
        "TWILIO_FROM_NUMBER to the number Twilio issued you."
    ),
    572006: (
        "this is a trial account, and a trial cannot send a custom message body at "
        "all -- `Body` must be the NAME of a Twilio-provided template (sms_2fa, "
        "sms_account_alerts, ...) whose text Twilio chooses. There is nowhere to "
        "put the approval link, so the approval SMS cannot be delivered on a trial "
        "account. Upgrade the account, or run without SMS and mint the link with "
        "POST /v1/ops/holds/<decision_id>/approval-link -- the approval path works "
        "identically either way. "
        "https://www.twilio.com/docs/usage/trials/try-out-sms"
    ),
    30044: (
        "a trial account limits how many segments one message may use, and this "
        "message was split. Shorten it, or upgrade. Note a trial also prepends "
        "\"Sent from your Twilio trial account\" to every body, which costs about "
        "40 of the 160 characters in a segment."
    ),
    21211: (
        "MANDATE_APPROVER_NUMBER is not valid E.164. It needs the country code and "
        "no spaces or dashes, e.g. +14155550123."
    ),
    21212: "TWILIO_FROM_NUMBER is not a valid, SMS-capable number on this account.",
    21610: (
        "this recipient replied STOP and is unsubscribed; Twilio will not deliver to "
        "them and nothing on this side can override that. The handset must reply "
        "START, or point MANDATE_APPROVER_NUMBER at someone else."
    ),
    20003: "authentication failed. Check TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.",
}


def redact(number: str) -> str:
    """`+14155550123` -> `+1******0123`. Enough to tell two numbers apart, not
    enough to call one."""
    if not number:
        return "(none)"
    if len(number) <= 6:
        return "*" * len(number)
    return f"{number[:2]}{'*' * (len(number) - 6)}{number[-4:]}"


class TwilioError(RuntimeError):
    """A send that failed, with the operator's next step attached when there is one."""

    def __init__(self, status: int, code: int | None, message: str, hint: str = "") -> None:
        self.status = status
        self.code = code
        self.hint = hint
        detail = f"Twilio {status}"
        if code:
            detail += f" ({code})"
        detail += f": {message}"
        if hint:
            detail += f"\n  → {hint}"
        super().__init__(detail)


@dataclass(frozen=True)
class Sent:
    sid: str
    status: str
    to: str  # already redacted; the real number is not kept


class TwilioClient:
    """One method. Send a message, or say clearly why not."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        *,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not (account_sid and auth_token and from_number):
            raise ValueError(
                "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER are all required"
            )
        self.account_sid = account_sid
        self.from_number = from_number
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            transport=transport,
            auth=(account_sid, auth_token),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send(self, *, to: str, body: str) -> Sent:
        """Send one SMS. The body is never logged."""
        try:
            response = await self._http.post(
                f"/2010-04-01/Accounts/{self.account_sid}/Messages.json",
                data={"To": to, "From": self.from_number, "Body": body},
            )
        except httpx.HTTPError as exc:
            # Same lesson as the Gemini backend: several httpx exceptions
            # stringify to nothing, so the type and the destination carry the
            # message instead.
            raise TwilioError(
                0, None, f"{type(exc).__name__} sending to {redact(to)}: {exc or 'no detail'}"
            ) from exc

        payload: dict[str, Any] = {}
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.status_code >= 300:
            code = payload.get("code")
            code = int(code) if isinstance(code, (int, str)) and str(code).isdigit() else None
            raise TwilioError(
                response.status_code,
                code,
                payload.get("message") or response.text[:200],
                HINTS.get(code or 0, ""),
            )

        sent = Sent(
            sid=str(payload.get("sid") or ""),
            status=str(payload.get("status") or "queued"),
            to=redact(to),
        )
        # Destination redacted, body absent. A delivery receipt is worth having in
        # the log; the contents of the message are not.
        logger.info("approval SMS %s to %s (%s)", sent.sid, sent.to, sent.status)
        return sent
