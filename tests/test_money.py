"""Money is an integer. These tests exist because the alternative is a float."""

from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

from mandate.engine.money import (
    CurrencyMismatch,
    Money,
    UnsupportedCurrency,
    total,
)


def test_parses_paypal_decimal_strings():
    assert Money.from_paypal("49.99", "USD").minor == 4999
    assert Money.from_paypal("0.01", "USD").minor == 1
    assert Money.from_paypal("1200", "JPY").minor == 1200


def test_round_trips_through_paypal_format():
    for value in ("0.00", "0.07", "49.99", "1234.50", "99999.99"):
        assert Money.from_paypal(value, "USD").to_paypal() == value


def test_rejects_precision_the_currency_does_not_have():
    with pytest.raises(ValueError, match="more precision"):
        Money.from_paypal("1.999", "USD")
    with pytest.raises(ValueError, match="more precision"):
        Money.from_paypal("1200.5", "JPY")


def test_rejects_unknown_currency_rather_than_guessing_two_decimals():
    with pytest.raises(UnsupportedCurrency):
        Money(100, "XYZ")


def test_rejects_float_minor_units():
    with pytest.raises(TypeError):
        Money(10.5, "USD")  # type: ignore[arg-type]


def test_rejects_bool_because_bool_is_an_int():
    with pytest.raises(TypeError):
        Money(True, "USD")  # type: ignore[arg-type]


def test_refuses_negative_amounts():
    with pytest.raises(ValueError):
        Money(-1, "USD")


def test_cross_currency_arithmetic_is_a_type_error():
    with pytest.raises(CurrencyMismatch):
        Money(100, "USD") + Money(100, "EUR")
    with pytest.raises(CurrencyMismatch):
        Money(100, "USD") > Money(100, "EUR")


def test_empty_total_is_zero_not_a_guess():
    assert total([], "USD") == Money(0, "USD")


def test_scaling_requires_an_integer_quantity():
    with pytest.raises(TypeError):
        Money(100, "USD") * 1.5  # type: ignore[operator]


@given(st.integers(min_value=0, max_value=10**12))
def test_paypal_string_round_trip_is_lossless(minor: int):
    amount = Money(minor, "USD")
    assert Money.from_paypal(amount.to_paypal(), "USD") == amount


@given(
    st.lists(st.integers(min_value=0, max_value=10**9), min_size=0, max_size=50),
)
def test_total_never_loses_a_cent(values: list[int]):
    """The property a float would break: summation is exact."""
    amounts = [Money(v, "USD") for v in values]
    assert total(amounts, "USD").minor == sum(values)
