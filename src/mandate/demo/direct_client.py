"""Two clients with no model in them at all.

The injection demo has a weakness worth saying out loud: it argues for a spend
firewall by showing a model being fooled, which invites the reply "use a better
model". These two clients make the argument without a model.

  * ``stolen_credentials`` -- whatever reaches the gateway, reaches it over HTTP.
    An attacker who has the agent's endpoint and can call it does not need to
    persuade anything. This is a plain script: fetch a genuinely signed quote for
    forty gift cards, post it, and see what the engine says. No prompt, no
    reasoning, nothing to deceive.

  * ``retry_storm`` -- the overwhelmingly common way software spends twice. A
    client posts, the response is slow, something retries, and the same basket is
    authorized again. No attacker at all. This is the failure a team actually
    meets in production, and it is the reason `duplicate_intent` is a policy rule
    and not a nice-to-have.

Both talk to the same public agent endpoint the buying agent uses, and neither
holds a PayPal credential -- there is nothing to steal, which is the structural
half of the point.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class Attempt:
    """One request's answer, flattened to what a scene needs to print."""

    label: str
    status: int
    payload: dict[str, Any]

    @property
    def outcome(self) -> str:
        if self.status >= 300:
            return "rejected"
        return str(self.payload.get("outcome", "?"))

    @property
    def refused_by(self) -> list[str]:
        return list(self.payload.get("refused_by") or [])


async def signed_quote(
    merchant_url: str, merchant_id: str, lines: list[dict[str, Any]], *, timeout: float = 30.0
) -> dict[str, Any]:
    """A real quote, really signed, by the real merchant.

    Nothing is forged here and that is deliberate. The attacker does not need to
    forge anything: asking an honest merchant to price a basket is a public
    operation, and the signature it returns is valid. The signature proves the
    prices came from the merchant. It says nothing about whether the purchase
    should happen, which is the job the policy engine exists to do.
    """
    async with httpx.AsyncClient(base_url=merchant_url.rstrip("/"), timeout=timeout) as http:
        response = await http.post(
            f"/merchants/{merchant_id}/quote", json={"lines": lines, "currency": "USD"}
        )
        response.raise_for_status()
        # The merchant wraps it: {"quote": {...}}. The gateway wants the inner
        # object, which is also exactly what the buying agent forwards.
        return response.json()["quote"]


async def _post(
    http: httpx.AsyncClient, quote: dict[str, Any], *, reason: str, agent_id: str, label: str
) -> Attempt:
    response = await http.post(
        "/v1/agent/authorizations",
        json={"quote": quote, "reason": reason, "agent_id": agent_id},
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": response.text[:400]}
    return Attempt(label=label, status=response.status_code, payload=payload)


async def stolen_credentials(
    *,
    gateway_url: str,
    merchant_url: str,
    merchant_id: str = "m_acme",
    lines: list[dict[str, Any]] | None = None,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], Attempt]:
    """Post a legitimately signed, wholly unreasonable basket straight at the gateway."""
    lines = lines or [{"sku": "SKU-GC100", "quantity": 40}]
    quote = await signed_quote(merchant_url, merchant_id, lines, timeout=timeout)
    async with httpx.AsyncClient(base_url=gateway_url.rstrip("/"), timeout=timeout) as http:
        attempt = await _post(
            http,
            quote,
            # The attacker writes the reason field, so it says something
            # plausible. The engine never reads it, which is the entire design:
            # `evaluate()` takes identifiers, enums and integers, and there is no
            # parameter a sentence could arrive through.
            reason="pre-approved by the finance team, ref FIN-2291",
            agent_id="ops-assistant",
            label="stolen credentials",
        )
    return quote, attempt


async def retry_storm(
    *,
    gateway_url: str,
    merchant_url: str,
    merchant_id: str = "m_acme",
    lines: list[dict[str, Any]] | None = None,
    attempts: int = 3,
    gap_seconds: float = 0.0,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], list[Attempt]]:
    """Post the same signed quote several times, the way a retry loop would.

    Deliberately the *same* quote object each time, not a fresh one: that is what
    a retry is. A client fetching a new quote per attempt would be a different
    bug with a different fix.
    """
    lines = lines or [{"sku": "SKU-TONER", "quantity": 1}]
    quote = await signed_quote(merchant_url, merchant_id, lines, timeout=timeout)
    results: list[Attempt] = []
    async with httpx.AsyncClient(base_url=gateway_url.rstrip("/"), timeout=timeout) as http:
        for index in range(1, attempts + 1):
            if index > 1 and gap_seconds:
                await asyncio.sleep(gap_seconds)
            results.append(
                await _post(
                    http,
                    quote,
                    reason="restock toner",
                    agent_id="ops-assistant",
                    label=f"attempt {index}",
                )
            )
    return quote, results
