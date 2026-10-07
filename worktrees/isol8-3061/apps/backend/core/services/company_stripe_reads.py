"""Read-only day metrics from the owner's company Stripe account.

Plain v1 reads (the Provisioning ``/v2`` client in ``stripe_provisioning.py``
is a different rail — do not route these through it). The account is
addressed one of two ways, chosen by the row's ``company_stripe_source``:

- ``connected`` / ``created`` — a Connect relationship: the PLATFORM key with
  ``stripe_account`` redirecting the call at the owner's account.
- ``api_key`` — the owner's OWN restricted key, held backend-side: their key,
  no header. Same calls, same shapes; only the authentication differs. This is
  the path that works for an account that IS the platform (ours) and for any
  owner who would rather hand over a read-scoped key than a Connect grant.

Amounts from Stripe are integer cents; everything we return is microcents
(×10_000) to match the rollup rows.

Errors are logged and swallowed to ``None`` in ``day_money`` — a rollup miss is
a stale tile, never a 500. The ``stripe_page`` list reads below do the opposite
and let ``StripeError`` propagate (the router maps it to 502): a list that
renders "no payments" during a Stripe outage is a lie, not a stale tile.
"""

import asyncio
import logging
from datetime import datetime, timezone

import stripe

from core.config import settings
from core.repositories import billing_repo
from core.services import company_stripe

logger = logging.getLogger(__name__)

# ponytail: 1000 signups/day cap on pagination — a fleet owner past that needs
# a count endpoint or search aggregation, not a longer walk.
_SIGNUP_CAP = 1000


def _client(api_key: str | None = None) -> stripe.StripeClient:
    """Module-level seam so tests patch one symbol (same pattern as
    ``stripe_provisioning._client``). ``api_key`` is the owner's own key on
    the ``api_key`` path; the platform key otherwise."""
    return stripe.StripeClient(api_key or settings.STRIPE_SECRET_KEY)


async def _addressing(row: dict) -> tuple[str | None, dict]:
    """``(api_key, options)`` for a bound row — the ONE place the two paths
    diverge. Connect rows: platform key + ``stripe_account``. ``api_key``
    rows: their key, no header. Returns ``(None, {})`` when the key is gone
    (secret deleted out from under the row) so callers read "unbound"."""
    account_id = row.get("company_stripe_account_id")
    if not account_id:
        return None, {}
    if row.get("company_stripe_source") == "api_key":
        key = await company_stripe.load_api_key(row["owner_id"])
        return (key, {}) if key else (None, {})
    return None, {"stripe_account": account_id}


def _day_bounds(day: str) -> dict[str, int]:
    y, m, d = (int(p) for p in day.split("-"))
    gte = int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())
    return {"gte": gte, "lt": gte + 86400}


def _read_day(api_key: str | None, options: dict, day: str) -> dict:
    """Blocking SDK reads — call via ``asyncio.to_thread``."""
    created = _day_bounds(day)
    client = _client(api_key)

    revenue_cents = 0
    # Every net movement into the balance, not only charges: a "charge"-only
    # filter never subtracts refunds, disputes or adjustments, so what it
    # produced was gross, not earned.
    txns = client.v1.balance_transactions.list(
        params={"created": created, "limit": 100},
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
    bound or the Stripe read fails (the billing_repo read raises through)."""
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    if not row.get("company_stripe_account_id"):
        return None
    try:
        api_key, options = await _addressing(row)
        if api_key is None and not options:
            return None
        return await asyncio.to_thread(_read_day, api_key, options, day)
    except Exception:
        logger.exception("day_money read failed owner=%s day=%s", owner_id, day)
        return None


# --- Money tab list reads --------------------------------------------------
# Read through, store nothing: a page is a view, never a copy. The rollup day
# row is the only sanctioned cache.
#
# Cursor pagination passes Stripe's own ``starting_after`` through instead of
# walking pages here — ``auto_paging_iter`` is right for a bounded day
# aggregate and wrong for a page the owner is scrolling.

_MAX_PAGE = 100

# kind -> (client.v1 resource, fields returned). Projection keeps the wire
# payload to what the Money tabs render; the full Stripe object is one
# dashboard click away.
_KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "payments": (
        "charges",
        (
            "id",
            "amount",
            "amount_refunded",
            "currency",
            "created",
            "status",
            "paid",
            "refunded",
            "disputed",
            "description",
            "receipt_url",
            "customer",
        ),
    ),
    "customers": ("customers", ("id", "email", "name", "created", "currency", "delinquent", "balance")),
    "products": ("products", ("id", "name", "active", "description", "created", "default_price")),
}


def _created_range(start: str | None, end: str | None) -> dict[str, int]:
    """Stripe's ``{gte, lt}`` filter over whole UTC days, end inclusive."""
    created: dict[str, int] = {}
    if start:
        created["gte"] = _day_bounds(start)["gte"]
    if end:
        created["lt"] = _day_bounds(end)["lt"]
    return created


def _read_page(api_key: str | None, options: dict, kind: str, params: dict) -> dict:
    """Blocking SDK read — call via ``asyncio.to_thread``."""
    resource, fields = _KINDS[kind]
    result = getattr(_client(api_key).v1, resource).list(params=params, options=options)
    data = [{f: item.get(f) for f in fields} for item in result.get("data", [])]
    has_more = bool(result.get("has_more"))
    return {
        "data": data,
        "has_more": has_more,
        "next_cursor": data[-1]["id"] if has_more and data else None,
    }


async def stripe_page(
    owner_id: str,
    kind: str,
    *,
    limit: int = 25,
    starting_after: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """One page of the owner's connected-account ``kind``.

    An owner with no connected account gets an empty page, never an error:
    most of the fleet is unbound and a 404/409 would turn every Money tab red.
    The onboarding flags ride along because a bound-but-unonboarded account
    returns legitimately empty lists — "finish Stripe setup" is a different
    empty state from "nothing here yet".
    """
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    account_id = row.get("company_stripe_account_id")
    page = {
        "stripe_bound": bool(account_id),
        "data": [],
        "has_more": False,
        "next_cursor": None,
        # Same keys company_stripe._status_payload publishes.
        "details_submitted": bool(row.get("company_stripe_details_submitted")),
        "charges_enabled": bool(row.get("company_stripe_charges_enabled")),
        "payouts_enabled": bool(row.get("company_stripe_payouts_enabled")),
    }
    if not account_id:
        return page
    api_key, options = await _addressing(row)
    if api_key is None and not options:
        # Bound row whose key is gone — the honest state is "not connected".
        page["stripe_bound"] = False
        return page

    params: dict = {"limit": min(limit, _MAX_PAGE)}
    if starting_after:
        params["starting_after"] = starting_after
    if kind == "payments":
        created = _created_range(start, end)
        if created:
            params["created"] = created
    elif kind == "products":
        params["active"] = True
        params["expand"] = ["data.default_price"]

    return page | await asyncio.to_thread(_read_page, api_key, options, kind, params)
