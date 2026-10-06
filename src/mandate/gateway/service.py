"""The gateway, as a service. One code path, two adapters on top of it.

Both the REST API and the MCP server call into this module and neither contains
any policy logic. That is the point: an agent reaching Mandate over MCP and an
agent reaching it over HTTP must be governed identically, and the cheapest way to
guarantee that is to leave them nothing to disagree about.

The order of operations in `request_authorization` is deliberate and worth reading
as a sequence, because each step exists to make the next one safe:

  1. Verify the merchant's signature. An unsigned or tampered quote is not a
     policy question.
  2. Check the quote's arithmetic and freshness. A total that does not match its
     lines is a broken quote, not a refusal.
  3. Project to `PolicyInput`, which drops every description and name. From here
     on, nothing the merchant wrote in prose can reach anything.
  4. Build the ledger window from the store, deriving reserved-ness from hold
     state so it cannot drift.
  5. Evaluate. Pure, deterministic, no model.
  6. Write the signed decision record **before** touching PayPal, so there is no
     window in which money has moved and nothing says why.
  7. Only then create the PayPal order.

Step 6 before step 7 is the one that matters. If the process dies between them
the ledger holds a decision with no PayPal object, which is recoverable and
visible. The other order would leave a hold on someone's funds with no record of
what permitted it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..engine.money import Money
from ..engine.policy import Evaluation, Outcome, Policy
from ..engine.quote import MerchantQuote, QuoteIntegrityError
from ..ledger.records import DecisionRecord, Ledger, build
from ..providers.paypal import Authorization, PayPalClient, PayPalError, approval_link
from .state import HoldState
from .store import Hold, Store, UnknownHold
from .webhooks import WebhookOutcome, apply as apply_webhook, event_id_of, locate as locate_hold

#: How far back to look when building the engine's window. Must exceed the
#: longest envelope in any policy, or an envelope would silently stop seeing its
#: own history. Checked in `_window_span`.
WINDOW_MARGIN = timedelta(days=2)


class WebhookRejected(RuntimeError):
    """The delivery was not authentic, or cannot be identified.

    Separate from GatewayError because the right HTTP answer differs: a rejected
    delivery must *not* be retried, while a gateway that could not reach PayPal's
    verifier should be.
    """


class GatewayError(RuntimeError):
    """Something the caller did wrong, or a PayPal failure worth surfacing."""


@dataclass(frozen=True)
class AuthorizationRequest:
    """What an agent asks for.

    `reason` is the agent's own account of why, in its own words. It is stored on
    the record for a human to read and is never shown to the engine -- it is the
    most obvious place an injected instruction would arrive, and the engine has
    no parameter for it.
    """

    quote: MerchantQuote
    reason: str = ""
    agent_id: str = "unknown-agent"


@dataclass(frozen=True)
class AuthorizationResult:
    decision_id: str
    outcome: Outcome
    state: HoldState
    evaluation: Evaluation
    hold: Hold
    record: DecisionRecord
    approval_url: str | None = None
    approval_token: str | None = None

    @property
    def refused(self) -> bool:
        return self.outcome is Outcome.DENY

    @property
    def needs_human(self) -> bool:
        return self.outcome is Outcome.HOLD_FOR_APPROVAL

    def explain(self) -> list[str]:
        """The engine's own sentences, in rule order. No model involved."""
        return [f"{r.rule_id}: {r.message}" for r in self.evaluation.refusals]


class Gateway:
    def __init__(
        self,
        *,
        store: Store,
        ledger: Ledger,
        policy: Policy,
        merchant_secret: bytes,
        paypal: PayPalClient | None = None,
        public_url: str = "http://localhost:8000",
        approval_ttl: timedelta = timedelta(minutes=15),
        webhook_id: str = "",
        # Whether a buyer approving at PayPal should immediately reserve their
        # funds. On, because that is the flow the project is about: the policy
        # already said yes, the order already exists, and leaving the hold
        # unplaced would mean an agent cannot complete a purchase without a human
        # pressing a second button for no reason. Off is for anyone who wants the
        # authorize step held back for review.
        auto_place: bool = True,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.policy = policy
        self.merchant_secret = merchant_secret
        self.paypal = paypal
        self.public_url = public_url.rstrip("/")
        self.approval_ttl = approval_ttl
        self.webhook_id = webhook_id
        self.auto_place = auto_place

    # -- the main path ---------------------------------------------------

    async def request_authorization(
        self, request: AuthorizationRequest, *, now: datetime | None = None
    ) -> AuthorizationResult:
        moment = now or datetime.now(timezone.utc)
        quote = request.quote

        # 1 and 2. Authenticity and internal consistency, before any rule runs.
        try:
            quote.verify(self.merchant_secret)
            quote.check_integrity(now=moment, max_age=self.policy.quote_max_age)
        except QuoteIntegrityError as exc:
            raise GatewayError(str(exc)) from exc

        # 3, 4 and 5. Project, gather history, decide.
        window = self.store.ledger_window(since=moment - self._window_span())
        record = build(
            quote=quote,
            policy=self.policy,
            ledger_window=window,
            evaluated_at=moment,
            key=self.ledger.key,
        )

        # 6. The record exists before anything irreversible happens.
        self.ledger.append(record)

        outcome = record.evaluation.outcome
        self.store.create(
            decision_id=record.decision_id,
            quote=quote,
            policy_id=self.policy.policy_id,
            engine_outcome=outcome.value,
            state=HoldState.RECEIVED,
            at=moment,
            detail=f"evaluated against {self.policy.policy_id}",
        )
        trace = "; ".join(record.evaluation.reason_ids) or "allowed by every rule"

        if outcome is Outcome.DENY:
            hold = self.store.transition(
                record.decision_id, HoldState.REFUSED, detail=trace, at=moment
            )
            return AuthorizationResult(
                record.decision_id, outcome, hold.state, record.evaluation, hold, record
            )

        if outcome is Outcome.HOLD_FOR_APPROVAL:
            hold = self.store.transition(
                record.decision_id, HoldState.AWAITING_HUMAN, detail=trace, at=moment
            )
            token = self.store.issue_approval_token(
                record.decision_id, ttl=self.approval_ttl, at=moment
            )
            return AuthorizationResult(
                record.decision_id,
                outcome,
                hold.state,
                record.evaluation,
                hold,
                record,
                approval_token=token,
            )

        # 7. Allowed outright: straight to PayPal, no human in the loop.
        return await self._create_order(record, quote, at=moment)

    async def approve(
        self, token: str, *, approver: str, now: datetime | None = None
    ) -> AuthorizationResult:
        """A human said yes. Resolves the SMS token and proceeds to PayPal."""
        moment = now or datetime.now(timezone.utc)
        hold = self.store.consume_approval_token(token, at=moment)
        if hold is None:
            raise GatewayError("approval link is unknown, already used, or expired")
        record = self._record_for(hold.decision_id)
        return await self._create_order(record, record.quote, at=moment, approver=approver)

    def decline(self, token: str, *, approver: str, now: datetime | None = None) -> Hold:
        """A human said no. Nothing was ever created at PayPal, so there is
        nothing to void -- the refusal is the whole action."""
        moment = now or datetime.now(timezone.utc)
        hold = self.store.consume_approval_token(token, at=moment)
        if hold is None:
            raise GatewayError("approval link is unknown, already used, or expired")
        return self.store.transition(
            hold.decision_id,
            HoldState.DECLINED_BY_HUMAN,
            detail=f"declined by {approver}",
            at=moment,
            approved_by=approver,
        )

    async def _create_order(
        self,
        record: DecisionRecord,
        quote: MerchantQuote,
        *,
        at: datetime,
        approver: str | None = None,
    ) -> AuthorizationResult:
        if self.paypal is None:
            raise GatewayError("no PayPal client configured on this gateway")
        try:
            order = await self.paypal.create_authorization_order(
                currency=quote.currency,
                value=quote.declared_total.to_paypal(),
                items=_paypal_items(quote),
                decision_id=record.decision_id,
                return_url=f"{self.public_url}/buyer/return/{record.decision_id}",
                cancel_url=f"{self.public_url}/buyer/cancel/{record.decision_id}",
            )
        except PayPalError as exc:
            hold = self.store.transition(
                record.decision_id,
                HoldState.FAILED,
                detail=str(exc),
                at=at,
                last_error=str(exc),
            )
            raise GatewayError(f"PayPal refused the order: {exc}") from exc

        hold = self.store.transition(
            record.decision_id,
            HoldState.AWAITING_BUYER,
            detail=f"order {order.get('id')} created"
            + (f", approved by {approver}" if approver else ""),
            at=at,
            paypal_order_id=order.get("id"),
            approval_url=approval_link(order),
            placed_at=at,
            approved_by=approver,
        )
        return AuthorizationResult(
            record.decision_id,
            record.evaluation.outcome,
            hold.state,
            record.evaluation,
            hold,
            record,
            approval_url=hold.approval_url,
        )

    # -- the hold lifecycle ----------------------------------------------

    async def place_hold(self, decision_id: str, *, now: datetime | None = None) -> Hold:
        """Authorize the approved order: funds reserved, not taken."""
        moment = now or datetime.now(timezone.utc)
        hold = self.store.get(decision_id)
        if hold.state is not HoldState.AWAITING_BUYER:
            raise GatewayError(f"{decision_id} is {hold.state.value}, not awaiting the buyer")
        if self.paypal is None or not hold.paypal_order_id:
            raise GatewayError("no PayPal order to authorize")
        try:
            auth: Authorization = await self.paypal.authorize_order(hold.paypal_order_id)
        except PayPalError as exc:
            self.store.transition(
                decision_id, HoldState.FAILED, detail=str(exc), at=moment, last_error=str(exc)
            )
            raise GatewayError(f"authorize failed: {exc}") from exc
        return self.store.transition(
            decision_id,
            HoldState.HELD,
            detail=f"authorization {auth.authorization_id} ({auth.amount_value} {auth.currency})",
            at=moment,
            authorization_id=auth.authorization_id,
            authorization_expires_at=auth.expires_at,
        )

    async def capture(
        self,
        decision_id: str,
        *,
        amount: Money | None = None,
        reason: str = "delivery confirmed",
        now: datetime | None = None,
    ) -> Hold:
        """Take the money. Only ever called by the delivery oracle or a human."""
        moment = now or datetime.now(timezone.utc)
        hold = self.store.get(decision_id)
        if hold.state is not HoldState.HELD:
            raise GatewayError(f"{decision_id} is {hold.state.value}, not held")
        if self.paypal is None or not hold.authorization_id:
            raise GatewayError("no authorization to capture")
        taking = amount or hold.amount
        if taking > hold.amount:
            raise GatewayError(f"cannot capture {taking}; only {hold.amount} is held")
        try:
            capture = await self.paypal.capture_authorization(
                hold.authorization_id,
                currency=taking.currency,
                value=taking.to_paypal(),
                final_capture=True,
            )
        except PayPalError as exc:
            self.store.transition(
                decision_id, HoldState.FAILED, detail=str(exc), at=moment, last_error=str(exc)
            )
            raise GatewayError(f"capture failed: {exc}") from exc
        return self.store.transition(
            decision_id,
            HoldState.CAPTURED,
            detail=f"{reason}; captured {taking}",
            at=moment,
            capture_id=capture.capture_id,
            captured_minor=taking.minor,
        )

    async def void(
        self, decision_id: str, *, reason: str, now: datetime | None = None
    ) -> Hold:
        """Release the hold. Cheaper than a refund and leaves nothing to reverse."""
        moment = now or datetime.now(timezone.utc)
        hold = self.store.get(decision_id)
        if hold.state is not HoldState.HELD:
            raise GatewayError(f"{decision_id} is {hold.state.value}, not held")
        if self.paypal is None or not hold.authorization_id:
            raise GatewayError("no authorization to void")
        try:
            await self.paypal.void_authorization(hold.authorization_id)
        except PayPalError as exc:
            self.store.transition(
                decision_id, HoldState.FAILED, detail=str(exc), at=moment, last_error=str(exc)
            )
            raise GatewayError(f"void failed: {exc}") from exc
        return self.store.transition(
            decision_id, HoldState.VOIDED, detail=reason, at=moment
        )

    # -- reads -----------------------------------------------------------

    # -- inbound from PayPal ---------------------------------------------

    async def ingest_webhook(
        self,
        *,
        headers: dict[str, str],
        raw_body: bytes,
        event: dict[str, object],
        now: datetime | None = None,
    ) -> WebhookOutcome:
        """Verify a delivery, then let it update one hold. In that order.

        The order is the whole security property, and it is worth being explicit
        about what each step refuses:

          * **No webhook id configured** -> rejected. A gateway that cannot verify
            must not accept, and the tempting alternative -- accept when
            unconfigured, "just for local development" -- is how an unverified path
            reaches production.
          * **Signature invalid** -> rejected, and recorded as rejected. This is the
            one endpoint a stranger can post JSON to.
          * **Event id already seen** -> acknowledged and ignored. PayPal retries,
            and a retried capture applied twice reads as two captures.

        Dedupe happens *after* verification on purpose. Remembering an event id
        before knowing the delivery is authentic would let anyone who can guess an
        id make the real delivery look like a replay.
        """
        moment = now or datetime.now(timezone.utc)
        if self.paypal is None or not self.webhook_id:
            raise GatewayError(
                "this gateway cannot verify webhooks: PAYPAL_WEBHOOK_ID and PayPal "
                "credentials are both required. Refusing to accept unverified events."
            )

        try:
            genuine = await self.paypal.verify_webhook(
                headers=headers, raw_body=raw_body, webhook_id=self.webhook_id
            )
        except PayPalError as exc:
            # Could not reach the verifier. Not "assume genuine" and not "assume
            # forged": a 503 from here asks PayPal to redeliver, which is correct.
            raise GatewayError(f"could not verify this delivery with PayPal: {exc}") from exc

        if not genuine:
            raise WebhookRejected("signature verification failed")

        event_id = event_id_of(event)  # type: ignore[arg-type]
        if not event_id:
            raise WebhookRejected("a verified event with no id cannot be deduplicated")
        if not self.store.remember_webhook(
            event_id, str(event.get("event_type") or ""), at=moment
        ):
            return WebhookOutcome(
                event_id,
                str(event.get("event_type") or ""),
                "duplicate",
                detail="already processed; PayPal retried this delivery",
            )

        if event.get("event_type") == "CHECKOUT.ORDER.APPROVED":
            return await self._buyer_approved(event, at=moment)  # type: ignore[arg-type]

        return apply_webhook(self.store, event, at=moment)  # type: ignore[arg-type]

    async def _buyer_approved(
        self, event: dict[str, object], *, at: datetime
    ) -> WebhookOutcome:
        """The buyer said yes at PayPal. Reserve the funds.

        Handled apart from the transition table because the answer is an outbound
        call rather than a recorded fact, and an action does not belong in a lookup
        table. Nothing new is decided here: the policy already allowed this exact
        basket, and the order it is authorizing is one this gateway created.
        """
        event_type = "CHECKOUT.ORDER.APPROVED"
        event_id = event_id_of(event)  # type: ignore[arg-type]
        hold = locate_hold(self.store, event)  # type: ignore[arg-type]
        if hold is None:
            return WebhookOutcome(
                event_id, event_type, "no_matching_hold", detail="no hold matches this order"
            )
        if hold.state is not HoldState.AWAITING_BUYER:
            return WebhookOutcome(
                event_id,
                event_type,
                "already_in_state" if hold.state is HoldState.HELD else "not_awaiting_buyer",
                decision_id=hold.decision_id,
                from_state=hold.state.value,
                to_state=hold.state.value,
            )
        if not self.auto_place:
            return WebhookOutcome(
                event_id,
                event_type,
                "noted",
                decision_id=hold.decision_id,
                from_state=hold.state.value,
                to_state=hold.state.value,
                detail="auto_place is off; an operator must place this hold",
            )
        try:
            placed = await self.place_hold(hold.decision_id, now=at)
        except (GatewayError, PayPalError) as exc:
            return WebhookOutcome(
                event_id,
                event_type,
                "place_failed",
                decision_id=hold.decision_id,
                from_state=hold.state.value,
                to_state=self.store.get(hold.decision_id).state.value,
                detail=str(exc),
            )
        return WebhookOutcome(
            event_id,
            event_type,
            "applied",
            decision_id=placed.decision_id,
            from_state=HoldState.AWAITING_BUYER.value,
            to_state=placed.state.value,
            detail="buyer approved at PayPal; funds reserved",
        )

    def budget(self, *, now: datetime | None = None) -> dict[str, object]:
        """What the agent has left, per envelope. Safe for an agent to read: it
        reports the policy's own figures and reveals nothing an attacker could
        not infer by trying."""
        moment = now or datetime.now(timezone.utc)
        window = self.store.ledger_window(since=moment - self._window_span())
        out = []
        for envelope in self.policy.envelopes:
            spent = Money.zero(envelope.cap.currency)
            for entry in window.since(moment - envelope.duration):
                if entry.reserved and entry.amount.currency == envelope.cap.currency:
                    spent = spent + entry.amount
            out.append(
                {
                    "window": envelope.label,
                    "cap": envelope.cap.to_paypal(),
                    "committed": spent.to_paypal(),
                    "remaining": (envelope.cap - spent).to_paypal(),
                    "currency": envelope.cap.currency,
                }
            )
        placements = len(window.since(moment - self.policy.velocity_window))
        return {
            "policy_id": self.policy.policy_id,
            "currency": self.policy.currency,
            "unattended_threshold": self.policy.approval_threshold.to_paypal(),
            "hard_cap": self.policy.hard_per_transaction_cap.to_paypal(),
            "envelopes": out,
            "velocity": {"used": placements, "limit": self.policy.velocity_limit},
        }

    def open_holds(self) -> list[Hold]:
        return self.store.list(
            states=frozenset(
                {HoldState.AWAITING_HUMAN, HoldState.AWAITING_BUYER, HoldState.HELD}
            )
        )

    def approval_link_for(self, token: str) -> str:
        return f"{self.public_url}/approve/{token}"

    def _record_for(self, decision_id: str) -> DecisionRecord:
        for record in self.ledger:
            if record.decision_id == decision_id:
                record.verify(self.ledger.key)
                return record
        raise UnknownHold(decision_id)

    def _window_span(self) -> timedelta:
        """Long enough that the widest envelope sees all of its own history."""
        widest = max(
            [e.duration for e in self.policy.envelopes]
            + [self.policy.velocity_window, self.policy.duplicate_window],
            default=timedelta(days=1),
        )
        return widest + WINDOW_MARGIN


def _paypal_items(quote: MerchantQuote) -> list[dict[str, object]]:
    """PayPal's `items` array.

    Names are truncated to PayPal's 127-character limit and descriptions are
    dropped entirely. There is no reason to forward a product description to a
    payment processor, and this is the one place where a long injected string
    would otherwise leave the system.
    """
    return [
        {
            "name": item.sku if len(item.sku) <= 127 else item.sku[:127],
            "description": item.category.value,
            "quantity": str(item.quantity),
            "unit_amount": {
                "currency_code": item.unit_price.currency,
                "value": item.unit_price.to_paypal(),
            },
            "category": "DIGITAL_GOODS" if item.category.value in {
                "software_subscription",
                "compute",
                "gift_card",
            } else "PHYSICAL_GOODS",
        }
        for item in quote.line_items
    ]
