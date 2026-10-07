"""Talking to the merchant over HTTP, with a cold start accounted for.

Both services run on Render's free plan, which spins an instance down after
about 15 minutes of inactivity. The next request pays for the boot: measured at
**33.8 seconds** against the deployed merchant, against 0.3 seconds warm. Render's
edge sometimes holds that connection open and sometimes answers 502 while the
instance comes up, so a caller sees either a timeout or a 502 -- and a 20-second
client timeout turns a working deployment into "could not reach the shop", which
is indistinguishable from the merchant being down.

So this is not a generic retry helper, and it is deliberately narrow:

  * Only transport failures and 502/503/504 are retried. Those are the edge
    saying "not up yet". A 404 or a 422 is the merchant answering, and answering
    is not failing -- retrying a real answer would hide a bug and triple the
    load while doing it.
  * Only idempotent reads and the quote endpoint go through here. A quote is
    priced and signed fresh each time and reserves nothing, so asking twice is
    safe. Nothing in the hold lifecycle uses this: those calls go to PayPal with
    an idempotency key, which is a different mechanism for a different problem.
  * The error raised when every attempt fails says the instance may be asleep
    and how long was spent, because the first thing anyone does with "could not
    reach the shop" is check whether the shop is down.

On a paid plan none of this triggers: the first attempt succeeds and the retry
path is never entered.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Long enough to cover a measured 33.8s cold boot with room to spare, and still
#: short enough that a genuinely dead merchant is reported rather than waited on.
WAKE_TIMEOUT = 75.0

#: Statuses that mean "the instance is not up yet", not "the merchant says no".
WAKING = frozenset({502, 503, 504})

#: Pauses between attempts. The first retry is near-immediate because the boot is
#: already in progress by then; the second gives it longer.
BACKOFF: tuple[float, ...] = (1.0, 4.0)


class ShopUnreachable(RuntimeError):
    """Every attempt failed. Carries what to check first."""


async def request(
    method: str,
    url: str,
    *,
    json: Any | None = None,
    timeout: float = WAKE_TIMEOUT,
    backoff: tuple[float, ...] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """One merchant call, retried only while the instance is waking.

    Returns the response for anything the merchant actually answered, including
    4xx -- the caller decides what a 404 means. Raises `ShopUnreachable` only
    when no attempt got an answer at all.

    `backoff` is read here rather than bound as a default so a test can shorten
    it; otherwise asserting on three attempts would mean really waiting five
    seconds, and a slow test gets deleted or skipped.
    """
    backoff = BACKOFF if backoff is None else backoff
    attempts = len(backoff) + 1
    last = ""
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as http:
        for attempt in range(attempts):
            try:
                response = await http.request(method, url, json=json)
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code not in WAKING:
                    return response
                last = f"HTTP {response.status_code} from the edge"

            if attempt + 1 < attempts:
                pause = backoff[attempt]
                logger.info(
                    "merchant not answering yet (%s); retrying in %ss [%d/%d]",
                    last, pause, attempt + 2, attempts,
                )
                await asyncio.sleep(pause)

    raise ShopUnreachable(
        f"the shop did not answer after {attempts} attempts over up to "
        f"{timeout:.0f}s each ({last}). On Render's free plan an idle instance "
        f"takes about 35s to wake; if this persists the merchant service is down."
    )


async def wake(merchant_url: str) -> bool:
    """Ask the merchant for anything, so it is awake before someone shops.

    Both services spin down independently, and the usual sequence on the hosted
    demo is: a judge opens the gateway, the gateway wakes (~35s), they sign in and
    type what they want, and *then* the merchant starts its own 35-second boot.
    Firing this when the gateway starts overlaps the second wait with the first,
    so by the time anyone asks for a shortlist the shop is usually already up.

    Never raises and never blocks startup. A gateway that refuses to boot because
    a shop it only talks to on demand is asleep would be worse than the wait.
    """
    try:
        async with httpx.AsyncClient(timeout=WAKE_TIMEOUT) as http:
            response = await http.get(f"{merchant_url.rstrip('/')}/merchants")
    except httpx.HTTPError as exc:
        logger.info("could not wake the merchant at %s: %s", merchant_url, exc)
        return False
    logger.info("merchant at %s answered %s", merchant_url, response.status_code)
    return response.status_code < 400
