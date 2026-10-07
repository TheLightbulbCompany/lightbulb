"""Money reads — /api/v1/company/{rollup,payments,customers,products}.

Four read-only routes: the stored day rollup that Home and Spend render, and
three live pages of the owner's connected Stripe account. The Stripe three are
read through and store nothing — a page is a view, never a copy; the rollup
day row is the only sanctioned cache.

Gating copies routers/company_policy.py: org context requires org:admin, and
the owner is the ORG, never the member who clicked. Deliberately NOT
paid-gated — reading your own numbers must survive a lapsed subscription.

Four things the rollup row says that a naive passthrough would say wrongly:

1. ``followers`` and ``open_threads`` are hardcoded zeros in the writer
   (bundle.social is P3; no owner-scoped thread store exists). They are named
   in ``unavailable`` and omitted from ``days[]`` — a 0 would render a tile
   that lies, a null would make a client's sum NaN. The WRITER contract is
   unchanged: rollup_repo.put_day still requires every key.
2. ``visitors`` is rule 1 again, but conditionally. owner_posthog.day_traffic
   returns None on every miss and rollup_service stores 0 for it, so an owner
   who never connected PostHog gets a confident 0 for a quantity nobody
   measured. ``unavailable`` is therefore computed, not constant: no PostHog
   connection ⇒ visitors joins the omitted set. An owner WHO HAS connected
   keeps a genuine 0 — suppressing a real zero-traffic day is the opposite
   error.
3. ``agent_spend_microcents`` on a row is MONTH-TO-DATE, not that day's spend
   (rollup_service stamps the running month total onto each current-month
   row), so summing the column double-counts. It never appears in ``days[]``;
   one top-level scalar is served live from the usage row instead.
4. Rollup rows exist only for Stripe-bound owners (the hourly pass iterates
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
from core.repositories import billing_repo, integrations_repo, rollup_repo, usage_repo
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
# ``visitors`` joins them for an owner with no PostHog: see _measures_visitors.
VISITORS = "visitors"
# Money moves in two directions and the Spend tab now adds three sources up.
# Stated per field so no client has to read a name and guess the sign.
DIRECTION = {"revenue_microcents": "in", "agent_spend_month_to_date_microcents": "out"}


async def _measures_visitors(owner_id: str) -> bool:
    """Whether this owner has a PostHog connection at all — i.e. whether the
    stored ``visitors`` number is a measurement or a placeholder.

    owner_posthog.day_traffic returns None on every miss and rollup_service
    stores 0 for it (put_day requires every key, deliberately), so the row
    itself cannot tell the two apart. One GetItem on the owner's posthog
    integrations row can: it is written only after a successful OAuth apply and
    deleted on disconnect/wipe, and "connected" is the same predicate
    catalog_service already gates deploys on. It must stay a DDB read — asking
    PostHog itself would put a vendor round-trip on every rollup request.

    ponytail: connection-level, not per-day. A connected owner whose refresh
    token has since lapsed still reads 0 (or, more often, rollup_service's
    carried-forward prior). Distinguishing THAT needs the writer to record
    whether the day was measured, which is a writer-contract change.
    """
    row = await integrations_repo.get(owner_id, "posthog")
    return bool(row) and row.get("status", "connected") == "connected"


def _owner(auth: AuthContext) -> str:
    if auth.is_org_context:
        require_org_admin(auth)
    return resolve_owner_id(auth)


def parse_day(value: str, field: str) -> datetime:
    """Public: the provisioning-charges route parses its window the same way."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} must be YYYY-MM-DD") from None


def day_range(start: str | None, end: str | None) -> tuple[str, str]:
    """The window contract every Money read shares: ``YYYY-MM-DD`` UTC, ending
    today, 30 days by default, ascending, and capped — an uncapped span walks
    an unbounded number of DynamoDB pages (and, on the charges route, of Stripe
    pages)."""
    end_at = parse_day(end, "end") if end else datetime.now(timezone.utc)
    start_at = parse_day(start, "start") if start else end_at - timedelta(days=DEFAULT_SPAN_DAYS - 1)
    span = (end_at - start_at).days + 1
    if not 1 <= span <= MAX_SPAN_DAYS:
        raise HTTPException(status_code=422, detail=f"range must be 1-{MAX_SPAN_DAYS} days, ascending")
    return start_at.strftime("%Y-%m-%d"), end_at.strftime("%Y-%m-%d")


def day_after(day: str) -> str:
    """The exclusive end of an inclusive day range — the same shape Stripe's
    own ``{gte, lt}`` created filter uses."""
    return (parse_day(day, "end") + timedelta(days=1)).strftime("%Y-%m-%d")


@router.get("/rollup")
async def rollup(
    auth: Annotated[AuthContext, Depends(get_current_user)],
    start: str | None = None,
    end: str | None = None,
):
    """Stored day rows for the range, plus the honesty markers."""
    owner_id = _owner(auth)
    start_day, end_day = day_range(start, end)
    rows = await rollup_repo.get_days(owner_id, start_day, end_day)
    # One GetItem on the same row rollup_service reads, so it is always at
    # least as fresh as the stamp on a day row. The stored attribute is named
    # total_spend_microdollars but holds microCENTS (misnomer, no conversion).
    usage = await usage_repo.get_period_usage(owner_id, end_day[:7])
    billing = await billing_repo.get_by_owner_id(owner_id) or {}
    measured = await _measures_visitors(owner_id)
    day_keys = DAY_KEYS if measured else tuple(k for k in DAY_KEYS if k != VISITORS)

    return {
        "start": start_day,
        "end": end_day,
        "stripe_bound": bool(billing.get("company_stripe_account_id")),
        # Sparse: absent days are omitted, and this is the first one that
        # exists — the writers do today+yesterday only, never a backfill.
        "first_day": rows[0]["day"] if rows else None,
        "days": [{k: row[k] for k in day_keys} for row in rows],
        "agent_spend_month_to_date_microcents": (usage or {}).get("total_spend_microdollars", 0),
        "unavailable": UNAVAILABLE if measured else [*UNAVAILABLE, VISITORS],
        # Spend sums three sources (agent inference, provider charges, company
        # Stripe), so direction is stated rather than inferred from a name: a
        # client that nets money IN against money OUT is the failure mode.
        "direction": DIRECTION,
    }


async def _page(auth: AuthContext, kind: str, **kwargs) -> dict:
    owner_id = _owner(auth)
    days = [parse_day(kwargs[field], field) for field in ("start", "end") if kwargs.get(field)]
    # No span cap here — Stripe's own cursor bounds the read, and an open range
    # means "the latest N". The ordering rule is still day_range's: a reversed
    # range is a caller mistake, and answering it with an empty page hides that.
    if len(days) == 2 and days[0] > days[1]:
        raise HTTPException(status_code=422, detail="range must be ascending")
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
