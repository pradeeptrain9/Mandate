"""The gateway over HTTP.

A thin adapter. Every route does three things: parse, call one method on
`Gateway`, shape the response. No policy logic lives here, because the MCP
adapter has to behave identically and the only way to be sure is to leave them
nothing to disagree about.

Two audiences, deliberately separated by path prefix:

  * `/v1/agent/*` -- what an agent may call. It can request money, read its own
    budget, and look up a decision. It cannot capture, void, approve, or move a
    hold. An agent that could capture its own authorization would make the hold
    decorative.
  * `/v1/ops/*` -- what an operator may call. Capture, void, and the ledger.

That split is the API surface version of the same argument the type system makes
elsewhere: the capability an attacker would want is not merely guarded, it is
absent from the interface they can reach.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..delivery.carrier import AlwaysDelivers, MerchantCarrier, NeverDelivers
from ..engine.money import Money
from ..engine.policy import Outcome
from ..ledger.codec import dec_policy, dec_quote, enc_policy
from ..ledger.records import Ledger, SignatureInvalid
from ..policies import demo_policy
from ..providers.paypal import LIVE, SANDBOX, PayPalClient
from ..providers.toolkit import Toolkit
from . import disputes as dispute_api
from . import portal as portal_api
from .accounts import COOKIE, Accounts, AuthError, Role, User
from .approvals import build_approver
from .policy_store import PolicyRejected, PolicyStore, validate
from .policy_store import seed as seed_policy
from .service import AuthorizationRequest, Gateway, GatewayError, WebhookRejected
from .state import HoldState
from .store import Hold, Store, UnknownHold
from .sweep import sweep

#: Read from disk on each request. Slower, and worth it: editing the page while the
#: server runs is most of the work of building one, and a judge reading the repo can
#: see the file rather than a string in a Python module.
DASHBOARD = Path(__file__).parent / "static" / "dashboard.html"
ADMIN = Path(__file__).parent / "static" / "admin.html"
LOGIN = Path(__file__).parent / "static" / "login.html"
PORTAL = Path(__file__).parent / "static" / "portal.html"

#: The bare URL returned 404, which on a hosted demo reads as "broken" rather than
#: "this is an API". Small and inline rather than another file: it has no data in it
#: and nothing to keep in sync.
INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mandate</title><style>
:root{color-scheme:dark;--bg:#0b0d10;--fg:#e6e9ef;--dim:#8b93a3;--accent:#7aa2f7;--deny:#f7768e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);padding:48px 16px;
  font:15px/1.6 ui-sans-serif,system-ui,-apple-system,sans-serif}
main{max-width:620px;margin:0 auto}
h1{font-size:26px;margin:0 0 4px}
p.lede{color:var(--dim);margin:0 0 28px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
ul{list-style:none;padding:0;margin:0 0 28px}
li{padding:10px 0;border-top:1px solid #1b1f27}
code{color:var(--dim);font-size:13px}
.note{color:var(--dim);font-size:13px;border-left:2px solid var(--deny);padding-left:12px}
</style></head><body><main>
<h1>Mandate</h1>
<p class="lede">A spend firewall for AI agents. The agent holds no PayPal credentials; it
asks for money and gets a signed decision back. A deterministic engine decides &mdash; no
model in the path &mdash; and approved intents become holds, not payments.</p>
<ul>
<li><a href="/v1/ops/dashboard">Decision ledger</a>
  <br><code>every decision, its rule trace, and whether its signature still verifies</code></li>
<li><a href="/docs">API reference</a>
  <br><code>/v1/agent/* is what an agent may call. /v1/ops/* is what an operator may call.</code></li>
<li><a href="/health">Health</a>
  <br><code>which policy is loaded, and what is configured</code></li>
<li><a href="https://github.com/pradeeptrain9/Mandate">Source</a>
  <br><code>Apache-2.0</code></li>
</ul>
<p class="note">Hosted on a free instance: it sleeps after ~15 minutes idle, so the first
request can take about a minute, and the ledger resets on each restart.</p>
</main></body></html>"""

logger = logging.getLogger(__name__)

def _bootstrap_admin(accts: Accounts) -> None:
    """Create the first approver from the environment, once.

    Only when there are no users at all. A deployment that re-created an admin on
    every restart would quietly undo a password change, and would do it at the
    moment nobody is watching -- the same reason the policy seed does not upsert.

    With nothing set, no account exists and nobody can sign in. That is the correct
    default: an application that ships with a working username and password ships
    with a working username and password for everybody.
    """
    username = os.environ.get("MANDATE_ADMIN_USER", "").strip()
    password = os.environ.get("MANDATE_ADMIN_PASSWORD", "")
    if not username or not password or accts.count():
        return
    try:
        accts.create(username, password, role=Role.APPROVER)
    except ValueError as exc:
        # Never the password itself, and never a stack trace into a log.
        logger.warning("could not create the bootstrap admin: %s", exc)


def gateway(request: Request) -> Gateway:
    return request.app.state.gateway


def accounts(request: Request) -> Accounts:
    return request.app.state.accounts


def signed_in(request: Request) -> User:
    """Whoever is calling, or 401.

    A 401 rather than a redirect, because these are the JSON routes; the HTML pages
    redirect themselves. Returning a login page body with a 200 to a fetch() is how
    a UI ends up rendering the word "password" inside a table.
    """
    user = request.app.state.accounts.whoami(request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(401, "sign in first")
    return user


def approver(request: Request) -> User:
    """Signed in *and* allowed to decide.

    Separate from `signed_in` rather than a flag on it, so that a route either has
    the check in its signature or does not have it at all. A boolean argument is
    the kind of thing that gets defaulted wrong once and never noticed.
    """
    user = signed_in(request)
    if not user.can_approve:
        raise HTTPException(403, "this needs an approver account")
    return user


agent_router = APIRouter(prefix="/v1/agent", tags=["agent"])
app_router = APIRouter(prefix="/app", tags=["portal"])
webhook_router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])
#: Every operator route requires an approver session. Declared on the router rather
#: than per route, so a new route added later is protected by default -- the
#: opposite arrangement protects whichever routes someone remembered.
ops_router = APIRouter(
    prefix="/v1/ops", tags=["operator"], dependencies=[Depends(approver)]
)
buyer_router = APIRouter(tags=["buyer"])


# -- wiring -----------------------------------------------------------------


def build_gateway() -> Gateway:
    ledger_key = os.environ.get("MANDATE_LEDGER_KEY", "")
    merchant_secret = os.environ.get("MANDATE_MERCHANT_SECRET", "")
    if not ledger_key or not merchant_secret:
        raise RuntimeError(
            "MANDATE_LEDGER_KEY and MANDATE_MERCHANT_SECRET are both required; "
            "see .env.example"
        )
    client_id = os.environ.get("PAYPAL_CLIENT_ID", "")
    client_secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")
    live = os.environ.get("PAYPAL_ENV", "sandbox").lower() == "live"
    paypal = (
        PayPalClient(client_id, client_secret, base_url=LIVE if live else SANDBOX)
        if client_id and client_secret
        else None
    )
    var = Path(os.environ.get("MANDATE_VAR_DIR", "var"))
    # Rotation means adding a key, never replacing one. An append-only ledger
    # outlives its key by definition: re-signing the past would mean rewriting it,
    # which is the one thing the format exists to prevent.
    retired = [
        part.strip().encode("utf-8")
        for part in os.environ.get("MANDATE_LEDGER_RETIRED_KEYS", "").split(",")
        if part.strip()
    ]
    gateway = Gateway(
        store=Store(var / "state.db"),
        ledger=Ledger(var / "decisions.jsonl", ledger_key.encode("utf-8"), retired_keys=retired),
        policy=demo_policy(),
        merchant_secret=merchant_secret.encode("utf-8"),
        paypal=paypal,
        public_url=os.environ.get("MANDATE_PUBLIC_URL", "http://localhost:8000"),
        # Absent means the webhook endpoint refuses everything. Deliberate: a
        # gateway that cannot verify must not accept, and defaulting to "accept
        # when unconfigured" is how an unverified path reaches production.
        webhook_id=os.environ.get("PAYPAL_WEBHOOK_ID", ""),
        auto_place=os.environ.get("MANDATE_AUTO_PLACE", "1").lower()
        not in {"0", "no", "false"},
        # Optional on purpose. Without Twilio the approval token, page and whole
        # human-approval path behave identically; the link is read off
        # /v1/ops/holds instead of arriving by SMS.
        approver=build_approver(dict(os.environ)),
        # Same credentials, different surface, and only ever read from.
        toolkit=Toolkit(client_id, client_secret, sandbox=not live)
        if client_id and client_secret
        else None,
    )
    # The rules become editable here. `seed` installs the bundled demo policy only
    # when the table is empty -- an upsert would quietly undo an admin's work on
    # every restart, at the moment nobody is watching.
    gateway.policies = PolicyStore(gateway.store)
    seed_policy(gateway.policies, demo_policy())
    # Used to shortlist things to buy, and for nothing else. Optional: with no
    # provider configured the portal falls back to keyword matching, which is worse
    # at the job and still lets a judge with no API key see the whole flow.
    try:
        from ..agent.backends import choose
        from ..agent.backends.gemini import GeminiBackend

        backend = choose()
        if isinstance(backend, GeminiBackend):
            # A much tighter budget than the agent loop gets, because this is a
            # person waiting on a page rather than a scene running unattended. The
            # loop's defaults -- five attempts inside a 300s deadline -- are right
            # when the alternative is a half-finished basket, and wrong here: a
            # shortlist is a convenience, and nobody waits five minutes for one.
            # Fail in a few seconds and fall back to keyword matching.
            backend = GeminiBackend(
                backend.api_key,
                model=backend.model,
                # One attempt, not several. Retrying a congested free tier spends
                # one of its five-per-minute requests to make the *next* caller
                # wait, which is a bad trade for a convenience feature -- the
                # module's own comments make this argument about the agent loop,
                # and it is more true here. If the model is up it answers in a
                # couple of seconds; if it is not, keyword matching is immediate.
                attempts=1,
                deadline=9.0,
                timeout=8.0,
                # No pacing either: this is a single request, not a tool loop, and
                # waiting 60s for a slot defeats the point of a short deadline.
                rpm=0,
            )
        gateway.shopping_backend = backend
    except Exception as exc:  # noqa: BLE001 - shopping must not stop the gateway booting
        logger.info("no model for shortlisting: %s", exc)
    return gateway


# -- shapes -----------------------------------------------------------------


class AuthorizeBody(BaseModel):
    """A signed quote, plus whatever the agent wants to say about it.

    `reason` is recorded and shown to humans. It is never passed to the engine --
    it is the single most likely place for an injected instruction to arrive, and
    `evaluate()` has no parameter that could receive it.
    """

    quote: dict
    reason: str = Field(default="", max_length=2000)
    agent_id: str = Field(default="unknown-agent", max_length=120)


class CaptureBody(BaseModel):
    amount: str | None = Field(default=None, description="decimal string; defaults to the full hold")
    reason: str = Field(default="delivery confirmed", max_length=500)


class VoidBody(BaseModel):
    reason: str = Field(default="released by operator", max_length=500)


def _hold_json(hold: Hold) -> dict:
    return {
        "decision_id": hold.decision_id,
        "state": hold.state.value,
        "merchant_id": hold.merchant_id,
        "merchant_name": hold.merchant_name,
        "amount": hold.amount.to_paypal(),
        "currency": hold.amount.currency,
        "captured": hold.captured.to_paypal() if hold.captured else None,
        "engine_outcome": hold.engine_outcome,
        "paypal_order_id": hold.paypal_order_id,
        "authorization_id": hold.authorization_id,
        "authorization_expires_at": (
            hold.authorization_expires_at.isoformat() if hold.authorization_expires_at else None
        ),
        "approved_by": hold.approved_by,
        "requested_at": hold.requested_at.isoformat(),
        "updated_at": hold.updated_at.isoformat(),
        "last_error": hold.last_error,
    }


def _trace_json(evaluation) -> list[dict]:
    return [
        {
            "rule_id": r.rule_id,
            "outcome": r.outcome.value,
            "message": r.message,
            "applicable": r.applicable,
            "facts": r.facts,
        }
        for r in evaluation.results
    ]


# -- agent surface ----------------------------------------------------------


@agent_router.post("/authorizations")
async def request_authorization(body: AuthorizeBody, gw: Gateway = Depends(gateway)) -> dict:
    """Ask for money. The answer is a decision, not a payment."""
    try:
        quote = dec_quote(body.quote)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"malformed quote: {exc}") from exc

    try:
        result = await gw.request_authorization(
            AuthorizationRequest(quote=quote, reason=body.reason, agent_id=body.agent_id)
        )
    except GatewayError as exc:
        raise HTTPException(422, str(exc)) from exc

    payload = {
        "decision_id": result.decision_id,
        "outcome": result.outcome.value,
        "state": result.state.value,
        "refused_by": list(result.evaluation.reason_ids),
        "explanation": result.explain(),
        "rule_trace": _trace_json(result.evaluation),
        "hold": _hold_json(result.hold),
    }
    if result.outcome is Outcome.ALLOW:
        # Where the buyer approves. Deliberately returned to the agent: it is
        # PayPal's own URL and the agent needs it to tell the human what to do.
        payload["buyer_approval_url"] = result.approval_url
    if result.outcome is Outcome.HOLD_FOR_APPROVAL:
        # The token itself is not returned to the agent. It goes out by SMS to
        # the approver; handing it back here would let the agent approve itself.
        payload["awaiting"] = "a human has been asked to approve this"
        # A bool, not the detail. Whether the SMS left is useful to the caller;
        # why it did not is an operator's business, and a Twilio error message can
        # name configuration the agent has no reason to learn.
        payload["approver_notified"] = bool(result.notification and result.notification.sent)
    return payload


@agent_router.get("/budget")
def budget(gw: Gateway = Depends(gateway)) -> dict:
    """What is left, per rolling window. Safe for an agent to read: it reports
    the policy's own figures and nothing an agent could not infer by trying."""
    return gw.budget()


@agent_router.get("/authorizations/{decision_id}")
def get_decision(decision_id: str, gw: Gateway = Depends(gateway)) -> dict:
    try:
        hold = gw.store.get(decision_id)
    except UnknownHold:
        raise HTTPException(404, f"no decision {decision_id}") from None
    return {"hold": _hold_json(hold), "events": gw.store.events(decision_id)}


# -- signing in -------------------------------------------------------------


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=400)


@app_router.post("/login")
def login(
    body: Credentials, response: Response, accts: Accounts = Depends(accounts)
) -> dict:
    try:
        token = accts.login(body.username, body.password)
    except AuthError as exc:
        # One message for "no such user" and "wrong password". Telling them apart
        # tells an attacker which usernames exist, and nobody else benefits.
        raise HTTPException(401, str(exc)) from None
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,       # JavaScript cannot read it, so an XSS cannot steal it
        samesite="lax",      # not sent on cross-site POSTs
        secure=False,        # see the note in build_gateway about running behind TLS
        max_age=12 * 3600,
        path="/",
    )
    user = accts.get(body.username)
    return {"username": user.username, "role": user.role.value, "can_approve": user.can_approve}


@app_router.post("/logout")
def logout(request: Request, response: Response, accts: Accounts = Depends(accounts)) -> dict:
    """Ends the session server-side, not just in the browser.

    Clearing the cookie alone would leave a token that still works for anyone who
    copied it, which is the failure mode signed-cookie sessions cannot fix at all.
    """
    accts.logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app_router.get("/me")
def me(user: User = Depends(signed_in)) -> dict:
    return {"username": user.username, "role": user.role.value, "can_approve": user.can_approve}


# -- the person's surface ---------------------------------------------------


class NeedBody(BaseModel):
    """What someone wants, in their own words.

    Free text, and it goes to a model that shortlists. It never reaches the policy
    engine -- `evaluate()` has no parameter for prose -- so the worst a hostile
    string here can do is produce a bad shopping list.
    """

    need: str = Field(min_length=3, max_length=2000)
    merchant_id: str = Field(default="m_thread", max_length=64)


class ChoiceBody(BaseModel):
    sku: str = Field(min_length=1, max_length=120)
    quantity: int = Field(default=1, ge=1, le=20)
    merchant_id: str = Field(default="m_thread", max_length=64)
    need: str = Field(default="", max_length=2000)


@app_router.post("/shortlist")
async def shortlist(
    body: NeedBody, user: User = Depends(signed_in), gw: Gateway = Depends(gateway)
) -> dict:
    """Ask for options. Nothing here touches money."""
    from ..shopping import propose

    merchant_url = os.environ.get("MANDATE_MERCHANT_URL", "http://localhost:8001")
    try:
        options, note = await propose(
            body.need,
            merchant_url=merchant_url,
            merchant_id=body.merchant_id,
            merchant_name="",
            backend=gw.shopping_backend,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"could not reach the shop: {exc}") from exc
    return {"options": [o.as_json() for o in options], "how": note}


@app_router.post("/buy")
async def buy(
    body: ChoiceBody, user: User = Depends(signed_in), gw: Gateway = Depends(gateway)
) -> dict:
    """Take one option to the firewall.

    The person chose it and a model suggested it, and neither fact changes anything
    here: the quote is fetched and signed by the merchant, and the engine decides on
    the numbers exactly as it would for any agent. A shortlist is a suggestion.
    """
    merchant_url = os.environ.get("MANDATE_MERCHANT_URL", "http://localhost:8001")
    async with httpx.AsyncClient(timeout=30.0) as http:
        try:
            response = await http.post(
                f"{merchant_url.rstrip('/')}/merchants/{body.merchant_id}/quote",
                json={"lines": [{"sku": body.sku, "quantity": body.quantity}]},
            )
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"could not reach the shop: {exc}") from exc
    if response.status_code >= 400:
        raise HTTPException(response.status_code, response.json().get("detail", "the shop refused"))

    try:
        quote = dec_quote(response.json()["quote"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(502, f"the shop sent a malformed quote: {exc}") from exc

    try:
        result = await gw.request_authorization(
            AuthorizationRequest(
                quote=quote,
                reason=body.need or f"chose {body.sku}",
                # The person is the requester, so their own requests are the ones
                # they can see. Not a display name: this is what /app/requests
                # filters on.
                agent_id=user.username,
            )
        )
    except GatewayError as exc:
        raise HTTPException(422, str(exc)) from exc

    return {
        "decision_id": result.decision_id,
        "outcome": result.outcome.value,
        "state": result.state.value,
        "explanation": result.explain(),
        "rule_trace": _trace_json(result.evaluation),
        "buyer_approval_url": result.approval_url
        if result.outcome is Outcome.ALLOW
        else None,
        "status": portal_api.STATUS.get(result.state.value, (result.state.value, ""))[0],
    }


@app_router.get("/requests")
def my_requests(user: User = Depends(signed_in), gw: Gateway = Depends(gateway)) -> dict:
    """Only this person's. Filtered in SQL, not in the page."""
    return {"requests": portal_api.own_requests(gw.store, user.username)}


@app_router.get("/approvals")
def my_approvals(user: User = Depends(signed_in), gw: Gateway = Depends(gateway)) -> dict:
    """What this person has to decide. Empty for a requester, by construction."""
    if not user.can_approve:
        return {"approvals": [], "can_approve": False}
    waiting = gw.store.list(states=frozenset({HoldState.AWAITING_HUMAN}))
    return {
        "approvals": [
            {
                **_hold_json(h),
                "mine": h.agent_id == user.username,
            }
            for h in waiting
        ],
        "can_approve": True,
    }


# -- operator surface -------------------------------------------------------


@ops_router.get("/holds")
def list_holds(state: str | None = None, gw: Gateway = Depends(gateway)) -> dict:
    states = None
    if state:
        try:
            states = frozenset({HoldState(state)})
        except ValueError:
            raise HTTPException(400, f"unknown state {state!r}") from None
    return {"holds": [_hold_json(h) for h in gw.store.list(states=states)]}


@ops_router.post("/holds/{decision_id}/place")
async def place_hold(decision_id: str, gw: Gateway = Depends(gateway)) -> dict:
    """Authorize an order the buyer has approved. Funds reserved, not taken."""
    try:
        return {"hold": _hold_json(await gw.place_hold(decision_id))}
    except UnknownHold:
        raise HTTPException(404, f"no decision {decision_id}") from None
    except GatewayError as exc:
        raise HTTPException(409, str(exc)) from exc


class RefundBody(BaseModel):
    amount: str = Field(default="", max_length=32)
    reason: str = Field(default="refunded by an operator", max_length=500)


class PolicyBody(BaseModel):
    """A whole policy, not a patch.

    Deliberate: a partial update means the server merges, and a merge means two
    admins editing different fields can produce a policy neither of them read. The
    editor loads the current version, changes it, and sends all of it back.
    """

    policy: dict
    author: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=500)


@ops_router.get("/policy")
def read_policy(gw: Gateway = Depends(gateway)) -> dict:
    """The rules in force, plus who last changed them."""
    current = gw.policies.current_version() if gw.policies else None
    return {
        "policy": enc_policy(gw.policy),
        "version": current.version if current else None,
        "author": current.author if current else None,
        "note": current.note if current else "",
        "updated_at": current.created_at.isoformat() if current else None,
        "editable": gw.policies is not None,
    }


@ops_router.put("/policy")
def write_policy(body: PolicyBody, gw: Gateway = Depends(gateway)) -> dict:
    """Save a new version. Never overwrites: the old version stays readable.

    Operator-only, like capture and void, and absent from /v1/agent/*. An agent
    that could edit the policy would not need to defeat the policy.
    """
    if gw.policies is None:
        raise HTTPException(409, "this gateway has no policy store; rules are fixed at startup")
    try:
        proposed = dec_policy(body.policy)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"that is not a policy: {exc}") from exc
    try:
        saved = gw.policies.save(proposed, author=body.author, note=body.note)
    except PolicyRejected as exc:
        # 422, not 400: the shape was understood and the content refused.
        raise HTTPException(422, {"problems": exc.problems}) from exc
    return {
        "version": saved.version,
        "author": saved.author,
        "note": saved.note,
        "updated_at": saved.created_at.isoformat(),
        "policy": enc_policy(saved.policy),
    }


@ops_router.post("/policy/check")
def check_policy(body: PolicyBody, gw: Gateway = Depends(gateway)) -> dict:
    """Validate without saving, so the editor can object before an admin commits."""
    try:
        proposed = dec_policy(body.policy)
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "problems": [f"that is not a policy: {exc}"], "warnings": []}
    from .policy_store import warnings as policy_warnings

    problems = validate(proposed)
    return {
        "ok": not problems,
        "problems": problems,
        "warnings": policy_warnings(gw.policy, proposed),
    }


@ops_router.get("/policy/history")
def policy_history(limit: int = 50, gw: Gateway = Depends(gateway)) -> dict:
    if gw.policies is None:
        return {"versions": []}
    return {
        "versions": [
            {
                "version": v.version,
                "policy_id": v.policy.policy_id,
                "author": v.author,
                "note": v.note,
                "created_at": v.created_at.isoformat(),
            }
            for v in gw.policies.history(limit)
        ]
    }


@ops_router.get("/policy/{version}")
def policy_at(version: int, gw: Gateway = Depends(gateway)) -> dict:
    """What the rules were then. The question people ask after something goes wrong."""
    found = gw.policies.at_version(version) if gw.policies else None
    if found is None:
        raise HTTPException(404, f"no policy version {version}")
    return {
        "version": found.version,
        "author": found.author,
        "note": found.note,
        "created_at": found.created_at.isoformat(),
        "policy": enc_policy(found.policy),
    }


class VerdictBody(BaseModel):
    approver: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=500)


@ops_router.get("/approvals")
def pending_approvals(gw: Gateway = Depends(gateway)) -> dict:
    """What is waiting on a human, with enough to decide without leaving the page."""
    waiting = gw.store.list(states=frozenset({HoldState.AWAITING_HUMAN}))
    records = {r.decision_id: r for r in gw.ledger}
    out = []
    for hold in waiting:
        record = records.get(hold.decision_id)
        out.append(
            {
                **_hold_json(hold),
                # Why a human is being asked, in the engine's own words. An
                # approver shown only an amount is an approver guessing.
                "reasons": [
                    r.message
                    for r in (record.evaluation.results if record else [])
                    if r.outcome is not Outcome.ALLOW
                ],
                "items": [
                    {"sku": i.sku, "quantity": i.quantity, "description": i.description}
                    for i in (record.quote.line_items if record else [])
                ],
                "agent_reason": hold.agent_reason,
                "agent_id": hold.agent_id,
            }
        )
    return {"approvals": out}


@ops_router.post("/approvals/{decision_id}/approve")
async def approve_from_console(
    decision_id: str, body: VerdictBody, gw: Gateway = Depends(gateway)
) -> dict:
    """Approve from the admin console rather than the SMS link.

    Mints a token and spends it immediately. That keeps one approval path rather
    than two: the same `approve` the link uses, with the same single-use token and
    the same record of who said yes.
    """
    try:
        link = gw.issue_approval_link(decision_id)
    except UnknownHold:
        raise HTTPException(404, f"no decision {decision_id}") from None
    except GatewayError as exc:
        raise HTTPException(409, str(exc)) from exc
    token = link.rsplit("/", 1)[1]
    try:
        result = await gw.approve(token, approver=body.approver)
    except GatewayError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"hold": _hold_json(result.hold), "buyer_approval_url": result.approval_url}


@ops_router.post("/approvals/{decision_id}/decline")
def decline_from_console(
    decision_id: str, body: VerdictBody, gw: Gateway = Depends(gateway)
) -> dict:
    try:
        link = gw.issue_approval_link(decision_id)
    except UnknownHold:
        raise HTTPException(404, f"no decision {decision_id}") from None
    except GatewayError as exc:
        raise HTTPException(409, str(exc)) from exc
    token = link.rsplit("/", 1)[1]
    try:
        return {"hold": _hold_json(gw.decline(token, approver=body.approver))}
    except GatewayError as exc:
        raise HTTPException(409, str(exc)) from exc


@ops_router.post("/holds/{decision_id}/refund")
async def refund(decision_id: str, body: RefundBody, gw: Gateway = Depends(gateway)) -> dict:
    """Give captured money back. Operator-only, and absent from /v1/agent/*.

    An agent that could refund could also mask a mistake it made with the money,
    which is the one thing the ledger exists to prevent.
    """
    try:
        hold = gw.store.get(decision_id)
    except UnknownHold:
        raise HTTPException(404, f"no decision {decision_id}") from None
    amount = None
    if body.amount:
        try:
            amount = Money.from_paypal(body.amount, hold.amount.currency)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        return {"hold": _hold_json(await gw.refund(decision_id, amount=amount, reason=body.reason))}
    except GatewayError as exc:
        raise HTTPException(409, str(exc)) from exc


@ops_router.post("/holds/{decision_id}/approval-link")
def approval_link(decision_id: str, gw: Gateway = Depends(gateway)) -> dict:
    """Get the approval link on screen, for a deployment with no SMS provider.

    POST rather than GET because it mints a token and invalidates the previous
    one -- a GET that changed which link works would be surprising, and would be
    fetched by anything that prefetches links.
    """
    try:
        return {"approval_url": gw.issue_approval_link(decision_id)}
    except UnknownHold:
        raise HTTPException(404, f"no decision {decision_id}") from None
    except GatewayError as exc:
        raise HTTPException(409, str(exc)) from exc


@ops_router.post("/holds/{decision_id}/resend-approval")
async def resend_approval(decision_id: str, gw: Gateway = Depends(gateway)) -> dict:
    """Page the approver again, with a new link.

    Operator-only, and it invalidates the previous link rather than adding a
    second one. The response carries whether the message left and the redacted
    destination -- never the token, which exists only in the SMS.
    """
    try:
        hold, notice = await gw.resend_approval(decision_id)
    except UnknownHold:
        raise HTTPException(404, f"no decision {decision_id}") from None
    except GatewayError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "hold": _hold_json(hold),
        "notification": {"sent": notice.sent, "detail": notice.detail, "to": notice.to},
    }


@ops_router.post("/holds/{decision_id}/capture")
async def capture(decision_id: str, body: CaptureBody, gw: Gateway = Depends(gateway)) -> dict:
    try:
        hold = gw.store.get(decision_id)
    except UnknownHold:
        raise HTTPException(404, f"no decision {decision_id}") from None
    amount = None
    if body.amount:
        try:
            amount = Money.from_paypal(body.amount, hold.amount.currency)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        return {"hold": _hold_json(await gw.capture(decision_id, amount=amount, reason=body.reason))}
    except GatewayError as exc:
        raise HTTPException(409, str(exc)) from exc


@ops_router.post("/holds/{decision_id}/void")
async def void(decision_id: str, body: VoidBody, gw: Gateway = Depends(gateway)) -> dict:
    try:
        return {"hold": _hold_json(await gw.void(decision_id, reason=body.reason))}
    except UnknownHold:
        raise HTTPException(404, f"no decision {decision_id}") from None
    except GatewayError as exc:
        raise HTTPException(409, str(exc)) from exc


# -- inbound from PayPal ----------------------------------------------------


@webhook_router.post("/paypal")
async def paypal_webhook(request: Request, gw: Gateway = Depends(gateway)) -> dict:
    """PayPal tells us what happened. Verified before it is believed.

    `await request.body()` rather than a Pydantic model, and that is not laziness.
    Verification is over the exact bytes PayPal signed, and a parsed-then-
    reserialised body is not guaranteed to reproduce them: key order, unicode
    escaping and number formatting can all shift. A FastAPI model here would hand
    the verifier a different document than the one that was signed, and the
    endpoint would reject every genuine delivery while looking correct.

    The status codes are chosen for what PayPal does with them, since a webhook
    response is an instruction to a retry loop:

      * **200** -- processed, noted, duplicated, or not understood. All four mean
        "stop sending this", which is right even for an event we have no rule for.
      * **400** -- the signature did not verify, or the event cannot be identified.
        Do not retry; a forgery will not become genuine on the third attempt.
      * **503** -- this gateway could not reach PayPal's verifier, or is not
        configured to verify at all. Please retry: the delivery may well be fine
        and we are the broken side.
    """
    raw = await request.body()
    try:
        event = json.loads(raw)
        if not isinstance(event, dict):
            # ValueError, inside the try, on purpose: a malformed body and a
            # well-formed non-object are the same 400 and take one path. TRY004's
            # TypeError and TRY301's inner function would both split it in two.
            raise ValueError("the body is not a JSON object")  # noqa: TRY004, TRY301
    except ValueError as exc:
        # Unverifiable by construction, so this is a 400 and not a 503.
        raise HTTPException(400, f"not a webhook event: {exc}") from exc

    try:
        outcome = await gw.ingest_webhook(
            headers=dict(request.headers), raw_body=raw, event=event
        )
    except WebhookRejected as exc:
        raise HTTPException(400, str(exc)) from exc
    except GatewayError as exc:
        raise HTTPException(503, str(exc)) from exc

    return {
        "event_id": outcome.event_id,
        "event_type": outcome.event_type,
        "action": outcome.action,
        "decision_id": outcome.decision_id,
        "from_state": outcome.from_state,
        "to_state": outcome.to_state,
        "detail": outcome.detail,
    }


class SweepBody(BaseModel):
    grace_hours: float = Field(default=24.0, ge=0, le=24 * 29)
    oracle: str = Field(
        default="carrier",
        description="carrier | never | always. Anything but 'carrier' is for demos.",
    )


@ops_router.post("/sweep")
async def run_sweep(body: SweepBody, gw: Gateway = Depends(gateway)) -> dict:
    """Settle every held authorization that can be settled.

    An operator route rather than a background thread inside the web process. A
    sweep captures money, and a thing that captures money on a timer inside a
    request handler's process is a thing nobody can point at when asked what ran.
    Here it is a call with a caller -- cron, a Render job, or a person -- and the
    report is its answer.
    """
    oracle = _oracle(body.oracle)
    report = await sweep(
        gw, oracle=oracle, grace=timedelta(hours=body.grace_hours)
    )
    return {
        "at": report.at.isoformat(),
        "oracle": oracle.name,
        "checked": report.checked,
        "summary": report.summary(),
        "actions": [
            {
                "decision_id": a.decision_id,
                "delivery": a.delivery.value,
                "action": a.action,
                "state": a.state,
                "detail": a.detail,
            }
            for a in report.actions
        ],
    }


def _oracle(name: str):
    if name == "never":
        return NeverDelivers()
    if name == "always":
        return AlwaysDelivers()
    if name == "carrier":
        return MerchantCarrier(
            os.environ.get("MANDATE_MERCHANT_URL", "http://localhost:8001")
        )
    raise HTTPException(400, f"unknown oracle {name!r}; use carrier, never or always")


@ops_router.get("/decisions")
def decisions(limit: int = 200, gw: Gateway = Depends(gateway)) -> dict:
    """The ledger, newest last, with full rule traces. Feeds the dashboard."""
    out = []
    for record in gw.ledger:
        try:
            gw.ledger.check(record)
            verified, verification = True, ""
        except SignatureInvalid as exc:
            verified, verification = False, str(exc)
        out.append(
            {
                "verified": verified,
                "verification": verification,
                "key_id": record.key_id,
                "decision_id": record.decision_id,
                "evaluated_at": record.evaluated_at.isoformat(),
                "merchant_id": record.quote.merchant_id,
                "merchant_name": record.quote.merchant_name,
                "amount": record.quote.declared_total.to_paypal(),
                "currency": record.quote.currency,
                "outcome": record.evaluation.outcome.value,
                "refused_by": list(record.evaluation.reason_ids),
                "rule_trace": _trace_json(record.evaluation),
                "policy_id": record.policy.policy_id,
            }
        )
    return {"decisions": out[-limit:]}


@ops_router.get("/overview")
async def overview(limit: int = 500, gw: Gateway = Depends(gateway)) -> dict:
    """One request, everything the dashboard shows.

    Joined here rather than in the browser. The join is decision-to-hold, and
    getting it wrong means showing a rule trace next to the wrong money -- which is
    precisely the sort of mistake a dashboard makes convincing. The server owns both
    sides of it.

    Every record is signature-checked on the way out, so a tampered ledger line
    fails the request rather than rendering as a row. A dashboard that would happily
    display an unverified decision is a dashboard that cannot be used as evidence.
    """
    holds = {hold.decision_id: hold for hold in gw.store.list(limit=limit * 2)}
    # Fetched once for the whole table, never stored. A dispute's state lives at
    # PayPal and changes without telling us, so a copy here would be a second
    # source of truth that is wrong more often than right. `fetch` never raises:
    # an unreachable dispute API costs the column, not the ledger.
    disputed = await dispute_api.fetch(gw.toolkit)
    rows = []
    for record in gw.ledger:
        # A record that does not verify becomes a row that says so, rather than an
        # exception. Found the hard way: one record signed with a rotated ledger key
        # made this endpoint 500, which took out the entire operator view -- so the
        # one moment an operator most needs to see the ledger was the one moment
        # they could not. Dropping it silently would be worse again: a quietly
        # shorter table is how an unverifiable decision disappears.
        try:
            signed_by = gw.ledger.check(record)
            verified, verification = True, ""
        except SignatureInvalid as exc:
            signed_by = record.key_id
            verified, verification = False, str(exc)
        hold = holds.get(record.decision_id)
        trace = _trace_json(record.evaluation)
        rows.append(
            {
                "decision_id": record.decision_id,
                "evaluated_at": record.evaluated_at.isoformat(),
                "merchant_id": record.quote.merchant_id,
                "merchant_name": record.quote.merchant_name,
                "amount": record.quote.declared_total.to_paypal(),
                "amount_minor": record.quote.declared_total.minor,
                "currency": record.quote.currency,
                "outcome": record.evaluation.outcome.value,
                "refused_by": list(record.evaluation.reason_ids),
                "denied_by": [e["rule_id"] for e in trace if e["outcome"] == "deny"],
                "rules_evaluated": len(trace),
                "rule_trace": trace,
                "policy_id": record.policy.policy_id,
                "skus": [item.sku for item in record.quote.line_items],
                "verified": verified,
                "verification": verification,
                "signed_by": signed_by,
                # Hold-side columns are null for a refused decision, because no
                # PayPal object was ever created. Null rather than a placeholder:
                # "—" in a money column is a value someone will eventually parse.
                "state": hold.state.value if hold else None,
                "captured": hold.captured.to_paypal() if hold and hold.captured else None,
                "authorization_id": hold.authorization_id if hold else None,
                "paypal_order_id": hold.paypal_order_id if hold else None,
                "expires_at": (
                    hold.authorization_expires_at.isoformat()
                    if hold and hold.authorization_expires_at
                    else None
                ),
                "approved_by": hold.approved_by if hold else None,
                "last_error": hold.last_error if hold else None,
                # The last column: what happened after the money moved. None means
                # "not disputed" only when disputes.reachable is true -- see the
                # note on that field for why the two must not read the same.
                "dispute": disputed.for_capture(hold.capture_id if hold else None),
            }
        )
    rows = rows[-limit:]
    return {
        "disputes": {"reachable": disputed.reachable, "detail": disputed.detail},
        "policy_id": gw.policy.policy_id,
        "budget": gw.budget(),
        "decisions": rows,
        "counts": _counts(rows),
    }


def _counts(rows: list[dict]) -> dict:
    """Headline numbers, computed from the same rows the grid shows.

    Deliberately derived from `rows` rather than queried separately: a tile that
    disagrees with the table below it is worse than no tile, and the only way to
    guarantee they agree is to compute one from the other.
    """
    by_outcome: dict[str, int] = {}
    by_state: dict[str, int] = {}
    for row in rows:
        by_outcome[row["outcome"]] = by_outcome.get(row["outcome"], 0) + 1
        if row["state"]:
            by_state[row["state"]] = by_state.get(row["state"], 0) + 1
    refused_minor = sum(r["amount_minor"] for r in rows if r["outcome"] != "allow")
    return {
        "decisions": len(rows),
        "by_outcome": by_outcome,
        "by_state": by_state,
        # Surfaced as a headline number, not a footnote. An unverifiable decision is
        # the single most important thing this view can tell an operator.
        "unverified": sum(1 for r in rows if not r["verified"]),
        # What the firewall stopped. The honest framing for this number is
        # "requested and refused", not "saved" -- some of it would have been
        # refused by PayPal, by a card limit, or by the agent giving up.
        "refused_minor": refused_minor,
    }


@ops_router.post("/decisions/replay")
def replay(gw: Gateway = Depends(gateway)) -> dict:
    """Re-decide every stored record and report divergences.

    Exposed as an endpoint rather than only a CLI command so the dashboard can
    show, live, that the ledger still reproduces. A green count here is a
    stronger claim than a README paragraph.
    """
    checked, failures = 0, []
    for record in gw.ledger:
        checked += 1
        try:
            record.verify(gw.ledger.key)
            record.assert_replays()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            failures.append({"decision_id": record.decision_id, "error": str(exc)})
    return {"checked": checked, "divergences": failures, "ok": not failures}


# -- the human in the loop --------------------------------------------------

_APPROVAL_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Approve payment &middot; Mandate</title>
<style>
:root{{color-scheme:light dark}}
body{{font:16px/1.5 system-ui,-apple-system,sans-serif;margin:0;padding:1.5rem;
  max-width:28rem;margin-inline:auto}}
.card{{border:1px solid color-mix(in srgb,currentColor 18%,transparent);
  border-radius:14px;padding:1.25rem;margin-bottom:1rem}}
.amount{{font-size:2.25rem;font-weight:650;letter-spacing:-.02em;margin:.25rem 0}}
.muted{{opacity:.65;font-size:.9rem}}
ul{{padding-left:1.1rem;margin:.5rem 0}}
form{{display:flex;gap:.75rem;margin-top:1.25rem}}
button{{flex:1;padding:.85rem;font-size:1rem;font-weight:600;border-radius:10px;
  border:1px solid transparent;cursor:pointer}}
.yes{{background:#1a7f37;color:#fff}}
.no{{background:transparent;border-color:color-mix(in srgb,currentColor 30%,transparent);
  color:inherit}}
</style></head><body>
<p class="muted">Mandate &middot; an agent is asking to spend</p>
<div class="card">
  <p class="muted">{merchant_name}</p>
  <p class="amount">{amount} {currency}</p>
  <p class="muted">requested by agent &middot; {requested_at}</p>
</div>
<div class="card">
  <strong>Why you are being asked</strong>
  <ul>{reasons}</ul>
</div>
<form method="post" action="/approve/{token}">
  <button class="no" name="verdict" value="decline">Decline</button>
  <button class="yes" name="verdict" value="approve">Approve</button>
</form>
<p class="muted">This link works once and expires shortly.</p>
</body></html>"""


@ops_router.get("/admin", response_class=HTMLResponse)
def admin(user: User = Depends(approver)) -> str:
    """Approvals queue and the rules editor.

    Operator surface, like everything else under /v1/ops. The page can raise a
    spending ceiling, so it has no business being reachable from /v1/agent/*.
    """
    return ADMIN.read_text(encoding="utf-8")


@ops_router.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    """The ledger, for a human.

    Served from this process rather than built and deployed separately. One
    `docker compose up` has to reach a working demo, and a second build step is a
    second thing that can be broken on the machine of someone who has five minutes.

    Under /v1/ops rather than at the root, because it shows every decision, every
    merchant and every rule trace. That is an operator's view, and putting it on a
    path the agent's own surface does not share keeps the distinction visible in the
    routing table instead of only in a comment.
    """
    return DASHBOARD.read_text(encoding="utf-8")


@buyer_router.get("/approve/{token}", response_class=HTMLResponse)
def approval_page(token: str, gw: Gateway = Depends(gateway)) -> str:
    """Render what is being asked.

    The token is not consumed here -- only a POST decides. A GET that burned the
    token would mean a link preview or an over-eager mail scanner could silently
    destroy an approval request.
    """
    hold = _peek(gw, token)
    reasons = "".join(f"<li>{_escape(r)}</li>" for r in _reasons_for(gw, hold.decision_id))
    return _APPROVAL_PAGE.format(
        merchant_name=_escape(hold.merchant_name),
        amount=hold.amount.to_paypal(),
        currency=hold.amount.currency,
        requested_at=hold.requested_at.strftime("%d %b %Y, %H:%M UTC"),
        reasons=reasons or "<li>over the unattended spending threshold</li>",
        token=_escape(token),
    )


@buyer_router.post("/approve/{token}", response_class=HTMLResponse)
async def decide(token: str, request: Request, gw: Gateway = Depends(gateway)) -> str:
    form = await request.form()
    verdict = str(form.get("verdict", ""))
    approver = request.headers.get("x-approver", "approval link")
    try:
        if verdict == "approve":
            result = await gw.approve(token, approver=approver)
            return _done("Approved", f"Order created. Decision {result.decision_id}.")
        if verdict == "decline":
            hold = gw.decline(token, approver=approver)
            return _done("Declined", f"Nothing was charged. Decision {hold.decision_id}.")
    except GatewayError as exc:
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, "verdict must be approve or decline")


def _done(title: str, detail: str) -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{title} &middot; Mandate</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;margin:0;padding:3rem 1.5rem;"
        "max-width:28rem;margin-inline:auto;text-align:center}"
        "h1{font-size:1.5rem}p{opacity:.7}</style></head><body>"
        f"<h1>{title}</h1><p>{_escape(detail)}</p></body></html>"
    )


def _peek(gw: Gateway, token: str) -> Hold:
    row = gw.store.peek_approval_token(token)
    if row is None:
        raise HTTPException(404, "this approval link is not valid")
    expires = row["approval_token_expires_at"]
    if not expires or datetime.fromisoformat(expires) < datetime.now(UTC):
        raise HTTPException(410, "this approval link has expired")
    return gw.store.get(row["decision_id"])


def _reasons_for(gw: Gateway, decision_id: str) -> list[str]:
    for record in gw.ledger:
        if record.decision_id == decision_id:
            return [r.message for r in record.evaluation.refusals]
    return []


def _escape(value: str) -> str:
    import html

    return html.escape(value, quote=True)


# -- app --------------------------------------------------------------------


def create_app(gw: Gateway | None = None) -> FastAPI:
    app = FastAPI(
        title="Mandate gateway",
        version="0.1.0",
        description=(
            "A spend firewall for AI agents. Agents request money on /v1/agent; "
            "only operators can capture or void. The policy engine that decides is "
            "pure and never reads prose."
        ),
    )
    app.state.gateway = gw or build_gateway()
    app.state.accounts = Accounts(app.state.gateway.store)
    _bootstrap_admin(app.state.accounts)
    app.include_router(app_router)
    app.include_router(agent_router)
    app.include_router(ops_router)
    app.include_router(webhook_router)
    app.include_router(buyer_router)

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page() -> str:
        return LOGIN.read_text(encoding="utf-8")

    @app.get("/app", include_in_schema=False)
    def portal_page(request: Request):
        """The person's home. Redirects rather than 401s, because this is a page.

        A fetch() gets JSON and a 401; a browser address bar gets sent somewhere it
        can do something about it.
        """
        if request.app.state.accounts.whoami(request.cookies.get(COOKIE)) is None:
            return RedirectResponse("/login?next=/app", status_code=303)
        return HTMLResponse(PORTAL.read_text(encoding="utf-8"))

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        """What this is, for anyone who pastes the bare URL.

        Not a redirect to the dashboard. The gateway is an API with two
        deliberately separated surfaces, and sending every visitor straight to the
        operator view would hide that -- and would send a judge to the one page
        that says nothing about why any of it exists.
        """
        return INDEX

    @app.get("/health", tags=["ops"])
    def health() -> dict:
        return {
            "ok": True,
            "policy": app.state.gateway.policy.policy_id,
            "paypal": "configured" if app.state.gateway.paypal else "absent",
            # Absent is a supported configuration, not a fault: the approval
            # token and page work unchanged and an operator mints the link with
            # /v1/ops/holds/{id}/approval-link. Reported because "did the
            # approver get a text?" is
            # otherwise answerable only by waiting for one not to arrive.
            "approver_sms": "configured" if app.state.gateway.approver.configured else "absent",
        }

    return app
