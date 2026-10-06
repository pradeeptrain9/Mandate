"""PayPal REST, for the parts that hold money.

The PayPal Agent Toolkit covers a lot -- invoices, subscriptions, tracking,
disputes, reporting -- and this project uses it for those. It does not expose
`authorize`, `void` or `reauthorize`, and those three are the entire mechanism by
which Mandate holds funds without taking them. So the hold lifecycle is spoken
in raw REST here, and the toolkit is used where it fits.

What "held" means precisely, because the word escrow would be wrong: an
authorization reserves funds on the buyer's funding instrument. PayPal does not
take custody and neither do we. Capture moves the money; void releases it; doing
nothing releases it when the authorization expires. The honor period is 3 days,
during which capture has the highest success rate, and the authorization itself
is valid for 29 days with reauthorization available after day 3.

Every state-changing call carries a `PayPal-Request-Id`. Retrying a create-order
or a capture without one is how a network timeout turns into two charges.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

SANDBOX = "https://api-m.sandbox.paypal.com"
LIVE = "https://api-m.paypal.com"


class PayPalError(RuntimeError):
    """A non-2xx response, carrying enough of the body to act on.

    PayPal's error bodies are genuinely useful -- `name`, `details[].issue` --
    and collapsing them into a status code throws away the only thing that says
    which field was wrong.
    """

    def __init__(self, status: int, body: dict[str, Any] | str, *, operation: str) -> None:
        self.status = status
        self.body = body
        self.operation = operation
        name = body.get("name") if isinstance(body, dict) else None
        issues = (
            ", ".join(
                f"{d.get('issue')}@{d.get('field', '-')}" for d in (body.get("details") or [])
            )
            if isinstance(body, dict)
            else ""
        )
        super().__init__(f"{operation} failed with {status}: {name or body}{f' ({issues})' if issues else ''}")

    @property
    def issues(self) -> list[str]:
        if not isinstance(self.body, dict):
            return []
        return [str(d.get("issue")) for d in (self.body.get("details") or [])]


@dataclass(frozen=True)
class Authorization:
    """A hold. `expires_at` is PayPal's figure, not one we computed."""

    authorization_id: str
    order_id: str
    status: str
    amount_value: str
    currency: str
    expires_at: datetime | None
    created_at: datetime | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class Capture:
    capture_id: str
    status: str
    amount_value: str
    currency: str
    final_capture: bool
    raw: dict[str, Any]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    # PayPal returns RFC 3339 with a trailing Z, which fromisoformat accepts
    # from 3.11 onwards.
    return datetime.fromisoformat(value)


class PayPalClient:
    """Thin async client. One job per method, no retries hidden inside.

    Retry policy lives with the caller because the right answer differs: a failed
    token fetch should retry, a failed capture should be re-driven through the
    same `PayPal-Request-Id` or escalated, never silently repeated with a new one.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = SANDBOX,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("PayPal client id and secret are both required")
        self.base_url = base_url.rstrip("/")
        self._auth = (client_id, client_secret)
        self._http = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, transport=transport
        )
        self._token: str | None = None

    async def __aenter__(self) -> "PayPalClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- auth ------------------------------------------------------------

    async def token(self, *, refresh: bool = False) -> str:
        if self._token and not refresh:
            return self._token
        response = await self._http.post(
            "/v1/oauth2/token",
            auth=self._auth,
            data={"grant_type": "client_credentials"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        body = _body(response)
        if response.status_code != 200:
            raise PayPalError(response.status_code, body, operation="oauth2/token")
        self._token = str(body["access_token"])
        return self._token

    async def _call(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: dict[str, Any] | None = None,
        request_id: str | None = None,
        representation: bool = True,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {await self.token()}",
            "Content-Type": "application/json",
        }
        if representation:
            headers["Prefer"] = "return=representation"
        if request_id:
            # Idempotency. PayPal replays the original response for a repeated
            # id, which is what makes a timeout safe to retry.
            headers["PayPal-Request-Id"] = request_id

        response = await self._http.request(method, path, headers=headers, json=json_body)

        if response.status_code == 401:
            # The token expired mid-flight. Refresh once and retry; a second 401
            # is a credentials problem, not a staleness problem.
            headers["Authorization"] = f"Bearer {await self.token(refresh=True)}"
            response = await self._http.request(method, path, headers=headers, json=json_body)

        body = _body(response)
        if response.status_code >= 300:
            raise PayPalError(response.status_code, body, operation=operation)
        return body if isinstance(body, dict) else {}

    # -- the hold lifecycle ---------------------------------------------

    async def create_authorization_order(
        self,
        *,
        currency: str,
        value: str,
        items: list[dict[str, Any]],
        decision_id: str,
        return_url: str,
        cancel_url: str,
        request_id: str | None = None,
        brand_name: str = "Mandate",
    ) -> dict[str, Any]:
        """Create an order with `intent=AUTHORIZE`. Nothing is held until the
        buyer approves and `authorize_order` runs."""
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [
                {
                    "reference_id": decision_id,
                    # Stamps our decision id onto the PayPal-side transaction, so
                    # a row in their dashboard can be traced back to the rule
                    # trace that permitted it.
                    "custom_id": decision_id,
                    "amount": {
                        "currency_code": currency,
                        "value": value,
                        "breakdown": {"item_total": {"currency_code": currency, "value": value}},
                    },
                    "items": items,
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "brand_name": brand_name,
                        "user_action": "PAY_NOW",
                        "return_url": return_url,
                        "cancel_url": cancel_url,
                    }
                }
            },
        }
        return await self._call(
            "POST",
            "/v2/checkout/orders",
            operation="create order",
            json_body=body,
            request_id=request_id or f"mandate-order-{decision_id}",
        )

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self._call(
            "GET", f"/v2/checkout/orders/{order_id}", operation="get order", representation=False
        )

    async def authorize_order(self, order_id: str, *, request_id: str | None = None) -> Authorization:
        """Place the hold. Valid for 29 days; honor period is the first 3."""
        body = await self._call(
            "POST",
            f"/v2/checkout/orders/{order_id}/authorize",
            operation="authorize order",
            json_body={},
            request_id=request_id or f"mandate-auth-{order_id}",
        )
        return _authorization_from_order(body)

    async def get_authorization(self, authorization_id: str) -> Authorization:
        body = await self._call(
            "GET",
            f"/v2/payments/authorizations/{authorization_id}",
            operation="get authorization",
            representation=False,
        )
        return _authorization_from_payment(body)

    async def capture_authorization(
        self,
        authorization_id: str,
        *,
        currency: str,
        value: str,
        final_capture: bool = True,
        request_id: str | None = None,
    ) -> Capture:
        """Take the money. Any amount up to the authorized total; the remainder
        is voided by `final_capture=True` or left to expire."""
        body = await self._call(
            "POST",
            f"/v2/payments/authorizations/{authorization_id}/capture",
            operation="capture authorization",
            json_body={
                "amount": {"currency_code": currency, "value": value},
                "final_capture": final_capture,
            },
            request_id=request_id or f"mandate-capture-{authorization_id}",
        )
        amount = body.get("amount") or {}
        return Capture(
            capture_id=str(body.get("id", "")),
            status=str(body.get("status", "")),
            amount_value=str(amount.get("value", value)),
            currency=str(amount.get("currency_code", currency)),
            final_capture=bool(body.get("final_capture", final_capture)),
            raw=body,
        )

    async def void_authorization(self, authorization_id: str) -> None:
        """Release the hold. Cheaper than refunding a capture and leaves no
        money movement to reverse."""
        await self._call(
            "POST",
            f"/v2/payments/authorizations/{authorization_id}/void",
            operation="void authorization",
            json_body=None,
            representation=False,
        )

    async def reauthorize(
        self, authorization_id: str, *, currency: str, value: str
    ) -> Authorization:
        """Extend a hold past the honor period. Returns a NEW authorization id;
        every later capture must use it, not the original."""
        body = await self._call(
            "POST",
            f"/v2/payments/authorizations/{authorization_id}/reauthorize",
            operation="reauthorize",
            json_body={"amount": {"currency_code": currency, "value": value}},
        )
        return _authorization_from_payment(body)

    async def refund_capture(
        self, capture_id: str, *, currency: str, value: str, note: str = ""
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"amount": {"currency_code": currency, "value": value}}
        if note:
            payload["note_to_payer"] = note
        return await self._call(
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            operation="refund capture",
            json_body=payload,
            request_id=f"mandate-refund-{capture_id}",
        )

    # -- webhooks --------------------------------------------------------

    async def verify_webhook(
        self, *, headers: dict[str, str], raw_body: bytes, webhook_id: str
    ) -> bool:
        """Ask PayPal whether this delivery is authentic.

        `raw_body` is spliced in verbatim rather than parsed and re-serialised.
        Verification is over the exact bytes PayPal signed, and `json.loads`
        followed by `json.dumps` is not guaranteed to reproduce them -- key
        order, unicode escaping and float formatting can all shift. So the
        request body is built as a string with the original bytes embedded.
        """
        required = {
            "paypal-auth-algo": "auth_algo",
            "paypal-cert-url": "cert_url",
            "paypal-transmission-id": "transmission_id",
            "paypal-transmission-sig": "transmission_sig",
            "paypal-transmission-time": "transmission_time",
        }
        lowered = {k.lower(): v for k, v in headers.items()}
        missing = [h for h in required if h not in lowered]
        if missing:
            # A delivery without the signature headers is not a verification
            # failure to be retried -- it is not a PayPal delivery.
            return False

        fields = {field: lowered[header] for header, field in required.items()}
        fields["webhook_id"] = webhook_id
        prefix = json.dumps(fields)[:-1]  # drop the closing brace
        body = f'{prefix},"webhook_event":{raw_body.decode("utf-8")}}}'

        response = await self._http.post(
            "/v1/notifications/verify-webhook-signature",
            headers={
                "Authorization": f"Bearer {await self.token()}",
                "Content-Type": "application/json",
            },
            content=body.encode("utf-8"),
        )
        payload = _body(response)
        if response.status_code >= 300:
            raise PayPalError(response.status_code, payload, operation="verify webhook")
        return isinstance(payload, dict) and payload.get("verification_status") == "SUCCESS"


# -- response shaping -------------------------------------------------------


def _body(response: httpx.Response) -> dict[str, Any] | str:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return response.text


def _authorization_from_order(order: dict[str, Any]) -> Authorization:
    units = order.get("purchase_units") or []
    authorizations = (units[0].get("payments", {}) if units else {}).get("authorizations") or []
    if not authorizations:
        raise PayPalError(
            200, order, operation="authorize order (no authorization in response)"
        )
    return _authorization_from_payment(authorizations[0], order_id=str(order.get("id", "")))


def _authorization_from_payment(
    payment: dict[str, Any], *, order_id: str = ""
) -> Authorization:
    amount = payment.get("amount") or {}
    if not order_id:
        for link in payment.get("links") or []:
            if link.get("rel") == "up" and "/checkout/orders/" in str(link.get("href", "")):
                order_id = str(link["href"]).rsplit("/", 1)[-1]
    return Authorization(
        authorization_id=str(payment.get("id", "")),
        order_id=order_id,
        status=str(payment.get("status", "")),
        amount_value=str(amount.get("value", "")),
        currency=str(amount.get("currency_code", "")),
        expires_at=_parse_time(payment.get("expiration_time")),
        created_at=_parse_time(payment.get("create_time")),
        raw=payment,
    )


def approval_link(order: dict[str, Any]) -> str | None:
    """Where the buyer goes to approve.

    `payer-action` is what a `payment_source.paypal` order returns; `approve` is
    the older shape. Checking both means the caller does not have to care which
    code path built the order.
    """
    for rel in ("payer-action", "approve"):
        for link in order.get("links") or []:
            if link.get("rel") == rel:
                return str(link.get("href"))
    return None


def new_request_id(prefix: str = "mandate") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"
