"""Launch Competition — per-user composition and the entry flow.

`me()` is the one place waitlist + billing + onboarding facts are combined
for a caller. `enter()` (Task 8) validates the pasted Stripe restricted key,
stores it on the BYO-key rail, computes the baseline and writes the entry.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from core.auth import AuthContext, resolve_owner_id
from core.config import settings
from core.repositories import billing_repo, launch_entries_repo, launch_waitlist_repo, user_repo
from core.services import readiness_state
from core.services.launch_phase import Schedule, phase

logger = logging.getLogger(__name__)

# trial_local can't enter: the $10 buy-in rides a subscription (paid or comped
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


def can_enter(
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
    ok, reason = can_enter(s=s, billing=billing, onboarding=onboarding)
    return {
        "phase": phase(s),
        "waitlist": waitlist,
        "activated": onboarding.get("bootstrap_state") == "delivered",
        "activated_at": onboarding.get("bootstrap_delivered_at"),
        "entry": _public_entry(entry),
        "can_enter": ok,
        "reason": reason,
    }
