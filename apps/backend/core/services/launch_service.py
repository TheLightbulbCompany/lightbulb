"""Launch Competition — per-user composition and the entry flow.

`me()` is the one place waitlist + billing + onboarding facts are combined
for a caller. Entry is two explicit steps: `join()` creates the entry row
once activation + a live subscription are in place, and `connect_stripe()`
(optional) validates a pasted Stripe restricted key, computes the baseline,
stores it on the BYO-key rail, and flips the entry onto the public board.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from core.auth import AuthContext, resolve_owner_id
from core.config import settings
from core.dynamodb import utc_now_iso
from core.repositories import billing_repo, launch_entries_repo, launch_waitlist_repo, user_repo
from core.services import launch_revenue, readiness_state
from core.services.launch_phase import Schedule, phase

logger = logging.getLogger(__name__)

# trial_local can't join: the $10 buy-in rides a subscription (paid or comped
# via the promo code), and a free-tier container has neither.
LIVE_FOR_ENTRY = frozenset({"active", "trialing"})


def referral_link(code: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/?ref={code}"


async def resolve_email(auth: AuthContext) -> str | None:
    if auth.email:
        return auth.email.strip().lower()
    row = await user_repo.get(auth.user_id)
    email = (row or {}).get("email")
    return email.strip().lower() if email else None


def can_join(
    *, s: Schedule, billing: dict | None, onboarding: dict, now: datetime | None = None
) -> tuple[bool, str | None]:
    p = phase(s, now or datetime.now(timezone.utc))
    if p == "waitlist":
        return False, "phase_waitlist"
    if p == "ended":
        return False, "phase_ended"
    if (billing or {}).get("subscription_status") not in LIVE_FOR_ENTRY:
        return False, "no_subscription"
    if onboarding.get("bootstrap_state") != "delivered":
        return False, "not_activated"
    return True, None


def _public_entry(entry: dict | None) -> dict | None:
    if not entry:
        return None
    return {
        "display_name": entry["display_name"],
        "public": bool(entry.get("public", True)),
        "entered_at": entry.get("entered_at"),
        "has_key": bool(entry.get("has_key", False)),
        "currency": entry.get("currency"),
        "baseline_cents": entry.get("baseline_cents", 0),
        "window_cents": entry.get("window_cents", 0),
        "delta_cents": entry.get("delta_cents", 0),
        "refreshed_at": entry.get("refreshed_at"),
        "refresh_error": entry.get("refresh_error"),
    }


async def me(auth: AuthContext, s: Schedule) -> dict:
    owner_id = resolve_owner_id(auth)
    email = await resolve_email(auth)
    waitlist: dict | None = None
    if email:
        row = await launch_waitlist_repo.join(email, referred_by=None, source="signup")
        await launch_waitlist_repo.claim_user(email, auth.user_id)
        waitlist = {
            "position": row["position"],
            "referral_code": row["referral_code"],
            "in_comp_window": row["position"] <= s.comp_slots,
            "referral_link": referral_link(row["referral_code"]),
            "referrals_signed_up": int(row.get("referrals_signed_up", 0)),
            "referrals_paid": int(row.get("referrals_paid", 0)),
            "free_months_banked": int(row.get("free_months_banked", 0)),
        }
    billing = await billing_repo.get_by_owner_id(owner_id)
    onboarding = await readiness_state.resolve_onboarding_state(owner_id)
    entry = await launch_entries_repo.get(owner_id)
    ok, reason = can_join(s=s, billing=billing, onboarding=onboarding)
    return {
        "phase": phase(s),
        "waitlist": waitlist,
        "activated": onboarding.get("bootstrap_state") == "delivered",
        "activated_at": onboarding.get("bootstrap_delivered_at"),
        "entry": _public_entry(entry),
        "can_enter": ok,
        "reason": reason,
    }


class LaunchRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def join(auth: AuthContext, s: Schedule, *, display_name: str, public: bool) -> dict:
    """Step 1: create the entry row. Requires activation + a live
    subscription; never touches Stripe. Idempotent — an owner who already has
    an entry gets that row back untouched (see launch_entries_repo.join)."""
    owner_id = resolve_owner_id(auth)
    billing = await billing_repo.get_by_owner_id(owner_id)
    onboarding = await readiness_state.resolve_onboarding_state(owner_id)
    ok, reason = can_join(s=s, billing=billing, onboarding=onboarding)
    if not ok:
        raise LaunchRefused(reason or "refused")
    position = None
    email = await resolve_email(auth)
    if email:
        row = await launch_waitlist_repo.get(email)
        position = int(row["position"]) if row else None
    entry = await launch_entries_repo.join(
        owner_id, display_name=display_name.strip(), public=public, waitlist_position=position
    )
    logger.info("launch: %s joined (position=%s)", owner_id, position)
    return _public_entry(entry)


async def connect_stripe(auth: AuthContext, s: Schedule, *, stripe_key: str) -> dict:
    """Step 2 (optional): validate the pasted restricted key, compute the
    baseline over [start-30d, start) — refined by the refresh path once the
    window has actually closed, see launch_revenue._baseline_is_provisional —
    store the key, then flip the entry onto the board. Requires an existing
    entry; replaces any previously-connected key."""
    owner_id = resolve_owner_id(auth)
    entry = await launch_entries_repo.get(owner_id)
    if not entry:
        raise LaunchRefused("no_entry")
    try:
        await launch_revenue.validate_key(stripe_key)
    except launch_revenue.KeyRejected as e:
        raise LaunchRefused(e.reason) from None
    baseline_end = s.competition_start_at
    baseline_start = baseline_end - timedelta(days=launch_revenue.BASELINE_DAYS)
    try:
        baseline_cents, currency = await launch_revenue.sum_net_revenue(stripe_key, baseline_start, baseline_end)
    except launch_revenue.MixedCurrency:
        raise LaunchRefused("mixed_currency") from None
    except launch_revenue.KeyRejected as e:
        raise LaunchRefused(e.reason) from None
    await launch_revenue.store_key(owner_id, stripe_key)
    await launch_entries_repo.set_key_connected(
        owner_id, currency=currency, baseline_cents=baseline_cents, baseline_at=utc_now_iso()
    )
    updated = await launch_entries_repo.get(owner_id)
    logger.info("launch: %s connected Stripe (baseline=%s %s)", owner_id, baseline_cents, currency)
    return _public_entry(updated)


def rank(entries: list[dict]) -> list[dict]:
    visible = [e for e in entries if e.get("has_key") and e.get("public", True)]
    visible.sort(key=lambda e: (-int(e.get("delta_cents", 0)), e.get("entered_at") or ""))
    rows = []
    for i, e in enumerate(visible, start=1):
        # The prorated baseline is what `delta_cents` was measured against; falling
        # back to the full baseline would understate growth mid-competition.
        prorated = int(e.get("baseline_prorated_cents") or 0)
        baseline = prorated if prorated > 0 else int(e.get("baseline_cents", 0))
        delta = int(e.get("delta_cents", 0))
        rows.append(
            {
                "rank": i,
                "display_name": e["display_name"],
                "delta_cents": delta,
                "pct": round(delta / baseline, 4) if baseline > 0 else None,
                "currency": e.get("currency") or "usd",
            }
        )
    return rows


async def leaderboard(limit: int = 50) -> dict:
    entries = await launch_entries_repo.list_all()
    on_board = sum(1 for e in entries if e.get("has_key"))
    return {"competing": len(entries), "on_board": on_board, "rows": rank(entries)[:limit]}
