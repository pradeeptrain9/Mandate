"""Two oracles: one that asks a carrier, one that never says yes.

`MerchantCarrier` talks to the merchant stub's shipping endpoint, which stands in
for a carrier API. In a real deployment this is where PayPal's Shipment Tracking
or a courier's webhook would go; the gateway cannot tell the difference, which is
the point of the Protocol.

`NeverDelivers` exists for tests and for the non-delivery scene, and it is worth
saying why it is not simply "return NEVER": a carrier that has genuinely never
heard of a shipment answers *the same way* as one whose API is down, and a demo
that conflated those would be demonstrating the easy case.
"""

from __future__ import annotations

import httpx

from . import Delivered, Delivery


class MerchantCarrier:
    """Ask the merchant's carrier endpoint about one shipment.

    An unreachable carrier yields UNKNOWN, not NEVER. The distinction decides
    whether a hold is released or left alone, and treating a network fault as
    proof of non-delivery would void holds for honest merchants during an outage.
    """

    name = "merchant-carrier"

    def __init__(self, base_url: str, *, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def check(self, *, decision_id: str, merchant_id: str) -> Delivery:
        url = f"/merchants/{merchant_id}/shipping/{decision_id}"
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as http:
                response = await http.get(url)
        except httpx.HTTPError as exc:
            return Delivery(Delivered.UNKNOWN, f"carrier unreachable: {type(exc).__name__}")

        if response.status_code == 404:
            # No such shipment. Not proof of anything: the merchant may not have
            # created it yet, and the reference may simply be unknown to them.
            return Delivery(Delivered.UNKNOWN, "carrier has no record of this shipment")
        if response.status_code >= 300:
            return Delivery(Delivered.UNKNOWN, f"carrier returned {response.status_code}")

        try:
            body = response.json()
        except ValueError:
            return Delivery(Delivered.UNKNOWN, "carrier returned something that is not JSON")

        reference = str(body.get("reference") or decision_id)
        status = str(body.get("status") or "")
        if body.get("delivered") is True or status == "delivered":
            return Delivery(Delivered.YES, f"carrier says {status or 'delivered'}", reference)
        if status == "never_shipped":
            return Delivery(Delivered.NEVER, "carrier says it was never shipped", reference)
        return Delivery(Delivered.NOT_YET, f"carrier says {status or 'in transit'}", reference)


class NeverDelivers:
    """Always NEVER. For the non-delivery scene and for tests."""

    name = "never-delivers"

    async def check(self, *, decision_id: str, merchant_id: str) -> Delivery:
        return Delivery(Delivered.NEVER, "this oracle reports non-delivery by construction")


class AlwaysDelivers:
    """Always YES. For the happy path in tests.

    Deliberately not the default anywhere. An oracle that cannot say no would make
    every capture unconditional, which is the failure this project is about.
    """

    name = "always-delivers"

    async def check(self, *, decision_id: str, merchant_id: str) -> Delivery:
        return Delivery(Delivered.YES, "this oracle confirms delivery by construction")
