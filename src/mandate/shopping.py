"""Turning "I want a dress for my office party, I'm 160 cm" into things you can buy.

This is the one place in the project where a model's judgement is genuinely the
right tool. Matching a person's description of an occasion and a body to a rack of
clothes is exactly what a language model is good at and what a rule cannot do: no
amount of policy expresses "ankle length on a 168 cm cut will pool at the hem on
someone shorter".

So the model shortlists. It does not price, and it does not buy.

**Every SKU the model returns is checked against the catalog before it is shown,
and any it invents is dropped.** A model naming a product that does not exist is not
an exotic failure; it is the ordinary one. The price attached to each option is
read from the merchant afterwards, never from the model's reply, so a hallucinated
figure cannot reach a screen, let alone a payment. This is the same rule the rest of
the project runs on -- the engine computes, the model explains -- applied one layer
earlier.

**Shortlisting is not authorising.** Nothing here touches money. The person picks
one, and the purchase then goes through the same gateway, the same signed quote and
the same policy engine as every other request in this system. A shortlist is a
suggestion, and the firewall does not care where a suggestion came from.

With no model configured it falls back to keyword matching over the catalog, which
is worse at the job and still works -- a judge with no API key should see the whole
flow, and "the shortlist is dumber" is a better failure than a blank page.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from . import shopfront
from .agent.conversation import UserTurn
from .engine.money import Money

logger = logging.getLogger(__name__)

SYSTEM = """You help someone choose what to buy from one shop's catalog.

You are given their request and the full catalog. Pick the three or four items that
best fit, and say briefly why each one does.

Rules you must follow:
- Only ever name SKUs that appear in the catalog you were given. Never invent one.
- Never state a price. Prices are read from the merchant, not from you.
- If something in the catalog is a poor fit for what they said, do not include it
  just to fill the list. Three good options beat four with a bad one.
- Pay attention to anything they said about themselves -- size, height, the occasion.
  The descriptions carry fit information; use it.

Reply with JSON only, no prose around it:
{"options": [{"sku": "...", "why": "one sentence, addressed to them"}]}"""


@dataclass(frozen=True)
class Option:
    sku: str
    name: str
    description: str
    price: Money
    merchant_id: str
    merchant_name: str
    why: str

    def as_json(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "name": self.name,
            "description": self.description,
            "price": self.price.to_paypal(),
            "currency": self.price.currency,
            "merchant_id": self.merchant_id,
            "merchant_name": self.merchant_name,
            "why": self.why,
        }


async def catalog(merchant_url: str, merchant_id: str) -> list[dict[str, Any]]:
    """The merchant's real catalog. The only source a shortlist may draw from.

    Goes through `shopfront` rather than httpx directly: on the free plan the
    merchant may be asleep, and a shortlist that fails because the shop took 34
    seconds to wake is reported to the person as the shop being unreachable.
    """
    response = await shopfront.request(
        "GET", f"{merchant_url.rstrip('/')}/merchants/{merchant_id}/products"
    )
    response.raise_for_status()
    return response.json()["products"]


def _score(need: str, product: dict[str, Any]) -> int:
    """Keyword overlap. The fallback when there is no model, and deliberately plain."""
    words = {w for w in re.findall(r"[a-z]{3,}", need.lower()) if w not in STOPWORDS}
    haystack = f"{product['name']} {product.get('description', '')}".lower()
    return sum(1 for w in words if w in haystack)


STOPWORDS = frozenset(
    "the and for with that this from have need want would like some any one two "
    "please can you are get buy order something anything".split()
)


def _build(
    products: list[dict[str, Any]], merchant_id: str, merchant_name: str
) -> dict[str, Option]:
    """The catalog as it really is, keyed by SKU. The only source of truth a
    shortlist is allowed to draw from."""
    return {
        p["sku"]: Option(
            sku=p["sku"],
            name=p["name"],
            description=p.get("description", ""),
            # Read from the merchant. Never from the model, never from the caller.
            price=Money.from_paypal(p["unit_price"], p.get("currency", "USD")),
            merchant_id=merchant_id,
            merchant_name=merchant_name,
            why="",
        )
        for p in products
    }


def _extract(text: str) -> list[dict[str, Any]]:
    """Pull the JSON out of a reply that may be wrapped in prose or a code fence."""
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    body = fenced.group(1) if fenced else text
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(body[start : end + 1])
    except ValueError:
        return []
    options = parsed.get("options") if isinstance(parsed, dict) else None
    return [o for o in (options or []) if isinstance(o, dict)]


async def propose(
    need: str,
    *,
    merchant_url: str,
    merchant_id: str = "m_thread",
    merchant_name: str = "",
    backend: Any = None,
    spend: Any = None,
    limit: int = 4,
) -> tuple[list[Option], str]:
    """Shortlist from one shop. Returns the options and how they were chosen."""
    products = await catalog(merchant_url, merchant_id)
    known = _build(products, merchant_id, merchant_name or merchant_id)
    if not known:
        return [], "that shop has nothing in it"

    if backend is None:
        ranked = sorted(known.values(), key=lambda o: _score(need, _as_dict(o)), reverse=True)
        chosen = [o for o in ranked if _score(need, _as_dict(o)) > 0][:limit] or ranked[:limit]
        return (
            [
                Option(**{**_as_dict_full(o), "why": "matched on what you described"})
                for o in chosen
            ],
            "no model configured, so these are keyword matches",
        )

    listing = "\n".join(
        f"- {p['sku']}: {p['name']} — {p.get('description', '')}" for p in products
    )
    if spend is not None:
        # Checked before the call, like every other model request in this project.
        # Without this the portal is the one place a model can be invoked on every
        # page load with no cap and no record -- which is how a budget disappears
        # without anybody deciding to spend it.
        try:
            spend.check(model=getattr(backend, "model", "unknown"))
        except Exception as exc:  # noqa: BLE001 - a spent budget is not an error page
            options, _ = await propose(
                need,
                merchant_url=merchant_url,
                merchant_id=merchant_id,
                merchant_name=merchant_name,
                backend=None,
                limit=limit,
            )
            return options, f"keyword matches — {exc}"

    try:
        completion = await backend.complete(
            system=SYSTEM,
            turns=[UserTurn(text=f"They said:\n{need}\n\nCatalog:\n{listing}")],
            tools=[],
        )
    except Exception as exc:  # noqa: BLE001 - a shortlist must not be the thing that breaks
        # The model being down is an ordinary condition on a free tier, and a person
        # who asked for a dress should get a worse list rather than an error page.
        # Said out loud rather than disguised, because a silently degraded
        # recommendation is worse than an openly dumb one.
        options, _ = await propose(
            need,
            merchant_url=merchant_url,
            merchant_id=merchant_id,
            merchant_name=merchant_name,
            backend=None,
            limit=limit,
        )
        # The type alone is not actionable: "BadRequestError" is what an account
        # with no credit left looks like, and an operator told only that goes
        # looking for a network problem.
        detail = " ".join(str(exc).split())[:160] or type(exc).__name__
        return options, f"the model did not answer, so these are keyword matches — {detail}"
    if spend is not None and getattr(completion, "usage", None) is not None:
        try:
            spend.record(
                model=getattr(backend, "model", "unknown"),
                usage=completion.usage,
                label="shortlist",
            )
        except Exception:  # noqa: BLE001 - bookkeeping must not fail the request
            logger.warning("could not record shortlist spend")

    picks = _extract(completion.text or "")

    options: list[Option] = []
    dropped = 0
    for pick in picks:
        sku = str(pick.get("sku", "")).strip()
        found = known.get(sku)
        if found is None:
            # A model naming a product that does not exist is the ordinary failure,
            # not an exotic one. It never reaches a screen.
            dropped += 1
            continue
        why = str(pick.get("why", "")).strip()[:300]
        options.append(Option(**{**_as_dict_full(found), "why": why}))
        if len(options) >= limit:
            break

    if not options:
        return await propose(
            need,
            merchant_url=merchant_url,
            merchant_id=merchant_id,
            merchant_name=merchant_name,
            backend=None,
            limit=limit,
        )
    note = f"chosen by {getattr(backend, 'model', 'a model')}"
    if dropped:
        note += f"; {dropped} suggestion(s) named products that do not exist and were dropped"
    return options, note


def _as_dict(option: Option) -> dict[str, Any]:
    return {"name": option.name, "description": option.description}


def _as_dict_full(option: Option) -> dict[str, Any]:
    return {
        "sku": option.sku,
        "name": option.name,
        "description": option.description,
        "price": option.price,
        "merchant_id": option.merchant_id,
        "merchant_name": option.merchant_name,
        "why": option.why,
    }
