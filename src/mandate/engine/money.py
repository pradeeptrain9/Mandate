"""Amounts of money, as integers.

Every amount in this codebase is an integer count of a currency's minor unit --
cents for USD, paise for INR -- paired with its currency code. There is no
float anywhere on the path between an agent's request and a PayPal
authorization, because 0.1 + 0.2 != 0.3 and a budget ceiling that is off by a
hundredth is a ceiling that can be walked through.

The currency is part of the value rather than context the caller is trusted to
remember. Adding USD to INR is a type error here, not a silent wrong number
that surfaces three rules later as a ceiling that did not bind.

PayPal's REST APIs speak decimal strings ("49.99") with a currency-dependent
number of decimal places, so `from_paypal` and `to_paypal` are the only places
that parse or render one. Both go through `Decimal`, never `float`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# Minor units per major unit, for the currencies this build accepts. PayPal
# supports more; a code that is not listed is refused rather than guessed at,
# because guessing two decimals for a zero-decimal currency inflates every
# amount by a hundredfold in the safe-looking direction.
#
# JPY, KRW, VND and TWD are PayPal's zero-decimal currencies. HUF and TWD are
# also restricted to in-country transactions, which this build does not model.
MINOR_UNITS: dict[str, int] = {
    "USD": 100,
    "EUR": 100,
    "GBP": 100,
    "CAD": 100,
    "AUD": 100,
    "INR": 100,
    "JPY": 1,
}


class CurrencyMismatch(TypeError):
    """Two amounts in different currencies were combined."""


class UnsupportedCurrency(ValueError):
    """A currency code this build has no minor-unit scale for."""


def _exponent(currency: str) -> int:
    try:
        scale = MINOR_UNITS[currency]
    except KeyError:
        raise UnsupportedCurrency(f"no minor-unit scale for {currency!r}") from None
    return 0 if scale == 1 else 2


@dataclass(frozen=True, order=False)
class Money:
    """A non-negative amount. `minor` is cents, paise, yen -- never a float."""

    minor: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.minor, int) or isinstance(self.minor, bool):
            raise TypeError(f"minor must be an int, got {type(self.minor).__name__}")
        if self.minor < 0:
            raise ValueError("Money cannot be negative; model refunds as a direction, not a sign")
        _exponent(self.currency)  # raises UnsupportedCurrency for unknown codes

    # -- construction ----------------------------------------------------

    @classmethod
    def from_paypal(cls, value: str, currency: str) -> Money:
        """Parse a PayPal decimal string. Rejects anything finer than the currency allows."""
        exponent = _exponent(currency)
        try:
            amount = Decimal(value)
        except InvalidOperation:
            raise ValueError(f"not a decimal amount: {value!r}") from None
        if amount != amount.quantize(Decimal(1).scaleb(-exponent)):
            raise ValueError(
                f"{value!r} has more precision than {currency} permits ({exponent} dp)"
            )
        return cls(int(amount.scaleb(exponent)), currency)

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(0, currency)

    # -- rendering -------------------------------------------------------

    def to_paypal(self) -> str:
        """The decimal string PayPal's `amount.value` field expects."""
        exponent = _exponent(self.currency)
        return str(Decimal(self.minor).scaleb(-exponent).quantize(Decimal(1).scaleb(-exponent)))

    def __str__(self) -> str:
        return f"{self.to_paypal()} {self.currency}"

    # -- arithmetic ------------------------------------------------------

    def _same(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(f"cannot combine {self.currency} and {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.minor - other.minor, self.currency)

    def __mul__(self, count: int) -> Money:
        if not isinstance(count, int) or isinstance(count, bool):
            raise TypeError("Money scales by an integer quantity only")
        if count < 0:
            raise ValueError("quantity cannot be negative")
        return Money(self.minor * count, self.currency)

    __rmul__ = __mul__

    # -- comparison ------------------------------------------------------

    def __lt__(self, other: Money) -> bool:
        self._same(other)
        return self.minor < other.minor

    def __le__(self, other: Money) -> bool:
        self._same(other)
        return self.minor <= other.minor

    def __gt__(self, other: Money) -> bool:
        self._same(other)
        return self.minor > other.minor

    def __ge__(self, other: Money) -> bool:
        self._same(other)
        return self.minor >= other.minor


def total(amounts: list[Money], currency: str) -> Money:
    """Sum amounts that must all be in `currency`. Empty sums to zero, not to a guess."""
    out = Money.zero(currency)
    for amount in amounts:
        out = out + amount
    return out
