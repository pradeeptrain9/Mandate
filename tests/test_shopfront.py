"""The merchant on a free plan: asleep, waking, or genuinely down.

All three look the same from the gateway for the first few seconds, and the
distinction is the whole value of this module. The deployed merchant was
measured at 33.8s cold against 0.3s warm, which is longer than the 20s client
timeout the shortlist used to carry -- so the first purchase attempt after any
quiet period failed with "could not reach the shop" on a healthy deployment.

The tests that matter are the ones that assert what is *not* retried. A helper
that retries everything would mask a merchant bug and triple the load doing it.
"""

from __future__ import annotations

import httpx
import pytest

from mandate import shopfront

FAST = (0.0, 0.0)


def _transport(*responses: int | Exception) -> tuple[httpx.MockTransport, list[int]]:
    """Answer with each of `responses` in turn. Ints are statuses; exceptions raise."""
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        index = len(calls)
        calls.append(index)
        answer = responses[min(index, len(responses) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={"products": [], "attempt": index})

    return httpx.MockTransport(handle), calls


# -- what gets retried ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cold_start_502_then_a_real_answer_succeeds():
    transport, calls = _transport(502, 200)

    response = await shopfront.request(
        "GET", "http://shop.test/merchants/m/products", transport=transport, backoff=FAST
    )

    assert response.status_code == 200
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_connect_error_while_booting_is_retried():
    transport, calls = _transport(httpx.ConnectError("connection refused"), 200)

    response = await shopfront.request(
        "GET", "http://shop.test/x", transport=transport, backoff=FAST
    )

    assert response.status_code == 200
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_read_timeout_is_retried():
    transport, calls = _transport(httpx.ReadTimeout("too slow"), 503, 200)

    response = await shopfront.request(
        "GET", "http://shop.test/x", transport=transport, backoff=FAST
    )

    assert response.status_code == 200
    assert len(calls) == 3


# -- what does not ----------------------------------------------------------


@pytest.mark.parametrize("status", [400, 404, 409, 422, 500])
@pytest.mark.asyncio
async def test_an_answer_from_the_merchant_is_returned_not_retried(status):
    """A 404 is the merchant answering. Retrying an answer hides a bug.

    500 is included deliberately: it is the merchant's own code failing, not the
    edge reporting an instance that has not booted. Those are 502/503/504.
    """
    transport, calls = _transport(status)

    response = await shopfront.request(
        "GET", "http://shop.test/x", transport=transport, backoff=FAST
    )

    assert response.status_code == status
    assert len(calls) == 1, "a merchant answer must not be retried"


@pytest.mark.asyncio
async def test_giving_up_says_what_to_check():
    transport, calls = _transport(502)

    with pytest.raises(shopfront.ShopUnreachable) as caught:
        await shopfront.request(
            "GET", "http://shop.test/x", transport=transport, backoff=FAST
        )

    message = str(caught.value)
    assert len(calls) == 3, "every attempt should be spent before giving up"
    # The failure a person reads has to distinguish "asleep" from "down", because
    # the two need opposite responses and the first is the common one.
    assert "wake" in message
    assert "down" in message
    assert "502" in message


@pytest.mark.asyncio
async def test_the_number_of_attempts_follows_the_backoff_schedule():
    transport, calls = _transport(503)

    with pytest.raises(shopfront.ShopUnreachable):
        await shopfront.request("GET", "http://shop.test/x", transport=transport, backoff=())

    assert len(calls) == 1


# -- the timeout is longer than the cold start we measured ------------------


def test_the_timeout_covers_a_measured_cold_start():
    """33.8s was measured against the deployed merchant. A 20s timeout -- which
    is what the shortlist used to carry -- cannot cover it, and that is the whole
    bug this module exists for."""
    assert shopfront.WAKE_TIMEOUT > 34.0


def test_only_edge_statuses_count_as_waking():
    assert shopfront.WAKING == {502, 503, 504}
    assert 500 not in shopfront.WAKING


# -- the call sites actually use it -----------------------------------------
#
# The unit tests above prove the helper behaves. These prove the two paths a
# person can reach go through it, which is the part that was broken: the code was
# correct, it just called httpx directly with a 20-second timeout.


@pytest.mark.asyncio
async def test_the_shortlists_catalog_fetch_goes_through_shopfront(monkeypatch):
    from mandate import shopping

    seen: dict[str, object] = {}

    async def recorder(method, url, **kw):
        seen["method"], seen["url"] = method, url
        return httpx.Response(
            200,
            json={"products": [{"sku": "SKU-A"}]},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(shopping.shopfront, "request", recorder)

    assert await shopping.catalog("http://shop.test", "m_thread") == [{"sku": "SKU-A"}]
    assert seen["method"] == "GET"
    assert seen["url"] == "http://shop.test/merchants/m_thread/products"


def test_the_buy_endpoint_reports_a_sleeping_shop_actionably(monkeypatch, tmp_path):
    """The message a person sees when the merchant never wakes.

    It used to be `could not reach the shop: Server error '502 Bad Gateway'`,
    which reads as the shop being broken when the usual cause is that it is
    asleep. The text is the fix, so the text is the assertion.
    """
    from fastapi.testclient import TestClient

    from mandate.gateway import api
    from mandate.gateway.accounts import Accounts, Role
    from mandate.gateway.api import create_app
    from mandate.gateway.service import Gateway
    from mandate.gateway.store import Store
    from mandate.ledger.records import Ledger
    from mandate.policies import demo_policy

    from helpers import LEDGER_KEY, MERCHANT_SECRET

    async def never_wakes(method, url, **kw):
        raise shopfront.ShopUnreachable(
            "the shop did not answer after 3 attempts over up to 75s each "
            "(HTTP 502 from the edge). On Render's free plan an idle instance "
            "takes about 35s to wake; if this persists the merchant service is down."
        )

    monkeypatch.setattr(api.shopfront, "request", never_wakes)

    store = Store(tmp_path / "state.db")
    try:
        gw = Gateway(
            store=store,
            ledger=Ledger(tmp_path / "decisions.jsonl", LEDGER_KEY),
            policy=demo_policy(),
            merchant_secret=MERCHANT_SECRET,
        )
        with TestClient(create_app(gw)) as client:
            Accounts(store).create("p@example.com", "a-long-enough-password", role=Role.REQUESTER)
            client.post(
                "/app/login",
                json={"username": "p@example.com", "password": "a-long-enough-password"},
            )
            response = client.post(
                "/app/buy",
                json={"merchant_id": "m_thread", "sku": "SKU-DRESS-NAVY-S", "quantity": 1},
            )

        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "wake" in detail
        assert "down" in detail
        assert "could not reach the shop" not in detail
    finally:
        store.close()
