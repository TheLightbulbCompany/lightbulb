"""Money reads — /api/v1/company/{rollup,payments,customers,products}.

Four read-only routes: the stored day rollup that Home and Spend render, and
three live pages of the owner's connected Stripe account. The Stripe three are
read through and store nothing — a page is a view, never a copy; the rollup
day row is the only sanctioned cache.

Gating copies routers/company_policy.py: org context requires org:admin, and
the owner is the ORG, never the member who clicked. Deliberately NOT
paid-gated — reading your own numbers must survive a lapsed subscription.

Three things the rollup row says that a naive passthrough would say wrongly:

1. ``followers`` and ``open_threads`` are hardcoded zeros in the writer
   (bundle.social is P3; no owner-scoped thread store exists). They are named
   in ``unavailable`` and omitted from ``days[]`` — a 0 would render a tile
   that lies, a null would make a client's sum NaN. The WRITER contract is
   unchanged: rollup_repo.put_day still requires every key.
2. ``agent_spend_microcents`` on a row is MONTH-TO-DATE, not that day's spend
   (rollup_service stamps the running month total onto each current-month
   row), so summing the column double-counts. It never appears in ``days[]``;
   one top-level scalar is served live from the usage row instead.
3. Rollup rows exist only for Stripe-bound owners (the hourly pass iterates
   connected accounts), so an unbound owner's empty ``days`` means "no
   source", not "no revenue" — ``stripe_bound`` lets the UI say which, and
   ``first_day`` lets it label a range the writers never backfilled.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.repositories import billing_repo, rollup_repo, usage_repo
from core.services import company_stripe_reads
from routers.company_stripe import _stripe_failure

logger = logging.getLogger(__name__)
router = APIRouter()

# rollup_repo.get_days pages on LastEvaluatedKey until exhausted, so the span
# is the only thing bounding the read.
MAX_SPAN_DAYS = 366
DEFAULT_SPAN_DAYS = 30
DAY_KEYS = ("day", "revenue_microcents", "signups", "visitors")
UNAVAILABLE = ["followers", "open_threads"]


def _owner(auth: AuthContext) -> str:
    if auth.is_org_context:
        require_org_admin(auth)
    return resolve_owner_id(auth)


def _day(value: str, field: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} must be YYYY-MM-DD") from None


@router.get("/rollup")
async def rollup(
    auth: Annotated[AuthContext, Depends(get_current_user)],
    start: str | None = None,
    end: str | None = None,
):
    """Stored day rows for the range, plus the two honesty markers."""
    owner_id = _owner(auth)
    end_at = _day(end, "end") if end else datetime.now(timezone.utc)
    start_at = _day(start, "start") if start else end_at - timedelta(days=DEFAULT_SPAN_DAYS - 1)
    span = (end_at - start_at).days + 1
    if not 1 <= span <= MAX_SPAN_DAYS:
        raise HTTPException(status_code=422, detail=f"range must be 1-{MAX_SPAN_DAYS} days, ascending")

    start_day, end_day = start_at.strftime("%Y-%m-%d"), end_at.strftime("%Y-%m-%d")
    rows = await rollup_repo.get_days(owner_id, start_day, end_day)
    # One GetItem on the same row rollup_service reads, so it is always at
    # least as fresh as the stamp on a day row. The stored attribute is named
    # total_spend_microdollars but holds microCENTS (misnomer, no conversion).
    usage = await usage_repo.get_period_usage(owner_id, end_at.strftime("%Y-%m"))
    billing = await billing_repo.get_by_owner_id(owner_id) or {}

    return {
        "start": start_day,
        "end": end_day,
        "stripe_bound": bool(billing.get("company_stripe_account_id")),
        # Sparse: absent days are omitted, and this is the first one that
        # exists — the writers do today+yesterday only, never a backfill.
        "first_day": rows[0]["day"] if rows else None,
        "days": [{k: row[k] for k in DAY_KEYS} for row in rows],
        "agent_spend_month_to_date_microcents": (usage or {}).get("total_spend_microdollars", 0),
        "unavailable": UNAVAILABLE,
    }


async def _page(auth: AuthContext, kind: str, **kwargs) -> dict:
    owner_id = _owner(auth)
    for field in ("start", "end"):
        if kwargs.get(field):
            _day(kwargs[field], field)
    try:
        return await company_stripe_reads.stripe_page(owner_id, kind, **kwargs)
    except stripe.error.StripeError as e:
        # Never an empty list: "no payments" during an outage is a lie.
        raise _stripe_failure(e) from e


@router.get("/payments")
async def payments(
    auth: Annotated[AuthContext, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    starting_after: str | None = None,
    start: str | None = None,
    end: str | None = None,
):
    """Charges — they carry receipt_url, refunded and amount_refunded, which
    PaymentIntents do not. The customer is returned as an id, not expanded."""
    return await _page(auth, "payments", limit=limit, starting_after=starting_after, start=start, end=end)


@router.get("/customers")
async def customers(
    auth: Annotated[AuthContext, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    starting_after: str | None = None,
):
    return await _page(auth, "customers", limit=limit, starting_after=starting_after)


@router.get("/products")
async def products(
    auth: Annotated[AuthContext, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    starting_after: str | None = None,
):
    """Active products with their default price expanded — one call."""
    return await _page(auth, "products", limit=limit, starting_after=starting_after)
