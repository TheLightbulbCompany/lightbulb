"""Lightbulb Launch Competition — waitlist, entry, leaderboard.

Every route 404s until ``settings.LAUNCH_DROP_AT`` is set (``require_launch``),
so a deploy with the env unset is inert. Public routes are rate-limited per
caller IP with the same in-process bucket the hook proxies use.
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from core.auth import AuthContext, get_current_user
from core.repositories import billing_repo, launch_entries_repo, launch_waitlist_repo
from core.services import launch_phase, launch_revenue, launch_service
from core.services.launch_phase import Schedule
from core.services.rate_bucket import RateBucket
from routers.hooks_proxy import _extract_caller_ip
from schemas.launch import (
    EnterRequest,
    LaunchInfo,
    LeaderboardResponse,
    ReferralAttributeRequest,
    WaitlistJoinRequest,
    WaitlistJoinResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()

PUBLIC_RATE_PER_MINUTE = 10
_RATE = RateBucket(PUBLIC_RATE_PER_MINUTE)

# The two public reads are unauthenticated full table scans. Cache-Control only
# binds a well-behaved client, so the shared answer is held server-side too.
INFO_TTL_S = 20.0
LEADERBOARD_TTL_S = 30.0
_RESP_CACHE: dict[str, tuple[float, dict]] = {}


async def _cached(key: str, ttl_s: float, factory: Callable[[], Awaitable[dict]]) -> dict:
    hit = _RESP_CACHE.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < ttl_s:
        return hit[1]
    value = await factory()
    _RESP_CACHE[key] = (now, value)
    return value


def require_launch() -> Schedule:
    s = launch_phase.schedule()
    if s is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return s


def _rate_limit(request: Request, surface: str) -> None:
    if not _RATE.allow(f"{surface}:{_extract_caller_ip(request)}"):
        raise HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": "60"})


@router.get("", response_model=LaunchInfo, operation_id="launch_info")
async def launch_info(response: Response, s: Schedule = Depends(require_launch)) -> dict:
    async def _build() -> dict:
        entrants = await launch_entries_repo.count()
        waitlist_count = await launch_waitlist_repo.count()
        return {
            "phase": launch_phase.phase(s),
            "drop_at": s.drop_at.isoformat(),
            "competition_start_at": s.competition_start_at.isoformat(),
            "competition_end_at": s.competition_end_at.isoformat(),
            "pot_cents": launch_phase.pot_cents(entrants),
            "entrants": entrants,
            "waitlist_count": waitlist_count,
            "comp_slots": s.comp_slots,
        }

    response.headers["Cache-Control"] = "public, max-age=30"
    return await _cached("info", INFO_TTL_S, _build)


@router.post("/waitlist", response_model=WaitlistJoinResponse, operation_id="launch_join_waitlist")
async def join_waitlist(body: WaitlistJoinRequest, request: Request, s: Schedule = Depends(require_launch)) -> dict:
    _rate_limit(request, "waitlist")
    if launch_phase.phase(s) != "waitlist":
        raise HTTPException(status_code=409, detail="waitlist_closed")
    try:
        email = launch_waitlist_repo.normalize_email(body.email)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_email")
    ref = (body.ref or "").strip().upper() or None
    row = await launch_waitlist_repo.join(email, referred_by=ref, source="waitlist")
    return {
        "position": row["position"],
        "referral_code": row["referral_code"],
        "in_comp_window": row["position"] <= s.comp_slots,
    }


@router.get("/me", operation_id="launch_me")
async def launch_me(auth: AuthContext = Depends(get_current_user), s: Schedule = Depends(require_launch)) -> dict:
    launch_revenue.maybe_revalidate()
    return await launch_service.me(auth, s)


@router.post("/referral", operation_id="launch_attribute_referral")
async def attribute_referral(
    body: ReferralAttributeRequest,
    auth: AuthContext = Depends(get_current_user),
    s: Schedule = Depends(require_launch),
) -> dict:
    """Attribute a post-drop sign-up who arrived via ``/?ref=``.
    Refused once the caller has a live subscription — attribution must
    precede the first payment, which is what pays the referrer."""
    from core.auth import resolve_owner_id

    email = await launch_service.resolve_email(auth)
    if not email:
        return {"attributed": False}
    billing = await billing_repo.get_by_owner_id(resolve_owner_id(auth))
    if (billing or {}).get("subscription_status") in launch_service.LIVE_FOR_ENTRY:
        return {"attributed": False}
    await launch_waitlist_repo.join(email, referred_by=None, source="signup")
    wrote = await launch_waitlist_repo.attribute(email, body.ref.strip().upper())
    return {"attributed": bool(wrote)}


@router.post("/enter", operation_id="launch_enter")
async def enter(
    body: EnterRequest,
    request: Request,
    auth: AuthContext = Depends(get_current_user),
    s: Schedule = Depends(require_launch),
) -> dict:
    _rate_limit(request, "enter")
    # One entry (and one stored key) per owner: an org member entering would
    # overwrite the org's row and its key with their own.
    if auth.is_org_context and not auth.is_org_admin:
        raise HTTPException(status_code=403, detail="org_admin_required")
    try:
        return await launch_service.enter(
            auth, s, display_name=body.display_name, stripe_key=body.stripe_key.strip(), public=body.public
        )
    except launch_service.EnterRefused as e:
        # The reason is a short enum; the key is never part of any response or log.
        raise HTTPException(status_code=400, detail=e.reason)


@router.get("/leaderboard", response_model=LeaderboardResponse, operation_id="launch_leaderboard")
async def leaderboard(response: Response, s: Schedule = Depends(require_launch)) -> dict:
    launch_revenue.maybe_revalidate()
    response.headers["Cache-Control"] = "public, max-age=60"
    return await _cached("leaderboard", LEADERBOARD_TTL_S, launch_service.leaderboard)
