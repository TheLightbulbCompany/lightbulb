"""Lightbulb Launch Competition — waitlist, entry, leaderboard.

Every route 404s until ``settings.LAUNCH_DROP_AT`` is set (``require_launch``),
so a deploy with the env unset is inert. Public routes are rate-limited per
caller IP with the same in-process bucket the hook proxies use.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from core.auth import AuthContext, get_current_user  # noqa: F401
from core.repositories import launch_entries_repo, launch_waitlist_repo
from core.services import launch_phase
from core.services.launch_phase import Schedule
from core.services.rate_bucket import RateBucket
from routers.hooks_proxy import _extract_caller_ip
from schemas.launch import (
    EnterRequest,  # noqa: F401
    LaunchInfo,
    LeaderboardResponse,  # noqa: F401
    ReferralAttributeRequest,  # noqa: F401
    WaitlistJoinRequest,
    WaitlistJoinResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()

PUBLIC_RATE_PER_MINUTE = 10
_RATE = RateBucket(PUBLIC_RATE_PER_MINUTE)


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
    entrants = await launch_entries_repo.count()
    waitlist_count = await launch_waitlist_repo.count()
    response.headers["Cache-Control"] = "public, max-age=30"
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
