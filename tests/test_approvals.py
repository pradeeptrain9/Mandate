"""Paging a human: what the message says, what it never says, and what happens
when it does not arrive.

The last one carries most of the weight. An SMS provider is the least reliable
component in this system and the only one whose failure must not change a money
decision, so the tests that matter are the ones where Twilio is broken.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
import pytest

from mandate.engine.policy import Outcome
from mandate.engine.quote import Category
from mandate.gateway.approvals import Approver, build_approver, compose
from mandate.gateway.service import AuthorizationRequest, Gateway, GatewayError
from mandate.gateway.state import HoldState
from mandate.gateway.store import Store
from mandate.ledger.records import Ledger
from mandate.policies import demo_policy
from mandate.providers.twilio import HINTS, Sent, TwilioClient, TwilioError, redact

from fake_paypal import FakePayPal
from helpers import LEDGER_KEY, MERCHANT_SECRET, quote

APPROVER = "+14155550123"
SENDER = "+15005550006"
BODY_WORDS = ("Mandate", "approve", "Expires")


# -- the recording transport ------------------------------------------------


class Twilio:
    """A fake Twilio. Records the form it was posted and answers however a test
    needs it to."""

    def __init__(self, *, status: int = 201, payload: dict | None = None, raises: Exception | None = None):
        self.status = status
        self.payload = payload
        self.raises = raises
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.raises is not None:
                raise self.raises
            body = self.payload or {"sid": "SM123", "status": "queued"}
            return httpx.Response(self.status, json=body)

        return httpx.MockTransport(handle)

    def client(self) -> TwilioClient:
        return TwilioClient("AC_test", "tok_test", SENDER, transport=self.transport())

    @property
    def form(self) -> dict[str, str]:
        assert self.requests, "nothing was sent"
        pairs = httpx.QueryParams(self.requests[-1].content.decode())
        return dict(pairs)


@pytest.fixture
def paypal(now):
    return FakePayPal(now=now)


def build(tmp_path, paypal, approver: Approver | None) -> Gateway:
    return Gateway(
        store=Store(tmp_path / "state.db"),
        ledger=Ledger(tmp_path / "decisions.jsonl", LEDGER_KEY),
        policy=demo_policy(),
        merchant_secret=MERCHANT_SECRET,
        paypal=paypal.client(),
        public_url="https://mandate.test",
        approver=approver,
    )


async def over_threshold(gateway, now):
    """$180 of compute: over the $100 unattended threshold, under every cap."""
    return await gateway.request_authorization(
        AuthorizationRequest(
            quote=quote(
                merchant_id="m_cloudspend",
                items=[("SKU-GPU", "GPU hour", Category.COMPUTE, "180.00", 1)],
            ),
            reason="training run",
            agent_id="ops-1",
        ),
        now=now,
    )


# -- redaction --------------------------------------------------------------


def test_redact_keeps_enough_to_tell_two_numbers_apart():
    assert redact("+14155550123") == "+1******0123"
    assert redact("+14155550124") != redact("+14155550123")


def test_redact_handles_absent_and_short_numbers():
    assert redact("") == "(none)"
    # Nothing is revealed rather than a mostly-intact number, because a short
    # string here means something has already gone wrong.
    assert redact("+1234") == "*****"


# -- the message ------------------------------------------------------------


async def test_body_names_the_amount_and_merchant(tmp_path, paypal, now):
    twilio = Twilio()
    gw = build(tmp_path, paypal, Approver(twilio.client(), APPROVER))
    try:
        result = await over_threshold(gw, now)
        assert result.outcome is Outcome.HOLD_FOR_APPROVAL
        body = twilio.form["Body"]
        # An approver who must open a link to find out what they are approving is
        # being trained to open links.
        assert "180.00" in body
        assert "Acme Supplies Ltd" in body
        assert all(word in body for word in BODY_WORDS)
    finally:
        gw.store.close()


async def test_the_token_is_in_the_path_and_nowhere_else(tmp_path, paypal, now):
    twilio = Twilio()
    gw = build(tmp_path, paypal, Approver(twilio.client(), APPROVER))
    try:
        result = await over_threshold(gw, now)
        token = result.approval_token
        assert token

        body = twilio.form["Body"]
        link = next(word for word in body.split() if word.startswith("https://"))
        parts = urlsplit(link)
        assert parts.path == f"/approve/{token}"
        # Query strings leak through referrers, proxy logs and analytics.
        assert parts.query == ""
        assert parts.fragment == ""
        # And the token appears exactly once in the whole message: in the link.
        assert body.count(token) == 1
    finally:
        gw.store.close()


async def test_the_token_is_not_returned_to_the_agent(tmp_path, paypal, now):
    """The HTTP surface, not the service: `AuthorizationResult` carries the token
    because the gateway needs it to build the link."""
    from fastapi.testclient import TestClient

    from mandate.gateway.api import agent_router
    from fastapi import FastAPI

    twilio = Twilio()
    gw = build(tmp_path, paypal, Approver(twilio.client(), APPROVER))
    app = FastAPI()
    app.include_router(agent_router)
    app.state.gateway = gw
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/agent/authorizations",
                json={
                    "quote": _quote_json(
                        quote(
                            merchant_id="m_cloudspend",
                            items=[("SKU-GPU", "GPU hour", Category.COMPUTE, "180.00", 1)],
                            # This route uses the real clock, and the policy
                            # refuses a quote older than ten minutes.
                            at=datetime.now(timezone.utc),
                        )
                    ),
                    "reason": "training run",
                },
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["outcome"] == Outcome.HOLD_FOR_APPROVAL.value
        assert payload["approver_notified"] is True
        token = twilio.form["Body"].rsplit("/approve/", 1)[1].split()[0]
        assert token not in response.text
    finally:
        gw.store.close()


def _quote_json(q):
    from mandate.ledger.codec import enc_quote

    return enc_quote(q)


# -- a send that fails -----------------------------------------------------


async def test_a_refused_send_does_not_change_the_decision(tmp_path, paypal, now):
    twilio = Twilio(status=400, payload={"code": 21608, "message": "unverified"})
    gw = build(tmp_path, paypal, Approver(twilio.client(), APPROVER))
    try:
        result = await over_threshold(gw, now)
        # The decision stands. Anything else would invite a retry, and a retry
        # would mint a second token for one purchase.
        assert result.outcome is Outcome.HOLD_FOR_APPROVAL
        assert result.state is HoldState.AWAITING_HUMAN
        assert result.approval_token
        assert result.notification is not None and not result.notification.sent

        # And an operator can see why hours later, from the dashboard.
        # Reported on the returned hold, not only in the database: an operator
        # shown a clean record of a send that failed is worse than no record.
        assert "approval SMS" in (result.hold.last_error or "")
        hold = gw.store.get(result.decision_id)
        assert hold.state is HoldState.AWAITING_HUMAN
        assert "approval SMS" in (hold.last_error or "")
        assert "21608" in hold.last_error
        # The hint, not just the number, because 21608 means nothing on its own.
        assert "Verified Caller IDs" in hold.last_error
    finally:
        gw.store.close()


async def test_an_unreachable_twilio_does_not_change_the_decision(tmp_path, paypal, now):
    twilio = Twilio(raises=httpx.ConnectTimeout(""))
    gw = build(tmp_path, paypal, Approver(twilio.client(), APPROVER))
    try:
        result = await over_threshold(gw, now)
        assert result.state is HoldState.AWAITING_HUMAN
        assert not result.notification.sent
        # The same lesson as the Gemini backend: several httpx errors stringify
        # to nothing, so the type has to carry the message.
        assert "ConnectTimeout" in result.notification.detail
    finally:
        gw.store.close()


async def test_no_twilio_at_all_is_a_working_configuration(tmp_path, paypal, now):
    gw = build(tmp_path, paypal, None)
    try:
        result = await over_threshold(gw, now)
        assert result.state is HoldState.AWAITING_HUMAN
        assert result.approval_token
        assert not result.notification.sent
        assert "no Twilio credentials" in result.notification.detail
        # Nothing was attempted, so nothing is recorded as having failed. A
        # deployment without Twilio is a supported one, not a broken one.
        assert not result.notification.attempted
        assert gw.store.get(result.decision_id).last_error is None
    finally:
        gw.store.close()


async def test_credentials_without_an_approver_number_say_so(tmp_path, paypal, now):
    twilio = Twilio()
    gw = build(tmp_path, paypal, Approver(twilio.client(), ""))
    try:
        result = await over_threshold(gw, now)
        assert not result.notification.sent
        assert "MANDATE_APPROVER_NUMBER" in result.notification.detail
        assert twilio.requests == []
    finally:
        gw.store.close()


# -- what the logs are allowed to hold -------------------------------------


async def test_the_body_is_never_logged(tmp_path, paypal, now, caplog):
    twilio = Twilio()
    gw = build(tmp_path, paypal, Approver(twilio.client(), APPROVER))
    try:
        with caplog.at_level(logging.DEBUG):
            result = await over_threshold(gw, now)
        body = twilio.form["Body"]
        logged = caplog.text
        assert result.notification.sent
        assert body not in logged
        assert result.approval_token not in logged
        assert APPROVER not in logged
        # The delivery receipt is worth having; the contents are not.
        assert "SM123" in logged
    finally:
        gw.store.close()


async def test_a_failure_is_logged_without_the_number_or_the_body(tmp_path, paypal, now, caplog):
    twilio = Twilio(status=400, payload={"code": 21610, "message": "unsubscribed"})
    gw = build(tmp_path, paypal, Approver(twilio.client(), APPROVER))
    try:
        with caplog.at_level(logging.DEBUG):
            result = await over_threshold(gw, now)
        assert APPROVER not in caplog.text
        assert twilio.form["Body"] not in caplog.text
        assert result.decision_id in caplog.text
    finally:
        gw.store.close()


def test_the_error_redacts_the_destination():
    exc = TwilioError(0, None, f"ConnectTimeout sending to {redact(APPROVER)}: no detail")
    assert APPROVER not in str(exc)
    assert "+1******0123" in str(exc)


# -- the client in isolation ----------------------------------------------


async def test_send_posts_the_form_twilio_expects():
    twilio = Twilio()
    client = twilio.client()
    try:
        sent = await client.send(to=APPROVER, body="hello")
    finally:
        await client.aclose()
    assert isinstance(sent, Sent)
    assert sent.sid == "SM123"
    assert sent.status == "queued"
    # The real number is not kept, even on the success path.
    assert sent.to == redact(APPROVER)
    assert twilio.form == {"To": APPROVER, "From": SENDER, "Body": "hello"}
    request = twilio.requests[-1]
    assert request.url.path == "/2010-04-01/Accounts/AC_test/Messages.json"
    assert request.headers["authorization"].startswith("Basic ")


async def test_an_unknown_error_code_still_reports_the_code():
    twilio = Twilio(status=400, payload={"code": 99999, "message": "something new"})
    client = twilio.client()
    try:
        with pytest.raises(TwilioError) as caught:
            await client.send(to=APPROVER, body="hello")
    finally:
        await client.aclose()
    assert caught.value.code == 99999
    assert "something new" in str(caught.value)
    assert caught.value.hint == ""


async def test_a_non_json_error_body_is_not_swallowed():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    client = TwilioClient("AC_test", "tok", SENDER, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(TwilioError) as caught:
            await client.send(to=APPROVER, body="hello")
    finally:
        await client.aclose()
    assert caught.value.status == 502
    assert caught.value.code is None


def test_the_client_refuses_to_exist_half_configured():
    for args in (("", "tok", SENDER), ("AC", "", SENDER), ("AC", "tok", "")):
        with pytest.raises(ValueError):
            TwilioClient(*args)


def test_every_hinted_code_tells_an_operator_what_to_do():
    for code, hint in HINTS.items():
        # A code on its own sends an operator to a search engine, so every hint
        # names something to change: an environment variable, a console page, or
        # an account action. Restating the code would be no help.
        assert len(hint) > 20, code
        assert str(code) not in hint, code
        assert any(
            word in hint
            for word in ("TWILIO_", "MANDATE_", "console", "Upgrade", "upgrade", "Shorten")
        ), code


# -- configuration --------------------------------------------------------


def test_the_trial_content_restriction_is_explained_not_echoed():
    """572006 is not in Twilio's published error dictionary, and the message it
    carries ("invalid template name") describes a mistake nobody made: the body was
    valid, the account simply cannot send one. The hint has to supply what the
    error does not."""
    hint = HINTS[572006]
    assert "trial" in hint
    assert "cannot be delivered" in hint
    # And it names the way out, because the way out is not "fix the body".
    assert "approval-link" in hint


def test_build_approver_tolerates_an_empty_environment():
    approver = build_approver({})
    assert not approver.configured
    assert approver.client is None


def test_build_approver_needs_all_three_credentials():
    partial = build_approver({"TWILIO_ACCOUNT_SID": "AC", "TWILIO_AUTH_TOKEN": "tok"})
    assert partial.client is None


def test_build_approver_wires_a_client_when_configured():
    approver = build_approver(
        {
            "TWILIO_ACCOUNT_SID": "AC",
            "TWILIO_AUTH_TOKEN": "tok",
            "TWILIO_FROM_NUMBER": SENDER,
            "MANDATE_APPROVER_NUMBER": APPROVER,
        }
    )
    assert approver.configured
    assert approver.to_number == APPROVER


def test_compose_is_one_sms_segment_for_a_realistic_amount(tmp_path, paypal, now):
    gw = build(tmp_path, paypal, None)
    try:
        from mandate.engine.money import Money
        from mandate.gateway.store import Hold

        hold = Hold(
            decision_id="dec_0123456789abcdef",
            state=HoldState.AWAITING_HUMAN,
            policy_id="demo",
            merchant_id="m_cloudspend",
            merchant_name="Cloudspend Inc",
            quote_id="q_1",
            amount=Money.from_paypal("180.00", "USD"),
            fingerprint="f",
            categories=frozenset(),
            engine_outcome="hold_for_approval",
            requested_at=now,
            updated_at=now,
        )
        text = compose(hold, "https://mandate.test/approve/" + "t" * 43, minutes=15)
        # Over one segment the message is split, billed twice, and can arrive out
        # of order -- the link landing before the context that explains it.
        assert len(text) <= 160, len(text)
    finally:
        gw.store.close()


# -- resending ------------------------------------------------------------


async def test_a_resend_kills_the_previous_link(tmp_path, paypal, now):
    twilio = Twilio()
    gw = build(tmp_path, paypal, Approver(twilio.client(), APPROVER))
    try:
        result = await over_threshold(gw, now)
        first = result.approval_token

        hold, notice = await gw.resend_approval(result.decision_id, now=now)
        assert notice.sent
        assert hold.decision_id == result.decision_id
        second = twilio.form["Body"].rsplit("/approve/", 1)[1].split()[0]
        assert second != first

        # A resend usually means the first message reached somewhere it should
        # not have. Leaving both links live would be the wrong way to fix that.
        assert gw.store.consume_approval_token(first, at=now) is None
        assert gw.store.consume_approval_token(second, at=now) is not None
    finally:
        gw.store.close()


async def test_resending_an_unknown_decision_is_a_key_error(tmp_path, paypal, now):
    from mandate.gateway.store import UnknownHold

    gw = build(tmp_path, paypal, None)
    try:
        with pytest.raises(UnknownHold):
            await gw.resend_approval("dec_nope")
    finally:
        gw.store.close()


async def test_you_cannot_resend_for_a_hold_that_is_not_waiting(tmp_path, paypal, now):
    twilio = Twilio()
    gw = build(tmp_path, paypal, Approver(twilio.client(), APPROVER))
    try:
        result = await over_threshold(gw, now)
        token = result.approval_token
        await gw.approve(token, approver="ops@example.com", now=now)
        with pytest.raises(GatewayError) as caught:
            await gw.resend_approval(result.decision_id, now=now)
        assert "not awaiting a human" in str(caught.value)
    finally:
        gw.store.close()


# -- Store.note -----------------------------------------------------------


def test_note_refuses_to_write_columns_it_was_not_given_permission_to(tmp_path, paypal, now):
    gw = build(tmp_path, paypal, None)
    try:
        with pytest.raises(ValueError) as caught:
            gw.store.note("dec_anything", state="captured")
        assert "state" in str(caught.value)
    finally:
        gw.store.close()


def test_note_on_an_unknown_hold_raises_rather_than_updating_nothing(tmp_path, paypal, now):
    from mandate.gateway.store import UnknownHold

    gw = build(tmp_path, paypal, None)
    try:
        with pytest.raises(UnknownHold):
            gw.store.note("dec_nope", last_error="boom")
    finally:
        gw.store.close()


async def test_note_does_not_move_the_hold_or_write_an_event(tmp_path, paypal, now):
    gw = build(tmp_path, paypal, None)
    try:
        result = await over_threshold(gw, now)
        before = len(gw.store.events(result.decision_id))
        hold = gw.store.note(result.decision_id, last_error="approval SMS: nope")
        assert hold.state is HoldState.AWAITING_HUMAN
        assert hold.last_error == "approval SMS: nope"
        # hold_events is the state machine's history. A send that failed is a fact
        # about the hold, not a change in what the hold is.
        assert len(gw.store.events(result.decision_id)) == before
    finally:
        gw.store.close()
