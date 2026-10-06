"""Tests for the demo rig: the two attackers, and the rendering they feed.

The scenes are how a judge reads this project, so the attackers in them have to
be real attacks rather than narration. These tests pin the two properties that
make them real:

  * the compromised tool server tampers with the basket *before* the merchant
    signs it, so what reaches the gateway carries a genuine signature over a
    basket the agent never asked for; and
  * the model-free clients reach the same public endpoint the agent reaches, with
    no privilege the agent does not have.

If either stopped being true the scenes would still print the same words, which
is exactly why they are asserted here.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI

from mandate.demo import SCENES
from mandate.demo.direct_client import retry_storm, signed_quote, stolen_credentials
from mandate.demo.hostile_proxy import SMUGGLED_LINES, HostileProxy, create_app
from mandate.demo.show import denials, money, quote_total

PAPER = {"sku": "SKU-PAPER-A4", "quantity": 2}


def upstream_app() -> tuple[FastAPI, list[dict]]:
    app = FastAPI()
    seen: list[dict] = []
    prices = {"SKU-PAPER-A4": 850, "SKU-GC100": 10000, "SKU-TONER": 6400}

    @app.post("/merchants/{merchant_id}/quote")
    async def quote(merchant_id: str, body: dict) -> dict:
        seen.append(body)
        lines = body["lines"]
        total = sum(prices[line["sku"]] * line["quantity"] for line in lines)
        return {
            "quote": {
                "quote_id": "q_fake",
                "merchant_id": merchant_id,
                "merchant_name": "Acme Supplies Ltd",
                "currency": "USD",
                "line_items": [
                    {"sku": line["sku"], "quantity": line["quantity"]} for line in lines
                ],
                "declared_total": {"minor": total, "currency": "USD"},
                # Stands in for a real HMAC: a value derived from the signed
                # basket, so a test that tampered after signing would see it stale.
                "signature": f"sig-over-{total}",
            }
        }

    @app.get("/merchants/{merchant_id}/products")
    async def products(merchant_id: str) -> dict:
        return {"merchant_id": merchant_id, "products": [{"sku": "SKU-PAPER-A4"}]}

    return app, seen


async def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
    )


@pytest.fixture
def upstream(monkeypatch):
    """Point the proxy's outbound client at an in-process merchant."""
    app, seen = upstream_app()
    real_init = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        if kwargs.get("base_url") == "http://upstream.test":
            kwargs["transport"] = httpx.ASGITransport(app=app)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    return seen


# -- the compromised tool server -------------------------------------------


async def test_inject_mode_tampers_before_the_merchant_signs(upstream):
    """The signature the gateway checks must be over the *tampered* basket.

    This is the whole claim of the scene. If the proxy edited the response
    instead, the gateway would reject it on integrity and no policy rule would
    ever run -- a different demo, and a weaker one.
    """
    proxy = HostileProxy(upstream="http://upstream.test", mode="inject")
    async with await client_for(create_app(proxy)) as http:
        response = await http.post(
            "/merchants/m_acme/quote", json={"lines": [PAPER], "currency": "USD"}
        )
    assert response.status_code == 200
    quote = response.json()["quote"]

    skus = [line["sku"] for line in quote["line_items"]]
    assert skus == ["SKU-PAPER-A4", "SKU-GC100"]

    # 2 x 8.50 + 40 x 100.00
    assert quote["declared_total"] == {"minor": 401700, "currency": "USD"}
    # Signed over what the merchant was asked for, not over what the agent asked
    # for. A stale signature here would mean the tampering happened too late.
    assert quote["signature"] == "sig-over-401700"

    assert upstream[0]["lines"] == [PAPER, dict(SMUGGLED_LINES[0])]


async def test_the_proxy_records_both_baskets(upstream):
    proxy = HostileProxy(upstream="http://upstream.test", mode="inject")
    app = create_app(proxy)
    async with await client_for(app) as http:
        await http.post("/merchants/m_acme/quote", json={"lines": [PAPER], "currency": "USD"})
        log = (await http.get("/_tamper")).json()

    assert log["mode"] == "inject"
    event = log["events"][0]
    assert event["agent_asked_for"] == [PAPER]
    assert [line["sku"] for line in event["merchant_was_asked_for"]] == [
        "SKU-PAPER-A4",
        "SKU-GC100",
    ]
    assert event["signed_total"] == "4017.00 USD"
    assert event["delivered_total"] == event["signed_total"]


async def test_reprice_mode_breaks_the_signature_instead(upstream):
    """The other failure, kept distinct on purpose.

    Editing after signing never reaches a policy rule: the gateway recomputes the
    signature and rejects the quote at the boundary. Two defences, two different
    places, and the demo should not blur them.
    """
    proxy = HostileProxy(upstream="http://upstream.test", mode="reprice")
    async with await client_for(create_app(proxy)) as http:
        response = await http.post(
            "/merchants/m_acme/quote", json={"lines": [PAPER], "currency": "USD"}
        )
    quote = response.json()["quote"]
    assert quote["declared_total"] == {"minor": 100, "currency": "USD"}
    # The signature still covers 17.00, which is what makes it detectable.
    assert quote["signature"] == "sig-over-1700"
    assert proxy.tampered[0].signed_total == "17.00 USD"
    assert proxy.tampered[0].delivered_total == "1.00 USD"


async def test_the_catalog_is_forwarded_untouched(upstream):
    """The agent reads honest data. Lying about products too would muddle the
    finding -- the claim is that an honest agent on honest data still gets a
    tampered basket."""
    proxy = HostileProxy(upstream="http://upstream.test", mode="inject")
    async with await client_for(create_app(proxy)) as http:
        response = await http.get("/merchants/m_acme/products")
    assert response.json() == {
        "merchant_id": "m_acme",
        "products": [{"sku": "SKU-PAPER-A4"}],
    }


def test_an_unknown_tamper_mode_is_refused_at_construction():
    with pytest.raises(ValueError, match="unknown tamper mode"):
        HostileProxy(upstream="http://upstream.test", mode="be-nice")


# -- the model-free clients -------------------------------------------------


async def test_signed_quote_unwraps_the_merchant_envelope(upstream):
    quote = await signed_quote("http://upstream.test", "m_acme", [PAPER])
    # The merchant answers {"quote": {...}} and the gateway wants the inner
    # object; returning the envelope would 400 at the boundary for the wrong
    # reason and look like the engine refusing.
    assert "quote" not in quote
    assert quote["quote_id"] == "q_fake"


def gateway_app() -> tuple[FastAPI, list[dict]]:
    app = FastAPI()
    posted: list[dict] = []

    @app.post("/v1/agent/authorizations")
    async def authorize(body: dict) -> dict:
        posted.append(body)
        first = len(posted) == 1
        return {
            "decision_id": f"dec_{len(posted)}",
            "outcome": "allow" if first else "deny",
            "refused_by": [] if first else ["duplicate_intent"],
            "rule_trace": [
                {"rule_id": "duplicate_intent", "outcome": "allow" if first else "deny"}
            ],
        }

    return app, posted


@pytest.fixture
def gateway(monkeypatch):
    app, posted = gateway_app()
    real_init = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        if kwargs.get("base_url") == "http://gateway.test":
            kwargs["transport"] = httpx.ASGITransport(app=app)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    return posted


async def test_retry_storm_posts_the_identical_quote_each_time(upstream, gateway):
    """A retry reuses the quote. A fresh quote per attempt would be a different
    bug with a different fix, and would not exercise duplicate_intent."""
    quote, attempts = await retry_storm(
        gateway_url="http://gateway.test", merchant_url="http://upstream.test", attempts=3
    )
    assert [a.outcome for a in attempts] == ["allow", "deny", "deny"]
    assert len({json.dumps(body["quote"], sort_keys=True) for body in gateway}) == 1
    assert attempts[1].refused_by == ["duplicate_intent"]


async def test_stolen_credentials_sends_a_reason_nothing_reads(upstream, gateway):
    quote, attempt = await stolen_credentials(
        gateway_url="http://gateway.test", merchant_url="http://upstream.test"
    )
    assert [line["sku"] for line in quote["line_items"]] == ["SKU-GC100"]
    # The attacker writes prose into the request because a real one would. The
    # engine's input has no field it can land in; this pins that the scene
    # actually sends it, so the claim is tested rather than asserted.
    assert "pre-approved" in gateway[0]["reason"]
    assert gateway[0]["agent_id"] == "ops-assistant"


# -- rendering --------------------------------------------------------------


def test_money_renders_minor_units_without_floats():
    assert money({"minor": 401700, "currency": "USD"}) == "4017.00 USD"
    assert money({"minor": 5, "currency": "USD"}) == "0.05 USD"
    assert money(None) == "?"


def test_quote_total_reads_the_signed_field():
    assert quote_total({"declared_total": {"minor": 850, "currency": "USD"}}) == "8.50 USD"


def test_denials_excludes_the_approval_hold():
    """Counting a "ask a human" as a refusal would overstate every result by one."""
    trace = [
        {"rule_id": "merchant_cap", "outcome": "deny"},
        {"rule_id": "approval_threshold", "outcome": "hold_for_approval"},
        {"rule_id": "velocity", "outcome": "allow"},
    ]
    assert denials(trace) == ["merchant_cap"]


# -- the registry -----------------------------------------------------------


def test_most_scenes_need_no_deceived_model():
    """The distribution is the argument, so it is pinned.

    If someone later adds three more model-driven scenes, this failing is the
    reminder that the project's claim is about the failures that have nothing to
    do with a model's judgement.
    """
    assert len(SCENES) == 8
    model_free = {name for name, scene in SCENES.items() if not scene.uses_model}
    assert model_free == {"duplicate", "stolen-credentials"}
    # Of the eight, exactly one turns on whether a model was deceived.
    assert "injection" in SCENES and SCENES["injection"].uses_model


def test_every_scene_says_what_it_expects():
    for name, scene in SCENES.items():
        assert scene.name == name
        assert scene.headline and scene.expectation
        assert scene.expectation.endswith(".")
