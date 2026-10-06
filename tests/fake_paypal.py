"""A PayPal stand-in that enforces the rules the real one enforces.

Not a mock that records calls -- a small state machine that refuses what PayPal
refuses: capturing before the buyer approves, capturing twice after a final
capture, voiding something already captured, capturing more than was held. A
fake that accepts everything would let the gateway's own ordering bugs pass.

Built on httpx.MockTransport so it exercises the real `PayPalClient`, including
its headers, idempotency keys and error parsing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from mandate.providers.paypal import PayPalClient


@dataclass
class FakeOrder:
    order_id: str
    currency: str
    value: str
    status: str = "CREATED"
    decision_id: str = ""


@dataclass
class FakeAuthorization:
    authorization_id: str
    order_id: str
    currency: str
    value: str
    status: str = "CREATED"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    captured: Decimal = Decimal("0")

    @property
    def expires_at(self) -> datetime:
        # PayPal's documented window, so the gateway's expiry logic sees a
        # realistic figure rather than a round number we invented.
        return self.created_at + timedelta(days=29)


class FakePayPal:
    """Drive this from tests: `approve_buyer`, then let the gateway authorize."""

    def __init__(self, *, now: datetime | None = None) -> None:
        self.now = now or datetime.now(timezone.utc)
        self.orders: dict[str, FakeOrder] = {}
        self.authorizations: dict[str, FakeAuthorization] = {}
        self.captures: dict[str, dict] = {}
        self.request_ids: list[str] = []
        self.fail_next: tuple[int, dict] | None = None
        self._counter = 0

    # -- test controls ---------------------------------------------------

    def approve_buyer(self, order_id: str) -> None:
        """Stand in for the buyer logging into PayPal and approving."""
        self.orders[order_id].status = "APPROVED"

    def only_order(self) -> FakeOrder:
        assert len(self.orders) == 1, f"expected one order, have {len(self.orders)}"
        return next(iter(self.orders.values()))

    def only_authorization(self) -> FakeAuthorization:
        assert len(self.authorizations) == 1
        return next(iter(self.authorizations.values()))

    def break_next_call(self, status: int = 422, name: str = "UNPROCESSABLE_ENTITY") -> None:
        self.fail_next = (status, {"name": name, "details": [{"issue": name}]})

    def client(self) -> PayPalClient:
        return PayPalClient("cid", "secret", transport=httpx.MockTransport(self.handle))

    # -- the transport ---------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:04d}"

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 32400})

        if self.fail_next is not None:
            status, body = self.fail_next
            self.fail_next = None
            return httpx.Response(status, json=body)

        if rid := request.headers.get("paypal-request-id"):
            self.request_ids.append(rid)

        body = json.loads(request.content) if request.content else {}

        if path == "/v2/checkout/orders" and request.method == "POST":
            return self._create_order(body)
        if path.endswith("/authorize") and request.method == "POST":
            return self._authorize(path.split("/")[-2])
        if path.endswith("/capture") and request.method == "POST":
            return self._capture(path.split("/")[-2], body)
        if path.endswith("/void") and request.method == "POST":
            return self._void(path.split("/")[-2])
        if path.endswith("/reauthorize") and request.method == "POST":
            return self._reauthorize(path.split("/")[-2], body)
        if path.startswith("/v2/checkout/orders/") and request.method == "GET":
            return self._get_order(path.rsplit("/", 1)[-1])
        if path.startswith("/v2/payments/authorizations/") and request.method == "GET":
            return self._get_authorization(path.rsplit("/", 1)[-1])
        if path == "/v1/notifications/verify-webhook-signature":
            return httpx.Response(200, json={"verification_status": "SUCCESS"})

        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "path": path})

    # -- endpoints -------------------------------------------------------

    def _create_order(self, body: dict) -> httpx.Response:
        unit = body["purchase_units"][0]
        amount = unit["amount"]
        order = FakeOrder(
            order_id=self._next_id("ORDER"),
            currency=amount["currency_code"],
            value=amount["value"],
            decision_id=unit.get("custom_id", ""),
        )
        self.orders[order.order_id] = order
        return httpx.Response(
            201,
            json={
                "id": order.order_id,
                "status": order.status,
                "links": [
                    {
                        "rel": "payer-action",
                        "href": f"https://sandbox.paypal.test/checkoutnow?token={order.order_id}",
                    }
                ],
            },
        )

    def _get_order(self, order_id: str) -> httpx.Response:
        order = self.orders.get(order_id)
        if order is None:
            return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
        return httpx.Response(200, json={"id": order.order_id, "status": order.status})

    def _authorize(self, order_id: str) -> httpx.Response:
        order = self.orders.get(order_id)
        if order is None:
            return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
        if order.status != "APPROVED":
            # The real API refuses this, and the gateway must not assume a hold
            # exists just because it asked for one.
            return httpx.Response(
                422,
                json={
                    "name": "UNPROCESSABLE_ENTITY",
                    "details": [{"issue": "ORDER_NOT_APPROVED"}],
                },
            )
        auth = FakeAuthorization(
            authorization_id=self._next_id("AUTH"),
            order_id=order_id,
            currency=order.currency,
            value=order.value,
            created_at=self.now,
        )
        self.authorizations[auth.authorization_id] = auth
        order.status = "COMPLETED"
        return httpx.Response(
            201,
            json={
                "id": order_id,
                "status": "COMPLETED",
                "purchase_units": [{"payments": {"authorizations": [_auth_json(auth)]}}],
            },
        )

    def _get_authorization(self, authorization_id: str) -> httpx.Response:
        auth = self.authorizations.get(authorization_id)
        if auth is None:
            return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
        return httpx.Response(200, json=_auth_json(auth))

    def _capture(self, authorization_id: str, body: dict) -> httpx.Response:
        auth = self.authorizations.get(authorization_id)
        if auth is None:
            return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
        if auth.status == "CAPTURED":
            return httpx.Response(
                422,
                json={
                    "name": "UNPROCESSABLE_ENTITY",
                    "details": [{"issue": "AUTHORIZATION_ALREADY_CAPTURED"}],
                },
            )
        if auth.status == "VOIDED":
            return httpx.Response(
                422,
                json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "AUTHORIZATION_VOIDED"}]},
            )
        asked = Decimal(body["amount"]["value"])
        if asked > Decimal(auth.value):
            return httpx.Response(
                422,
                json={
                    "name": "UNPROCESSABLE_ENTITY",
                    "details": [{"issue": "CAPTURE_AMOUNT_EXCEEDS_AUTHORIZED_AMOUNT"}],
                },
            )
        capture_id = self._next_id("CAPTURE")
        auth.captured = asked
        auth.status = "CAPTURED" if body.get("final_capture", True) else "PARTIALLY_CAPTURED"
        payload = {
            "id": capture_id,
            "status": "COMPLETED",
            "amount": {"currency_code": auth.currency, "value": str(asked)},
            "final_capture": bool(body.get("final_capture", True)),
        }
        self.captures[capture_id] = payload
        return httpx.Response(201, json=payload)

    def _void(self, authorization_id: str) -> httpx.Response:
        auth = self.authorizations.get(authorization_id)
        if auth is None:
            return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
        if auth.status == "CAPTURED":
            return httpx.Response(
                422,
                json={
                    "name": "UNPROCESSABLE_ENTITY",
                    "details": [{"issue": "AUTHORIZATION_ALREADY_CAPTURED"}],
                },
            )
        auth.status = "VOIDED"
        return httpx.Response(204)

    def _reauthorize(self, authorization_id: str, body: dict) -> httpx.Response:
        auth = self.authorizations.get(authorization_id)
        if auth is None or auth.status != "CREATED":
            return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY"})
        # A reauthorization is a NEW authorization id with a restarted window.
        fresh = FakeAuthorization(
            authorization_id=self._next_id("AUTH"),
            order_id=auth.order_id,
            currency=auth.currency,
            value=body["amount"]["value"],
            created_at=self.now,
        )
        self.authorizations[fresh.authorization_id] = fresh
        auth.status = "VOIDED"
        return httpx.Response(201, json=_auth_json(fresh))


def _auth_json(auth: FakeAuthorization) -> dict:
    return {
        "id": auth.authorization_id,
        "status": auth.status,
        "amount": {"currency_code": auth.currency, "value": auth.value},
        "create_time": auth.created_at.isoformat().replace("+00:00", "Z"),
        "expiration_time": auth.expires_at.isoformat().replace("+00:00", "Z"),
        "links": [
            {
                "rel": "up",
                "href": f"https://api.sandbox.paypal.test/v2/checkout/orders/{auth.order_id}",
            }
        ],
    }
