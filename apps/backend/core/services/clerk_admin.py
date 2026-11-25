"""Clerk Backend API client: deleting a person (Spark's DELETE /os/account).

Empty/missing CLERK_SECRET_KEY: a no-op, for local dev.
"""

import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)


_CLERK_API_BASE = "https://api.clerk.com/v1"
_TIMEOUT_S = 5.0


async def delete_user(user_id: str) -> None:
    """Permanently delete a Clerk user. RAISES on any unexpected failure; 404 (already deleted) is success,
    so a retry is safe. No-ops when CLERK_SECRET_KEY is unset."""
    if not settings.CLERK_SECRET_KEY:
        return

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.delete(f"{_CLERK_API_BASE}/users/{user_id}", headers=headers)

    if response.status_code == 404:
        return
    if response.status_code >= 400:
        raise RuntimeError(f"clerk_admin.delete_user failed: http_{response.status_code} body={response.text}")


class ClerkLookupError(RuntimeError):
    """Clerk user lookup failed without answering (no key, network, non-404).

    Distinct from "user does not exist": callers that gate a destructive or
    ownership-changing action on absence must fail CLOSED on this, and only
    treat a positive 404 as absence. ``get_user`` above collapses both to
    None for lenient read-side callers; use ``get_user_strict`` when the
    difference matters.
    """
