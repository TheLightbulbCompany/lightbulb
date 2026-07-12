"""Shared over-cap guard for Teams BFF endpoints that dispatch a run NOW.

UX layer only: the authoritative stop is the adapter's pre-run allowance
check (every Paperclip run passes through it, whatever created it). These
guards exist so a click on Run/Retry/Check-now gets a clean 402 instead
of a failed-run card.
"""

from fastapi import HTTPException

from core.services import trial_cap


async def require_under_daily_cap(owner_id: str) -> None:
    if await trial_cap.is_over_daily_cap(owner_id):
        raise HTTPException(
            status_code=402,
            detail={
                "blocked": True,
                "code": "meta_daily_cap",
                "message": "You've used today's free allowance — resets tomorrow. Upgrade to keep going.",
            },
        )
