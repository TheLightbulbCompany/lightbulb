"""Lightbulb Launch Competition — the sign-up sheet, referrals, and the board.

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
from routers._public_ingress import _extract_caller_ip
from schemas.launch import (
    ConnectStripeRequest,
    DisplayNamePatchRequest,
    EntryPatchRequest,
    JoinRequest,
    LaunchInfo,
    LeaderboardResponse,
    ReferralAttributeRequest,
    TeamInterestRequest,
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
    """Explicit competition-sheet join — the only door onto the sheet.
    Refused with 409 {"reason": "closed"} once the sheet is no longer open.
    Idempotent: joining twice returns the existing membership.

    No onboarding gate: an owner can join while their container is still
    provisioning, which is the whole point of the sign-up modal (see
    ``launch_service.join_sheet``). ``not_onboarded`` is kept in the 409 set
    below purely so a client running against an older backend still maps the
    refusal to copy rather than a bare 400."""
    _rate_limit(request, "join")
    # Org contexts can't join AT ALL: the sheet row keys to the personal
    # email/user_id while org billing keys by org_id, so every money leg
    # (the close's paying lookup, the coupon attach, milestone grants,
    # referral counting) would silently miss an org actor. Personal
    # competition only. Attribution and /launch/me stay readable for them.
    if auth.is_org_context:
        raise HTTPException(status_code=409, detail={"reason": "org_not_supported"})
    try:
        return await launch_service.join_sheet(auth, s, display_name=body.display_name, public=body.public)
    except launch_service.LaunchRefused as e:
        if e.reason in ("not_onboarded", "closed"):
            raise HTTPException(status_code=409, detail={"reason": e.reason})
        raise HTTPException(status_code=400, detail=e.reason)


@router.post("/team-interest", operation_id="launch_team_interest")
async def team_interest(
    body: TeamInterestRequest,
    request: Request,
    auth: AuthContext = Depends(get_current_user),
    s: Schedule = Depends(require_launch),
) -> dict:
    """A team raises its hand — the founders enter it BY HAND. This is not a
    join: it stores an interest row and emails the founders, and the org
    refusal on /join above stays exactly as it is. Auth is required (any
    signed-in caller, personal or org): it keeps the form off the spam list
    and it is what identifies the submitter. Idempotent per owner — a second
    submission edits the same row."""
    _rate_limit(request, "team_interest")
    # Same one-row-per-owner rule as /connect-stripe and PATCH /entry: the row
    # keys to resolve_owner_id (the ORG id in an org context), so a member
    # submitting would overwrite the org's row under their own identity.
    if auth.is_org_context and not auth.is_org_admin:
        raise HTTPException(status_code=403, detail="org_admin_required")
    return await launch_service.submit_team_interest(auth, team_name=body.team_name, seats=body.seats, note=body.note)


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


@router.patch("/display-name", operation_id="launch_rename")
async def rename_display_name(
    body: DisplayNamePatchRequest,
    request: Request,
    auth: AuthContext = Depends(get_current_user),
    s: Schedule = Depends(require_launch),
) -> dict:
    """Rename the caller on the sheet (and on the board entry if they have
    one). Exists because joining is no longer gated on onboarding, so the
    first name an owner types is no longer one they had Friday's help
    choosing — see ``launch_service.rename_display_name``."""
    _rate_limit(request, "display_name")
    # Same one-row-per-owner rule as /entry: an org member renaming would
    # touch the org's row under their own identity.
    if auth.is_org_context and not auth.is_org_admin:
        raise HTTPException(status_code=403, detail="org_admin_required")
    try:
        return await launch_service.rename_display_name(auth, s, display_name=body.display_name)
    except launch_service.LaunchRefused as e:
        raise HTTPException(status_code=400, detail=e.reason)


@router.get("/leaderboard", response_model=LeaderboardResponse, operation_id="launch_leaderboard")
async def leaderboard(response: Response, s: Schedule = Depends(require_launch)) -> dict:
    launch_revenue.maybe_revalidate()
    response.headers["Cache-Control"] = "public, max-age=60"
    return await _cached("leaderboard", LEADERBOARD_TTL_S, launch_service.leaderboard)
