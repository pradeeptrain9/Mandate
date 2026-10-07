"""The settlement controls on the admin page, and the one way they can go wrong.

Every button here already has a tested endpoint behind it. The new failure mode is
different and invisible to those tests: the page decides which buttons to show
from its own copy of the state machine, and a copy drifts. Add a state to
state.py, or change which transitions are legal, and the page keeps offering what
used to be allowed -- a 409 the operator has to read an error message to discover,
on a panel whose whole purpose is to not need curl.

So the test is not "does the button work". It is "does the page's idea of what is
legal still match state.py". Parsed out of the page rather than duplicated here,
because a second hand-written copy of the table would be the third thing to drift.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mandate.gateway.state import TRANSITIONS, HoldState

ADMIN = Path(__file__).resolve().parents[1] / "src/mandate/gateway/static/admin.html"

#: What each action asks PayPal for, and therefore which transition it performs.
TARGET = {
    "place": HoldState.HELD,
    "capture": HoldState.CAPTURED,
    "void": HoldState.VOIDED,
    "refund": HoldState.REFUNDED,
}


def _object(name: str) -> dict:
    """Pull a flat `const NAME = {...}` out of the page as JSON."""
    page = ADMIN.read_text(encoding="utf-8")
    match = re.search(rf"const {name} = (\{{.*?\n\}});", page, re.DOTALL)
    assert match, f"{name} is not in admin.html any more; this test needs updating"
    body = match.group(1)
    body = re.sub(r"'([^']*)'", r'"\1"', body)          # single to double quotes
    body = re.sub(r"(\w+):", r'"\1":', body)            # bare keys
    body = re.sub(r",(\s*[}\]])", r"\1", body)          # trailing commas
    return json.loads(body)


@pytest.fixture(scope="module")
def settle() -> dict:
    return _object("SETTLE")


# -- the page cannot offer an illegal transition ----------------------------


def test_every_action_the_page_offers_is_legal_in_that_state(settle):
    for state_name, actions in settle.items():
        state = HoldState(state_name)
        for action in actions:
            target = TARGET[action]
            assert target in TRANSITIONS[state], (
                f"admin.html offers {action!r} on a {state_name} hold, but "
                f"{state_name} -> {target.value} is not a legal transition"
            )


def test_capture_is_offered_only_on_held(settle):
    """The irreversible one. It must not appear anywhere else."""
    offering = {s for s, actions in settle.items() if "capture" in actions}
    assert offering == {"held"}


def test_refund_is_offered_only_on_captured(settle):
    """Nothing to give back until something was taken."""
    offering = {s for s, actions in settle.items() if "refund" in actions}
    assert offering == {"captured"}


def test_no_terminal_state_is_given_buttons(settle):
    terminal = {s.value for s in HoldState if not TRANSITIONS[s]}
    assert not (set(settle) & terminal), (
        "a terminal hold has nothing left to do; offering an action on one is a 409"
    )


def test_every_state_with_money_at_stake_can_be_acted_on(settle):
    """The states an operator must be able to resolve from the page.

    `held` is money reserved and `captured` is money taken; both need a way out.
    `failed` is the one that would otherwise be stuck: something went wrong at
    PayPal, the hold may still carry an authorization, and the only legal moves
    are void and expire.
    """
    for state in ("held", "captured", "failed"):
        assert settle.get(state), f"{state} holds have no action on the admin page"


# -- the page is wired to the routes that exist -----------------------------


def test_the_actions_map_to_real_operator_routes(tmp_path):
    """A button pointing at a URL nobody serves is a 404 the operator discovers.

    Read from the OpenAPI schema rather than by walking `app.routes`: this FastAPI
    wraps each included router in an object with no `.path`, so walking the list
    sees the five routers and none of their routes.
    """
    from mandate.gateway.api import create_app
    from mandate.gateway.service import Gateway
    from mandate.gateway.store import Store
    from mandate.ledger.records import Ledger
    from mandate.policies import demo_policy

    from helpers import LEDGER_KEY, MERCHANT_SECRET

    store = Store(tmp_path / "state.db")
    try:
        app = create_app(
            Gateway(
                store=store,
                ledger=Ledger(tmp_path / "decisions.jsonl", LEDGER_KEY),
                policy=demo_policy(),
                merchant_secret=MERCHANT_SECRET,
            )
        )
        paths = set(app.openapi()["paths"])
    finally:
        store.close()

    for action in TARGET:
        assert f"/v1/ops/holds/{{decision_id}}/{action}" in paths
    assert "/v1/ops/sweep" in paths
    assert "/v1/ops/holds" in paths


def test_the_sweep_panel_offers_only_oracles_the_server_accepts():
    """`_oracle` raises 400 on anything else, which would be a dead dropdown."""
    page = ADMIN.read_text(encoding="utf-8")
    block = re.search(r'<select id="oracle">(.*?)</select>', page, re.DOTALL)
    assert block
    offered = set(re.findall(r'value="([^"]+)"', block.group(1)))
    assert offered == {"carrier", "never", "always"}


def test_a_capture_asks_before_it_takes_the_money():
    """The one irreversible action on the page, and a sweep that can perform it."""
    page = ADMIN.read_text(encoding="utf-8")
    capture = re.search(r"capture: \{.*?confirm:h =>(.*?)\},", page, re.DOTALL)
    assert capture, "the capture confirmation is gone"
    assert "irreversible" in capture.group(1)
    assert "confirm(" in page.split("async function runSweep()")[1][:900], (
        "a sweep can capture, so it has to be confirmed like one"
    )
