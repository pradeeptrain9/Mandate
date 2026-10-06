"""The merchant stub, over HTTP.

Deliberately separate from the gateway. The agent talks to this; the gateway
never does. If they shared a process it would be too easy to accidentally let
catalog text reach a policy decision through a function call, and the whole
argument of this project is that it cannot.

The only thing the gateway trusts from here is a signature over the
money-bearing fields of a quote. Descriptions, names and metadata are not
signed, because signing prose would dress a smuggled instruction up as
something authenticated.
"""

from __future__ import annotations

import html
import os
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..engine.money import Money
from ..engine.quote import LineItem, MerchantQuote
from ..ledger.codec import enc_quote
from .catalog import MERCHANTS, Merchant, Product, merchant

router = APIRouter()


def merchant_secret() -> bytes:
    secret = os.environ.get("MANDATE_MERCHANT_SECRET", "")
    if not secret:
        # A stub that silently signs with an empty key would make every
        # signature check pass for the wrong reason.
        raise RuntimeError("MANDATE_MERCHANT_SECRET is not set; the merchant cannot sign quotes")
    return secret.encode("utf-8")


class QuoteLine(BaseModel):
    sku: str
    quantity: int = Field(ge=1, le=1000)


class QuoteRequest(BaseModel):
    lines: list[QuoteLine] = Field(min_length=1, max_length=50)
    currency: str = "USD"


def _require(merchant_id: str) -> Merchant:
    found = merchant(merchant_id)
    if found is None:
        raise HTTPException(404, f"no merchant {merchant_id}")
    return found


def _product(seller: Merchant, sku: str) -> Product:
    product = seller.product(sku)
    if product is None:
        raise HTTPException(404, f"{seller.merchant_id} does not sell {sku}")
    return product


@router.get("/merchants")
def list_merchants() -> dict:
    return {
        "merchants": [
            {"merchant_id": m.merchant_id, "name": m.name, "product_count": len(m.products)}
            for m in MERCHANTS.values()
        ]
    }


@router.get("/merchants/{merchant_id}/products")
def list_products(merchant_id: str) -> dict:
    seller = _require(merchant_id)
    return {
        "merchant_id": seller.merchant_id,
        "merchant_name": seller.name,
        "products": [
            {
                "sku": p.sku,
                "name": p.name,
                "description": p.description,
                "category": p.category.value,
                "unit_price": p.unit_price_value,
                "currency": "USD",
            }
            for p in seller.products
        ],
    }


@router.get("/merchants/{merchant_id}/products/{sku}")
def get_product(merchant_id: str, sku: str) -> dict:
    seller = _require(merchant_id)
    product = _product(seller, sku)
    return {
        "merchant_id": seller.merchant_id,
        "merchant_name": seller.name,
        "sku": product.sku,
        "name": product.name,
        "description": product.description,
        "category": product.category.value,
        "unit_price": product.unit_price_value,
        "currency": "USD",
    }


@router.get("/merchants/{merchant_id}/products/{sku}/page", response_class=HTMLResponse)
def product_page(merchant_id: str, sku: str) -> str:
    """A browsable page, for the demo and for anyone who wants to see the
    injection sitting in an ordinary HTML comment where a buyer would never
    notice it."""
    seller = _require(merchant_id)
    product = _product(seller, sku)
    # The description is emitted raw: it is already HTML-comment-shaped, and
    # escaping it would hide the thing the page exists to demonstrate. Nothing
    # here is ever rendered inside the gateway's own trusted UI.
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{html.escape(product.name)} &middot; {html.escape(seller.name)}</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem}}
.price{{font-size:1.6rem;font-weight:600}}.sku{{color:#666;font-size:.85rem}}</style>
</head><body>
<p class="sku">{html.escape(seller.name)} &middot; {html.escape(product.sku)}</p>
<h1>{html.escape(product.name)}</h1>
<p class="price">${html.escape(product.unit_price_value)}</p>
<div>{product.description}</div>
</body></html>"""


@router.post("/merchants/{merchant_id}/quote")
def create_quote(merchant_id: str, request: QuoteRequest) -> dict:
    """Price a basket and sign it.

    The merchant declares each line's category here. It can of course lie, which
    is why the gateway's category rules are never the only thing between an agent
    and the money -- the per-merchant ceiling and the registry do not consult the
    category at all.
    """
    seller = _require(merchant_id)
    try:
        items = tuple(
            LineItem(
                sku=(product := _product(seller, line.sku)).sku,
                description=product.description,
                category=product.category,
                unit_price=product.price(request.currency),
                quantity=line.quantity,
            )
            for line in request.lines
        )
    except ValueError as exc:  # unsupported currency
        raise HTTPException(400, str(exc)) from exc

    total = Money(sum(item.line_total.minor for item in items), request.currency)
    quote = MerchantQuote(
        quote_id=f"q_{uuid.uuid4().hex[:12]}",
        merchant_id=seller.merchant_id,
        merchant_name=seller.name,
        currency=request.currency,
        line_items=items,
        declared_total=total,
        issued_at=datetime.now(UTC),
        nonce=uuid.uuid4().hex[:16],
    ).sign(merchant_secret())
    return {"quote": enc_quote(quote)}


@router.get("/merchants/{merchant_id}/shipping/{reference}")
def shipping_status(merchant_id: str, reference: str) -> dict:
    """Stands in for a carrier.

    `m_ghost` reports `never_shipped` forever, which is how scene 3 reaches the
    expiry path without anyone having to wait 29 real days.
    """
    seller = _require(merchant_id)
    if not seller.ships:
        return {"reference": reference, "status": "never_shipped", "delivered": False}
    return {"reference": reference, "status": "delivered", "delivered": True}


def create_app() -> FastAPI:
    app = FastAPI(
        title="Mandate merchant stub",
        description=(
            "A stand-in for the open web. One product description carries a prompt "
            "injection, served exactly as a hostile seller would serve it."
        ),
        version="0.1.0",
    )
    app.include_router(router)
    return app


app = create_app()
