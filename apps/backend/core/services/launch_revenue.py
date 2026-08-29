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
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import launch_entries_repo
from core.secrets_manager import create_or_update_secret
from core.services import key_service, launch_phase
from core.services.launch_phase import Schedule

logger = logging.getLogger(__name__)

KEY_PATTERN_PROD = re.compile(r"^rk_live_[A-Za-z0-9]+$")
KEY_PATTERN_NONPROD = re.compile(r"^rk_(live|test)_[A-Za-z0-9]+$")
# The baseline window is the 30 days before the competition starts (spec).
BASELINE_DAYS = 30
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
    return key_service._secret_name(owner_id, "stripe-launch")


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


async def _record(owner_id: str, coro) -> None:
    """Repo writes in the refresh path must never abort the pass — a row deleted
    mid-pass or a throttled write just leaves that owner stale until the next read."""
    try:
        await coro
    except Exception:  # noqa: BLE001
        logger.warning("launch refresh: write failed for %s", owner_id, exc_info=True)


def _baseline_is_provisional(entry: dict, s: Schedule) -> bool:
    """An entrant who enters during the activation window has a baseline summed
    over a window whose tail is still in the future — the last days contribute $0.
    ``refresh_entry`` only runs at/after ``competition_start_at``, so the first
    pass is the earliest moment the real number exists."""
    raw = entry.get("baseline_at")
    if not raw:
        return True
    return launch_phase.parse_iso(raw) < s.competition_start_at


def prorated_baseline(baseline_cents: int, s: Schedule, end: datetime) -> int:
    """The baseline covers a full 30 days; the live window covers only the days
    elapsed so far. Comparing them whole ranks the board by baseline SIZE until
    the last day — a big incumbent sits bottom no matter how fast it grows — so
    the baseline is scaled to the elapsed fraction of the competition."""
    total = (s.competition_end_at - s.competition_start_at).total_seconds()
    if total <= 0:
        return baseline_cents
    elapsed = min(max((end - s.competition_start_at).total_seconds(), 0.0), total)
    return round(baseline_cents * elapsed / total)


async def refresh_entry(entry: dict, s: Schedule, now: datetime | None = None) -> None:
    owner_id = entry["owner_id"]
    now = now or datetime.now(timezone.utc)
    end = min(now, s.competition_end_at)
    baseline_cents = int(entry.get("baseline_cents", 0))
    baseline_currency = entry.get("currency")
    try:
        key = await load_key(owner_id)
        if not key:
            await _record(owner_id, launch_entries_repo.set_refresh_error(owner_id, "missing_key"))
            return
        if _baseline_is_provisional(entry, s):
            baseline_cents, baseline_currency = await sum_net_revenue(
                key, s.competition_start_at - timedelta(days=BASELINE_DAYS), s.competition_start_at
            )
            await _record(
                owner_id,
                launch_entries_repo.set_key_connected(
                    owner_id,
                    baseline_cents=baseline_cents,
                    currency=baseline_currency,
                    baseline_at=utc_now_iso(),
                ),
            )
        window, currency = await sum_net_revenue(key, s.competition_start_at, end)
    except KeyRejected as e:
        await _record(owner_id, launch_entries_repo.set_refresh_error(owner_id, e.reason))
        return
    except MixedCurrency:
        await _record(owner_id, launch_entries_repo.set_refresh_error(owner_id, "mixed_currency"))
        return
    except Exception:  # noqa: BLE001 — one bad entry must not stop the pass
        logger.warning("launch refresh: failed for %s", owner_id, exc_info=True)
        await _record(owner_id, launch_entries_repo.set_refresh_error(owner_id, "refresh_failed"))
        return
    # sum_net_revenue only catches a mismatch WITHIN one call's own page walk;
    # a finalized baseline in one currency vs. this pass's window in another
    # (e.g. the entrant switched their Stripe account's default currency) isn't
    # caught there — check it here, before the numbers get compared/stored.
    if baseline_currency and baseline_currency != currency:
        await _record(owner_id, launch_entries_repo.set_refresh_error(owner_id, "mixed_currency"))
        return
    prorated = prorated_baseline(baseline_cents, s, end)
    delta = window - prorated
    await _record(
        owner_id,
        launch_entries_repo.set_revenue(
            owner_id,
            window_cents=window,
            delta_cents=delta,
            currency=currency,
            baseline_prorated_cents=prorated,
        ),
    )


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
        if not entry.get("has_key"):
            continue  # joined but never connected Stripe — no key to read revenue with
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
            return
        try:
            # Lazy import: routers.launch imports core.services.launch_revenue
            # at module load, so a top-level import here would be circular.
            from routers.launch import clear_response_cache

            clear_response_cache()
        except Exception:  # noqa: BLE001 — never let cache housekeeping break the sweep
            logger.warning("launch refresh: failed to clear response cache", exc_info=True)

    _inflight = asyncio.create_task(_run())
    return True
