"""Lightbulb Launch Competition — waitlist, join/connect, leaderboard.

Every route 404s until the launch-settings row is ``enabled`` with both dates
set for this env (``require_launch``, backed by
``core.services.launch_phase.schedule()``), so a deploy with the feature off
is inert. Public routes are rate-limited per caller IP with the same
in-process bucket the hook proxies use.
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from core.auth import AuthContext, get_current_user
from core.repositories import billing_repo, launch_waitlist_repo
from core.services import launch_phase, launch_revenue, launch_service
from core.services.launch_phase import Schedule
from core.services.rate_bucket import RateBucket
from routers.hooks_proxy import _extract_caller_ip
from schemas.launch import (
    ConnectStripeRequest,
    EntryPatchRequest,
    JoinRequest,
    LaunchInfo,
    LeaderboardResponse,
    ReferralAttributeRequest,
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


def clear_response_cache() -> None:
    """Invalidate the cached /launch and /launch/leaderboard responses. Called
    by launch_revenue after a background refresh pass lands new numbers, so a
    reader doesn't see a stale leaderboard for up to LEADERBOARD_TTL_S more."""
    _RESP_CACHE.clear()


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
    # NO pot fields here, or in any other public payload — the pot is
    # admin-only (schemas.launch.LaunchInfo is the enforced shape).
    async def _build() -> dict:
        return {
            "phase": launch_phase.phase(s),
            "sheet_closes_at": s.sheet_closes_at.isoformat(),
            "competition_start_at": s.competition_start_at.isoformat(),
            "competition_end_at": s.competition_end_at.isoformat(),
            "signups": await launch_waitlist_repo.count(),
            "comp_slots": s.comp_slots,
            "referral_jump": launch_phase.constants()["referral_jump"],
        }

    response.headers["Cache-Control"] = "public, max-age=30"
    return await _cached("info", INFO_TTL_S, _build)


@router.get("/me", operation_id="launch_me")
async def launch_me(auth: AuthContext = Depends(get_current_user), s: Schedule = Depends(require_launch)) -> dict:
    launch_revenue.maybe_revalidate()
    return await launch_service.me(auth, s)


@router.post("/referral", operation_id="launch_attribute_referral")
async def attribute_referral(
    body: ReferralAttributeRequest,
    request: Request,
    auth: AuthContext = Depends(get_current_user),
    s: Schedule = Depends(require_launch),
) -> dict:
    """Attribute a post-drop sign-up who arrived via ``/?ref=``.
    Refused once the caller has a live subscription — attribution must
    precede the first payment, which is what pays the referrer. Rate-limited:
    it writes to the waitlist table with a caller-supplied code, and the
    `{attributed}` response confirms code existence — an unlimited caller
    could enumerate referral codes."""
    _rate_limit(request, "referral")
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


@router.post("/join", operation_id="launch_join")
async def join(
    body: JoinRequest,
    request: Request,
    auth: AuthContext = Depends(get_current_user),
    s: Schedule = Depends(require_launch),
) -> dict:
    _rate_limit(request, "join")
    # One entry per owner: an org member joining would create the org's row
    # under their own identity instead of the org's.
    if auth.is_org_context and not auth.is_org_admin:
        raise HTTPException(status_code=403, detail="org_admin_required")
    try:
        return await launch_service.join(auth, s, display_name=body.display_name, public=body.public)
    except launch_service.LaunchRefused as e:
        raise HTTPException(status_code=400, detail=e.reason)


@router.post("/connect-stripe", operation_id="launch_connect_stripe")
async def connect_stripe(
    body: ConnectStripeRequest,
    request: Request,
    auth: AuthContext = Depends(get_current_user),
    s: Schedule = Depends(require_launch),
) -> dict:
    _rate_limit(request, "connect_stripe")
    # One stored key per owner: an org member connecting would overwrite the
    # org's key with their own.
    if auth.is_org_context and not auth.is_org_admin:
        raise HTTPException(status_code=403, detail="org_admin_required")
    try:
        return await launch_service.connect_stripe(auth, s, stripe_key=body.stripe_key.strip())
    except launch_service.LaunchRefused as e:
        # The reason is a short enum; the key is never part of any response or log.
        raise HTTPException(status_code=400, detail=e.reason)


@router.patch("/entry", operation_id="launch_set_public")
async def set_public(
    body: EntryPatchRequest,
    request: Request,
    auth: AuthContext = Depends(get_current_user),
    s: Schedule = Depends(require_launch),
) -> dict:
    _rate_limit(request, "entry")
    # Same one-entry-per-owner rule as /join and /connect-stripe: an org
    # member flipping this would touch the org's row under their own identity.
    if auth.is_org_context and not auth.is_org_admin:
        raise HTTPException(status_code=403, detail="org_admin_required")
    try:
        return await launch_service.set_public(auth, s, public=body.public)
    except launch_service.LaunchRefused as e:
        raise HTTPException(status_code=400, detail=e.reason)


@router.get("/leaderboard", response_model=LeaderboardResponse, operation_id="launch_leaderboard")
async def leaderboard(response: Response, s: Schedule = Depends(require_launch)) -> dict:
    launch_revenue.maybe_revalidate()
    response.headers["Cache-Control"] = "public, max-age=60"
    return await _cached("leaderboard", LEADERBOARD_TTL_S, launch_service.leaderboard)
