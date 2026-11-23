"""Clerk Backend API client for admin-side operations.

Read surface: list users, get one user. Mutation surface: ban / unban /
revoke sessions / resend verification, alongside the admin router
endpoints that need them.

billing.py and desktop_auth.py call api.clerk.com inline via httpx;
this module consolidates the admin-side calls so admin_service.py can
compose without duplicating httpx setup.

Empty/missing CLERK_SECRET_KEY → callers get a stubbed empty response
so local dev without real Clerk admin creds still renders the admin
UI (with no users listed).
"""

import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)


_CLERK_API_BASE = "https://api.clerk.com/v1"
_TIMEOUT_S = 5.0


async def list_users(*, query: str = "", limit: int = 50, offset: int = 0) -> dict:
    """Paginated list. Returns {users: list[dict], next_offset: int | None}.

    `query` is a Clerk search string (matches email, first/last name,
    user_id). `offset` is integer-paged (Clerk supports limit + offset).
    Stubs when CLERK_SECRET_KEY is unset.
    """
    if not settings.CLERK_SECRET_KEY:
        return {"users": [], "next_offset": None, "stubbed": True}

    params: dict = {"limit": min(limit, 500), "offset": offset, "order_by": "-created_at"}
    if query:
        params["query"] = query

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(f"{_CLERK_API_BASE}/users", headers=headers, params=params)
    except httpx.TimeoutException:
        return {"users": [], "next_offset": None, "error": "timeout"}
    except Exception as e:  # noqa: BLE001
        logger.warning("clerk_admin.list_users network error: %s", e)
        return {"users": [], "next_offset": None, "error": str(e)}

    if response.status_code >= 400:
        logger.warning("clerk_admin.list_users HTTP %s", response.status_code)
        return {"users": [], "next_offset": None, "error": f"http_{response.status_code}"}

    users = response.json()
    next_offset = offset + len(users) if len(users) >= limit else None
    return {"users": users, "next_offset": next_offset, "stubbed": False}


async def _post_no_body(path: str) -> dict:
    """Helper for fire-and-forget POST endpoints (ban/unban/revoke/resend).

    Returns the response JSON on success, or {error: ...} on failure.
    Stubs gracefully when CLERK_SECRET_KEY is unset (returns
    {stubbed: True}).
    """
    if not settings.CLERK_SECRET_KEY:
        return {"stubbed": True}

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.post(f"{_CLERK_API_BASE}{path}", headers=headers)
    except Exception as e:  # noqa: BLE001
        logger.warning("clerk_admin POST %s network error: %s", path, e)
        return {"error": str(e)}

    if response.status_code >= 400:
        logger.warning("clerk_admin POST %s HTTP %s", path, response.status_code)
        return {"error": f"http_{response.status_code}"}

    try:
        return response.json()
    except ValueError:
        return {"ok": True}


async def ban_user(user_id: str) -> dict:
    """Suspend a user — they cannot sign in until unbanned."""
    return await _post_no_body(f"/users/{user_id}/ban")


async def unban_user(user_id: str) -> dict:
    """Reactivate a previously-banned user."""
    return await _post_no_body(f"/users/{user_id}/unban")


async def revoke_sessions(user_id: str) -> dict:
    """Force-signout all of a user's active sessions."""
    return await _post_no_body(f"/users/{user_id}/revoke_session")


async def delete_user(user_id: str) -> None:
    """Permanently delete a Clerk user — Admin Account-Wipe teardown step 12
    (docs/superpowers/specs/2026-07-14-admin-account-wipe-design.md). This is
    the LAST step of the wipe: leaving the Clerk user alive lets everything
    else regrow on the next /container/status poll.

    Unlike this module's other mutations (which swallow failures into an
    {"error": ...} dict so read-side/UX callers degrade gracefully), this
    RAISES on any unexpected failure. The wipe orchestrator wraps every
    teardown step in its own try/except and records a per-step
    ok/failed result — silently swallowing here would hide a failed delete
    behind a fake "ok" step. Treats 404 (already deleted) as success, so a
    re-run after a partial wipe failure is safe. No-ops when
    CLERK_SECRET_KEY is unset, matching the module's local-dev stub
    posture elsewhere.
    """
    if not settings.CLERK_SECRET_KEY:
        return

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.delete(f"{_CLERK_API_BASE}/users/{user_id}", headers=headers)

    if response.status_code == 404:
        return
    if response.status_code >= 400:
        raise RuntimeError(f"clerk_admin.delete_user failed: http_{response.status_code} body={response.text}")


async def resend_verification(user_id: str) -> dict:
    """Resend the email-verification link to the user's primary email.

    Clerk requires the email_address_id rather than user_id for this op,
    so we fetch the user first to get their primary email's id, then
    issue the resend POST. If the user fetch fails, returns {error}.
    """
    user = await get_user(user_id)
    if not user:
        return {"error": "user_not_found"}

    primary_email_id = user.get("primary_email_address_id")
    if not primary_email_id:
        return {"error": "no_primary_email"}

    return await _post_no_body(f"/email_addresses/{primary_email_id}/verification")


async def get_user(user_id: str) -> dict | None:
    """Single user by Clerk user_id. Returns None on 404."""
    if not settings.CLERK_SECRET_KEY:
        return None

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(f"{_CLERK_API_BASE}/users/{user_id}", headers=headers)
    except Exception as e:  # noqa: BLE001
        logger.warning("clerk_admin.get_user network error: %s", e)
        return None

    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        logger.warning("clerk_admin.get_user HTTP %s", response.status_code)
        return None
    return response.json()


class ClerkLookupError(RuntimeError):
    """Clerk user lookup failed without answering (no key, network, non-404).

    Distinct from "user does not exist": callers that gate a destructive or
    ownership-changing action on absence must fail CLOSED on this, and only
    treat a positive 404 as absence. ``get_user`` above collapses both to
    None for lenient read-side callers; use ``get_user_strict`` when the
    difference matters.
    """


async def get_user_strict(user_id: str) -> dict | None:
    """Single user by Clerk user_id. Returns None ONLY on Clerk 404.

    Raises ``ClerkLookupError`` when CLERK_SECRET_KEY is unset, on network
    errors, or on non-404 HTTP errors — the caller cannot tell absence from
    a blip, so it must not act as if the user is gone.
    """
    if not settings.CLERK_SECRET_KEY:
        raise ClerkLookupError("CLERK_SECRET_KEY is unset — cannot verify user existence")

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(f"{_CLERK_API_BASE}/users/{user_id}", headers=headers)
    except Exception as e:  # noqa: BLE001
        raise ClerkLookupError(f"clerk_admin.get_user_strict network error: {e}") from e

    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise ClerkLookupError(f"clerk_admin.get_user_strict HTTP {response.status_code}")
    return response.json()


async def get_primary_email(user_id: str) -> str:
    """The user's primary email address, falling back to their first one.

    Raises ``RuntimeError`` when the user or any email is missing — callers
    (per-user Paperclip sign-in) need a hard failure, not a None.
    """
    user = await get_user(user_id)
    if not user:
        raise RuntimeError(f"clerk has no user with id {user_id}")
    primary_id = user.get("primary_email_address_id")
    for entry in user.get("email_addresses") or []:
        if entry.get("id") == primary_id:
            email = entry.get("email_address")
            if email:
                return email
    # Fall back to the first email if the primary id pointer is unset.
    addrs = user.get("email_addresses") or []
    if addrs and addrs[0].get("email_address"):
        return addrs[0]["email_address"]
    raise RuntimeError(f"clerk user {user_id} has no email address")


async def find_user_by_email(email: str, *, strict: bool = False) -> dict | None:
    """Return the first Clerk user matching `email`, or None.

    Used by the curator lookup to resolve a human-gated address to its
    Clerk user. Stubs to None when CLERK_SECRET_KEY is unset.
    Strict callers require one unambiguous result; configuration, transport,
    HTTP and malformed-directory failures raise so inbound mail can retry.
    """
    if not settings.CLERK_SECRET_KEY:
        if strict:
            raise RuntimeError("clerk_admin.find_user_by_email is not configured")
        return None

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    params = {"email_address": email, "limit": 2 if strict else 1}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(f"{_CLERK_API_BASE}/users", headers=headers, params=params)
    except Exception as e:  # noqa: BLE001
        if strict:
            raise
        logger.warning("clerk_admin.find_user_by_email network error: %s", e)
        return None

    if response.status_code >= 400:
        if strict:
            raise RuntimeError(f"clerk_admin.find_user_by_email failed: http_{response.status_code}")
        logger.warning(
            "clerk_admin.find_user_by_email HTTP %s for %s",
            response.status_code,
            email,
        )
        return None

    users = response.json()
    if strict and (not isinstance(users, list) or len(users) > 1):
        raise RuntimeError("clerk_admin.find_user_by_email ambiguous or malformed result")
    return users[0] if users else None


async def oauth_application_name(client_id: str) -> str | None:
    """The name an OAuth client registered under (a dynamically registered MCP client names
    itself, e.g. "Cursor"). None when Clerk is unreachable or the client is unknown."""
    if not settings.CLERK_SECRET_KEY:
        return None
    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    offset = 0
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            # ponytail: a linear scan of the instance's OAuth apps, cached per client id by the caller;
            # a by-client-id lookup when Clerk offers one.
            while True:
                response = await client.get(
                    f"{_CLERK_API_BASE}/oauth_applications", headers=headers, params={"limit": 100, "offset": offset}
                )
                if response.status_code >= 400:
                    return None
                page = response.json().get("data") or []
                for app in page:
                    if app.get("client_id") == client_id:
                        name = app.get("name")
                        return name.strip()[:40] if isinstance(name, str) and name.strip() else None
                if len(page) < 100:
                    return None
                offset += 100
    except Exception as e:  # noqa: BLE001
        logger.warning("clerk_admin.oauth_application_name error: %s", e)
        return None
