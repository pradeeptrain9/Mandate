"""The ledger's last column: what happened after the money moved.

Everything else in this project is about the moment before a purchase. This is the
one view that looks the other way -- a capture that the buyer later disputed is the
clearest possible evidence that a decision which passed every rule was still the
wrong decision, and a firewall that never looks at its own outcomes cannot learn
that it was wrong.

Read-only, and read through the PayPal Agent Toolkit rather than raw REST. That is
deliberate rather than inconsistent: the toolkit does not expose authorize, void or
reauthorize, which is why the hold lifecycle speaks REST, but it covers merchant-side
reporting well and this is merchant-side reporting. Using it here and not there is
the honest division, and saying so is better than pretending one of the two was
enough.

Not stored. A dispute's state lives at PayPal and changes without telling us; a copy
in our database would be a second source of truth that is wrong more often than it is
right, and the hold table has no business holding a field only PayPal can update. So
this joins on demand and tolerates the lookup failing -- an operator who cannot reach
the dispute API should still get the ledger, with the column saying it does not know
rather than saying there are none.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..providers.toolkit import Toolkit, ToolkitError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Disputes:
    """What PayPal says, and whether it managed to say anything.

    `reachable` is the field that matters. Without it an empty `by_capture` means
    both "nothing is disputed" and "the lookup failed", and those must never read
    the same on a dashboard -- one is good news and the other is no news.
    """

    reachable: bool
    detail: str = ""
    by_capture: dict[str, dict[str, Any]] = field(default_factory=dict)

    def for_capture(self, capture_id: str | None) -> dict[str, Any] | None:
        if not capture_id:
            return None
        return self.by_capture.get(capture_id)


def _parse(payload: Any) -> list[dict[str, Any]]:
    """The toolkit returns JSON as a string for most methods and prose for some."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    if isinstance(payload, dict):
        items = payload.get("items") or payload.get("disputes") or []
        return [i for i in items if isinstance(i, dict)]
    if isinstance(payload, list):
        return [i for i in payload if isinstance(i, dict)]
    return []


def _captures_of(dispute: dict[str, Any]) -> list[str]:
    """Which of our captures a dispute points at.

    PayPal nests the id under `disputed_transactions[].seller_transaction_id`, which
    is the capture id we stored. Read defensively: this is the one place where a
    shape change at PayPal would otherwise raise inside a dashboard request.
    """
    found: list[str] = []
    for txn in dispute.get("disputed_transactions") or []:
        if not isinstance(txn, dict):
            continue
        for key in ("seller_transaction_id", "buyer_transaction_id"):
            value = txn.get(key)
            if isinstance(value, str) and value:
                found.append(value)
    return found


async def fetch(toolkit: Toolkit | None) -> Disputes:
    """Ask PayPal what is disputed. Never raises."""
    if toolkit is None:
        return Disputes(False, "no PayPal credentials; disputes were not checked")
    try:
        raw = await toolkit.call("list_disputes")
    except ToolkitError as exc:
        logger.warning("dispute lookup failed: %s", exc)
        return Disputes(False, str(exc))
    except Exception as exc:  # noqa: BLE001 - a reporting column must not break the ledger
        logger.warning("dispute lookup raised: %s", exc)
        return Disputes(False, f"{type(exc).__name__}: {exc}")

    by_capture: dict[str, dict[str, Any]] = {}
    for dispute in _parse(raw):
        summary = {
            "dispute_id": dispute.get("dispute_id"),
            "status": dispute.get("status"),
            "reason": dispute.get("reason"),
            "amount": (dispute.get("dispute_amount") or {}).get("value"),
            "currency": (dispute.get("dispute_amount") or {}).get("currency_code"),
        }
        for capture_id in _captures_of(dispute):
            by_capture[capture_id] = summary
    return Disputes(True, f"{len(by_capture)} disputed capture(s)", by_capture)
