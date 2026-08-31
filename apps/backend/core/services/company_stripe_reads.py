"""Read-only day metrics from the owner's connected Stripe account.

v1 Connect reads with the platform key + ``stripe_account`` per call (the
Provisioning ``/v2`` client in ``stripe_provisioning.py`` is a different rail —
do not route these through it). Amounts from Stripe are integer cents;
everything we return is microcents (×10_000) to match the rollup rows.

Errors are logged and swallowed to ``None`` — a rollup miss is a stale tile,
never a 500.
"""

import asyncio
import logging
from datetime import datetime, timezone

import stripe

from core.config import settings
from core.repositories import billing_repo

logger = logging.getLogger(__name__)

# ponytail: 1000 signups/day cap on pagination — a fleet owner past that needs
# a count endpoint or search aggregation, not a longer walk.
_SIGNUP_CAP = 1000


def _client() -> stripe.StripeClient:
    """Module-level seam so tests patch one symbol (same pattern as
    ``stripe_provisioning._client``)."""
    return stripe.StripeClient(settings.STRIPE_SECRET_KEY)


def _day_bounds(day: str) -> dict[str, int]:
    y, m, d = (int(p) for p in day.split("-"))
    gte = int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())
    return {"gte": gte, "lt": gte + 86400}


def _read_day(account_id: str, day: str) -> dict:
    """Blocking SDK reads — call via ``asyncio.to_thread``."""
    created = _day_bounds(day)
    options = {"stripe_account": account_id}
    client = _client()

    revenue_cents = 0
    txns = client.v1.balance_transactions.list(
        params={"created": created, "type": "charge", "limit": 100},
        options=options,
    )
    for txn in txns.auto_paging_iter():
        revenue_cents += int(txn["net"])

    signups = 0
    customers = client.v1.customers.list(
        params={"created": created, "limit": 100},
        options=options,
    )
    for _ in customers.auto_paging_iter():
        signups += 1
        if signups >= _SIGNUP_CAP:
            break

    return {"revenue_microcents": revenue_cents * 10_000, "signups": signups}


async def day_money(owner_id: str, day: str) -> dict | None:
    """``{"revenue_microcents": int, "signups": int}`` for the owner's
    connected account on ``day`` (UTC); ``None`` when no company account is
    bound or the read fails."""
    row = await billing_repo.get_by_owner_id(owner_id)
    account_id = (row or {}).get("company_stripe_account_id")
    if not account_id:
        return None
    try:
        return await asyncio.to_thread(_read_day, account_id, day)
    except Exception:
        logger.exception("day_money read failed owner=%s day=%s", owner_id, day)
        return None
