"""Did the thing arrive?

The question the whole hold mechanism exists to wait for, and the one place this
project has to be honest about not knowing the answer. So the contract is three
values rather than a boolean: **yes**, **not yet**, and **cannot tell**. A boolean
would collapse the last two, and they have opposite consequences -- "not yet" means
wait, "cannot tell" means do not take the money.

Pluggable because the real answer comes from somewhere different in every
deployment: PayPal's own shipment tracking, a carrier API, a merchant-signed
receipt, a human ticking a box in a warehouse. The gateway must not care which,
and nothing here is allowed to reach into a hold and change it -- an oracle
reports, the sweep decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class Delivered(StrEnum):
    YES = "yes"
    #: Expected, just not yet. Keep waiting.
    NOT_YET = "not_yet"
    #: Never going to arrive, as far as anyone can tell. Release the hold.
    NEVER = "never"
    #: The oracle could not answer: unreachable, no tracking, an error. Explicitly
    #: not the same as NEVER, because this one is about *our* ignorance, and the
    #: sweep treats it accordingly.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Delivery:
    status: Delivered
    detail: str = ""
    reference: str = ""

    @property
    def confirmed(self) -> bool:
        return self.status is Delivered.YES


class DeliveryOracle(Protocol):
    """One method, because one question.

    It takes the decision id and the merchant rather than a whole `Hold`: an oracle
    has no business reading a hold's amount or its state, and a narrow argument list
    is the cheapest way to make that true rather than merely intended.
    """

    name: str

    async def check(self, *, decision_id: str, merchant_id: str) -> Delivery: ...
