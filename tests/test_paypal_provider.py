"""Provider tests that need no credentials, driven through httpx MockTransport.

The webhook tests matter most. An endpoint that accepts unverified PayPal
deliveries is the worst thing that could be in this repository: a forged
`PAYMENT.CAPTURE.COMPLETED` would convince the gateway a hold had been settled,
and a forged tracking event would make it capture one.
"""

from __future__ import annotations

import json
from datetime import UTC

import httpx
import pytest

from mandate.providers.paypal import PayPalClient, PayPalError, approval_link

TOKEN = {"access_token": "A21AA-test", "expires_in": 32400, "token_type": "Bearer"}


def transport(handler):
    return httpx.MockTransport(handler)


def client(handler) -> PayPalClient:
    return PayPalClient("cid", "secret", transport=transport(handler))


def ok(payload, status=200):
    return httpx.Response(status, json=payload)


@pytest.mark.asyncio
async def test_token_is_fetched_once_and_reused():
    calls = {"token": 0, "order": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            calls["token"] += 1
            return ok(TOKEN)
        calls["order"] += 1
        return ok({"id": "ORDER-1", "status": "CREATED"})

    async with client(handler) as pp:
        await pp.get_order("ORDER-1")
        await pp.get_order("ORDER-1")
    assert calls == {"token": 1, "order": 2}


@pytest.mark.asyncio
async def test_a_401_refreshes_the_token_once_then_retries():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        seen.append(request.headers["Authorization"])
        if len(seen) == 1:
            return ok({"name": "AUTHENTICATION_FAILURE"}, status=401)
        return ok({"id": "ORDER-1"})

    async with client(handler) as pp:
        assert await pp.get_order("ORDER-1") == {"id": "ORDER-1"}
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_state_changing_calls_carry_an_idempotency_key():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        captured.update(request.headers)
        return ok(
            {
                "id": "ORDER-1",
                "status": "CREATED",
                "links": [{"rel": "payer-action", "href": "https://sandbox.paypal.com/approve"}],
            }
        )

    async with client(handler) as pp:
        order = await pp.create_authorization_order(
            currency="USD",
            value="46.00",
            items=[],
            decision_id="dec_abc",
            return_url="https://example.test/ok",
            cancel_url="https://example.test/no",
        )
    assert captured["paypal-request-id"] == "mandate-order-dec_abc"
    assert captured["prefer"] == "return=representation"
    assert approval_link(order) == "https://sandbox.paypal.com/approve"


@pytest.mark.asyncio
async def test_create_order_uses_authorize_intent_and_stamps_the_decision_id():
    body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        body.update(json.loads(request.content))
        return ok({"id": "ORDER-1"})

    async with client(handler) as pp:
        await pp.create_authorization_order(
            currency="USD",
            value="46.00",
            items=[],
            decision_id="dec_abc",
            return_url="https://example.test/ok",
            cancel_url="https://example.test/no",
        )
    assert body["intent"] == "AUTHORIZE"
    unit = body["purchase_units"][0]
    assert unit["custom_id"] == "dec_abc"
    assert unit["amount"] == {
        "currency_code": "USD",
        "value": "46.00",
        "breakdown": {"item_total": {"currency_code": "USD", "value": "46.00"}},
    }


@pytest.mark.asyncio
async def test_authorize_order_reads_the_expiry_paypal_reports():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        return ok(
            {
                "id": "ORDER-1",
                "purchase_units": [
                    {
                        "payments": {
                            "authorizations": [
                                {
                                    "id": "AUTH-9",
                                    "status": "CREATED",
                                    "amount": {"currency_code": "USD", "value": "46.00"},
                                    "create_time": "2026-10-06T12:00:00Z",
                                    "expiration_time": "2026-11-04T12:00:00Z",
                                }
                            ]
                        }
                    }
                ],
            }
        )

    async with client(handler) as pp:
        auth = await pp.authorize_order("ORDER-1")
    assert auth.authorization_id == "AUTH-9"
    assert auth.order_id == "ORDER-1"
    # 29 days, from PayPal's own figure rather than one we computed.
    assert (auth.expires_at - auth.created_at).days == 29


@pytest.mark.asyncio
async def test_errors_surface_paypal_field_level_issues():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        return ok(
            {
                "name": "UNPROCESSABLE_ENTITY",
                "details": [
                    {"issue": "AUTHORIZATION_ALREADY_CAPTURED", "field": "/authorization_id"}
                ],
            },
            status=422,
        )

    async with client(handler) as pp:
        with pytest.raises(PayPalError) as excinfo:
            await pp.capture_authorization("AUTH-9", currency="USD", value="46.00")
    assert excinfo.value.issues == ["AUTHORIZATION_ALREADY_CAPTURED"]
    assert "UNPROCESSABLE_ENTITY" in str(excinfo.value)


# -- webhook verification ---------------------------------------------------

SIG_HEADERS = {
    "paypal-auth-algo": "SHA256withRSA",
    "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
    "paypal-transmission-id": "tx-1",
    "paypal-transmission-sig": "sig-1",
    "paypal-transmission-time": "2026-10-06T12:00:00Z",
}

# Deliberately awkward: key order that json.dumps would not reproduce, a
# non-ASCII character, and a trailing float. Re-serialising this would change
# the bytes PayPal signed.
RAW_EVENT = (
    b'{"id":"WH-1","event_type":"PAYMENT.CAPTURE.COMPLETED",'
    b'"resource":{"amount":{"value":"46.00","currency_code":"USD"},"note":"caf\xc3\xa9"}}'
)


@pytest.mark.asyncio
async def test_webhook_verification_sends_the_exact_bytes_paypal_signed():
    sent: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        sent["body"] = request.content
        return ok({"verification_status": "SUCCESS"})

    async with client(handler) as pp:
        assert await pp.verify_webhook(
            headers=SIG_HEADERS, raw_body=RAW_EVENT, webhook_id="WH-ID"
        )

    # The original event appears verbatim inside the verification request.
    assert RAW_EVENT in sent["body"]
    envelope = json.loads(sent["body"])
    assert envelope["webhook_id"] == "WH-ID"
    assert envelope["transmission_sig"] == "sig-1"
    assert envelope["webhook_event"]["resource"]["note"] == "café"


@pytest.mark.asyncio
async def test_a_failure_verdict_is_not_treated_as_verified():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        return ok({"verification_status": "FAILURE"})

    async with client(handler) as pp:
        assert not await pp.verify_webhook(
            headers=SIG_HEADERS, raw_body=RAW_EVENT, webhook_id="WH-ID"
        )


@pytest.mark.asyncio
async def test_a_delivery_missing_signature_headers_is_rejected_without_calling_paypal():
    calls = {"verify": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        calls["verify"] += 1
        return ok({"verification_status": "SUCCESS"})

    async with client(handler) as pp:
        for dropped in SIG_HEADERS:
            partial = {k: v for k, v in SIG_HEADERS.items() if k != dropped}
            assert not await pp.verify_webhook(
                headers=partial, raw_body=RAW_EVENT, webhook_id="WH-ID"
            )
    assert calls["verify"] == 0


@pytest.mark.asyncio
async def test_header_casing_does_not_matter():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        return ok({"verification_status": "SUCCESS"})

    shouty = {k.upper(): v for k, v in SIG_HEADERS.items()}
    async with client(handler) as pp:
        assert await pp.verify_webhook(headers=shouty, raw_body=RAW_EVENT, webhook_id="WH-ID")


def test_missing_credentials_fail_fast():
    with pytest.raises(ValueError):
        PayPalClient("", "secret")


# -- idempotency keys identify the attempt, not just the resource ------------
#
# Found by the sandbox spike. Keying a capture on the authorization id alone made
# a $20 capture and a later $26 capture collide: PayPal replayed the first
# response, the second call returned 201, and it looked like a double capture had
# been allowed.


@pytest.mark.asyncio
async def test_two_different_capture_amounts_get_different_idempotency_keys():
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        keys.append(request.headers["paypal-request-id"])
        return ok({"id": "CAPTURE-1", "status": "COMPLETED"})

    async with client(handler) as pp:
        await pp.capture_authorization("AUTH-9", currency="USD", value="20.00")
        await pp.capture_authorization("AUTH-9", currency="USD", value="26.00")
    assert len(set(keys)) == 2, "a different amount must not reuse the earlier key"


@pytest.mark.asyncio
async def test_retrying_the_same_capture_reuses_its_key():
    """The other half: a timeout retry must be idempotent, not a second charge."""
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        keys.append(request.headers["paypal-request-id"])
        return ok({"id": "CAPTURE-1", "status": "COMPLETED"})

    async with client(handler) as pp:
        await pp.capture_authorization("AUTH-9", currency="USD", value="20.00")
        await pp.capture_authorization("AUTH-9", currency="USD", value="20.00")
    assert len(set(keys)) == 1


@pytest.mark.asyncio
async def test_final_capture_flag_is_part_of_the_key():
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        keys.append(request.headers["paypal-request-id"])
        return ok({"id": "CAPTURE-1", "status": "COMPLETED"})

    async with client(handler) as pp:
        await pp.capture_authorization("AUTH-9", currency="USD", value="20.00", final_capture=True)
        await pp.capture_authorization("AUTH-9", currency="USD", value="20.00", final_capture=False)
    assert len(set(keys)) == 2


@pytest.mark.asyncio
async def test_partial_refunds_do_not_collide():
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        keys.append(request.headers["paypal-request-id"])
        return ok({"id": "REFUND-1", "status": "COMPLETED"})

    async with client(handler) as pp:
        await pp.refund_capture("CAPTURE-1", currency="USD", value="5.00")
        await pp.refund_capture("CAPTURE-1", currency="USD", value="15.00")
    assert len(set(keys)) == 2


@pytest.mark.asyncio
async def test_idempotency_keys_fit_paypals_header_limit():
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        keys.append(request.headers["paypal-request-id"])
        return ok({"id": "CAPTURE-1", "status": "COMPLETED"})

    async with client(handler) as pp:
        await pp.capture_authorization("A" * 300, currency="USD", value="20.00")
    assert len(keys[0]) <= 108


# -- transaction search ------------------------------------------------------
#
# The Agent Toolkit's list_transactions is broken against sandbox: it builds
# start_date from datetime.utcnow().isoformat() with no UTC offset and PayPal
# answers 400 INVALID_REQUEST "Invalid date passed". Hence this one call living
# here, where the formatting can be tested.


@pytest.mark.asyncio
async def test_transaction_search_sends_an_offset_qualified_timestamp():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        seen["query"] = str(request.url.query, "ascii")
        return ok({"transaction_details": []})

    from datetime import datetime

    async with client(handler) as pp:
        await pp.search_transactions(
            start=datetime(2026, 9, 5, 8, 49, 4, tzinfo=UTC),
            end=datetime(2026, 10, 6, 8, 49, 4, tzinfo=UTC),
        )
    # The exact shape the toolkit failed to send: an explicit offset on both ends.
    assert "start_date=2026-09-05T08%3A49%3A04-0000" in seen["query"]
    assert "end_date=2026-10-06T08%3A49%3A04-0000" in seen["query"]
    # And no literal "None" smuggled into the query, which the toolkit also did.
    assert "None" not in seen["query"]


@pytest.mark.asyncio
async def test_transaction_search_normalises_a_non_utc_offset():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return ok(TOKEN)
        seen["query"] = str(request.url.query, "ascii")
        return ok({"transaction_details": []})

    from datetime import datetime, timedelta, timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    async with client(handler) as pp:
        await pp.search_transactions(
            start=datetime(2026, 9, 5, 14, 19, 4, tzinfo=ist),
            end=datetime(2026, 9, 6, 14, 19, 4, tzinfo=ist),
        )
    assert "start_date=2026-09-05T08%3A49%3A04-0000" in seen["query"]


def test_a_naive_datetime_is_read_as_utc_rather_than_guessed():
    from datetime import datetime

    from mandate.providers.paypal import _rfc3339

    naive = datetime(2026, 9, 5, 8, 49, 4)  # noqa: DTZ001 - naive is the subject
    aware = datetime(2026, 9, 5, 8, 49, 4, tzinfo=UTC)
    assert _rfc3339(naive) == _rfc3339(aware)
