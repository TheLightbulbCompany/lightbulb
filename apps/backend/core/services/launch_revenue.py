"""Read an entrant's OWN Stripe revenue through a pasted restricted key.

Why a restricted key and not Connect OAuth: Stripe grants `read_only` OAuth
scope to Extension-registered apps only, and ours is a Platform (marketplace
payouts). A restricted key with Balance:Read + Charges:Read is read-only by
construction. It is stored on the BYO-key Secrets Manager rail (only the
backend task role and the per-user task EXECUTION role can read that prefix;
a running agent cannot) and is never logged or echoed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import boto3
import stripe

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import launch_entries_repo
from core.secrets_manager import create_or_update_secret
from core.services import launch_phase
from core.services.launch_phase import Schedule

logger = logging.getLogger(__name__)

KEY_PATTERN_PROD = re.compile(r"^rk_live_[A-Za-z0-9]+$")
KEY_PATTERN_NONPROD = re.compile(r"^rk_(live|test)_[A-Za-z0-9]+$")
REVENUE_TYPES = frozenset({"charge", "payment"})
REFUND_TYPES = frozenset({"refund", "payment_refund"})
_PAGE = 100


class KeyRejected(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class MixedCurrency(ValueError):
    pass


def key_pattern() -> re.Pattern:
    return KEY_PATTERN_PROD if settings.ENVIRONMENT == "prod" else KEY_PATTERN_NONPROD


def _list_page(key: str, **kwargs):
    return stripe.BalanceTransaction.list(api_key=key, **kwargs)


def _map_stripe_error(e: Exception) -> KeyRejected:
    if isinstance(e, stripe.AuthenticationError):
        return KeyRejected("stripe_auth")
    if isinstance(e, stripe.PermissionError):
        return KeyRejected("stripe_permission")
    return KeyRejected("stripe_error")


async def validate_key(key: str) -> None:
    if not key_pattern().match(key or ""):
        raise KeyRejected("bad_prefix")
    try:
        await run_in_thread(_list_page, key, limit=1)
    except stripe.StripeError as e:
        raise _map_stripe_error(e) from None


async def sum_net_revenue(key: str, start: datetime, end: datetime) -> tuple[int, str]:
    """Gross charges minus refunds in [start, end), in the account's balance
    currency. Payouts, fees, transfers and everything else are not revenue."""
    cents = 0
    currency: str | None = None
    kwargs: dict = {"created": {"gte": int(start.timestamp()), "lt": int(end.timestamp())}, "limit": _PAGE}
    while True:
        try:
            page = await run_in_thread(_list_page, key, **kwargs)
        except stripe.StripeError as e:
            raise _map_stripe_error(e) from None
        for txn in page.data:
            if txn.type not in REVENUE_TYPES and txn.type not in REFUND_TYPES:
                continue
            if currency is None:
                currency = txn.currency
            elif txn.currency != currency:
                raise MixedCurrency(f"{currency} vs {txn.currency}")
            cents += int(txn.amount)
        if not page.has_more or not page.data:
            break
        kwargs["starting_after"] = page.data[-1].id
    return cents, currency or "usd"


# --- Secrets Manager (BYO-key rail) ---------------------------------------


def secret_name(owner_id: str) -> str:
    env = settings.ENVIRONMENT or "dev"
    return f"isol8/{env}/user-keys/{owner_id}/stripe-launch"


def _sm():
    return boto3.client("secretsmanager", region_name=getattr(settings, "AWS_REGION", "us-east-1"))


async def store_key(owner_id: str, key: str) -> None:
    sm = _sm()
    await run_in_thread(create_or_update_secret, sm, secret_name(owner_id), key)


async def load_key(owner_id: str) -> str | None:
    sm = _sm()
    try:
        resp = await run_in_thread(sm.get_secret_value, SecretId=secret_name(owner_id))
    except sm.exceptions.ResourceNotFoundException:
        return None
    return resp.get("SecretString")


async def delete_key(owner_id: str) -> None:
    sm = _sm()
    try:
        await run_in_thread(sm.delete_secret, SecretId=secret_name(owner_id), ForceDeleteWithoutRecovery=True)
    except sm.exceptions.ResourceNotFoundException:
        return


# --- Refresh ---------------------------------------------------------------

STALE_AFTER_S = 55 * 60
REVALIDATE_THROTTLE_S = 5 * 60
SWEEP_GRACE_AFTER_END = timedelta(hours=24)
_PER_OWNER_PAUSE_S = 0.2

_inflight: asyncio.Task | None = None
_last_started_at: datetime | None = None


async def refresh_entry(entry: dict, s: Schedule, now: datetime | None = None) -> None:
    owner_id = entry["owner_id"]
    now = now or datetime.now(timezone.utc)
    end = min(now, s.competition_end_at)
    try:
        key = await load_key(owner_id)
        if not key:
            await launch_entries_repo.set_refresh_error(owner_id, "missing_key")
            return
        window, currency = await sum_net_revenue(key, s.competition_start_at, end)
    except KeyRejected as e:
        await launch_entries_repo.set_refresh_error(owner_id, e.reason)
        return
    except MixedCurrency:
        await launch_entries_repo.set_refresh_error(owner_id, "mixed_currency")
        return
    except Exception:  # noqa: BLE001 — one bad entry must not stop the pass
        logger.warning("launch refresh: failed for %s", owner_id, exc_info=True)
        await launch_entries_repo.set_refresh_error(owner_id, "refresh_failed")
        return
    delta = window - int(entry.get("baseline_cents", 0))
    await launch_entries_repo.set_revenue(owner_id, window_cents=window, delta_cents=delta, currency=currency)


def _in_refresh_window(s: Schedule, now: datetime) -> bool:
    return s.competition_start_at <= now <= s.competition_end_at + SWEEP_GRACE_AFTER_END


def _is_stale(entry: dict, now: datetime) -> bool:
    raw = entry.get("refreshed_at")
    if not raw:
        return True
    return (now - launch_phase.parse_iso(raw)).total_seconds() > STALE_AFTER_S


async def refresh_all(*, force: bool = False, now: datetime | None = None) -> int:
    s = launch_phase.schedule()
    now = now or datetime.now(timezone.utc)
    if s is None or not _in_refresh_window(s, now):
        return 0
    entries = await launch_entries_repo.list_all()
    n = 0
    for entry in entries:
        if not force and not _is_stale(entry, now):
            continue
        await refresh_entry(entry, s, now=now)
        n += 1
        await asyncio.sleep(_PER_OWNER_PAUSE_S)
    return n


def maybe_revalidate(now: datetime | None = None) -> bool:
    """Stale-while-revalidate for the board: called on every /launch/me and
    /launch/leaderboard read. Returns the stored numbers to the caller (the
    caller doesn't await this) and schedules at most one background pass.
    ponytail: module-level in-flight/throttle state on the single backend
    task; a scheduled Lambda if the fleet ever runs more than one instance."""
    global _inflight, _last_started_at
    s = launch_phase.schedule()
    now = now or datetime.now(timezone.utc)
    if s is None or not _in_refresh_window(s, now):
        return False
    if _inflight is not None and not _inflight.done():
        return False
    if _last_started_at is not None and (now - _last_started_at).total_seconds() < REVALIDATE_THROTTLE_S:
        return False
    _last_started_at = now

    async def _run() -> None:
        try:
            n = await refresh_all(now=now)
            logger.info("launch refresh: %d entries", n)
        except Exception:  # noqa: BLE001
            logger.warning("launch refresh pass failed", exc_info=True)

    _inflight = asyncio.create_task(_run())
    return True
