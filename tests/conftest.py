"""Fixtures. Builders are in helpers.py."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mandate.policies import demo_policy


@pytest.fixture
def now() -> datetime:
    # Fixed instant. Every window in the policy is relative, so a frozen clock
    # makes the whole suite deterministic without any monkeypatching.
    return datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def policy():
    return demo_policy()
