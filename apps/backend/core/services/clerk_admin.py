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


async def get_organization(org_id: str) -> dict | None:
    """Single Clerk organization by org_id. Returns None on 404 / no key.

    Mirrors get_user's defensive posture: catches network errors, logs,
    returns None. Callers should handle None as "no metadata available"
    rather than as failure.
    """
    if not settings.CLERK_SECRET_KEY:
        return None

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(f"{_CLERK_API_BASE}/organizations/{org_id}", headers=headers)
    except Exception as e:  # noqa: BLE001
        logger.warning("clerk_admin.get_organization network error: %s", e)
        return None

    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        logger.warning("clerk_admin.get_organization HTTP %s", response.status_code)
        return None
    return response.json()


async def list_user_organizations(user_id: str, *, limit: int = 25) -> list[dict]:
    """Return the orgs a Clerk user belongs to.

    Each element: {"id": "org_...", "slug": "...", "name": "...", "role": "org:admin" | "org:member" | ...}
    The role field comes from the membership, not the org itself.
    Returns [] if the user is in no orgs, if the Clerk key is unset, or on
    network / HTTP error (logged as warning).

    Uses Clerk Backend API GET /v1/users/{user_id}/organization_memberships,
    which returns an envelope {data: [...memberships], total_count: N}. Each
    membership has shape {id, role, organization: {id, slug, name, ...}, ...}.
    """
    if not settings.CLERK_SECRET_KEY:
        return []

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    params = {"limit": min(limit, 100)}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(
                f"{_CLERK_API_BASE}/users/{user_id}/organization_memberships",
                headers=headers,
                params=params,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("clerk_admin.list_user_organizations network error: %s", e)
        return []

    if response.status_code == 404:
        return []
    if response.status_code >= 400:
        logger.warning("clerk_admin.list_user_organizations HTTP %s", response.status_code)
        return []

    try:
        payload = response.json()
    except ValueError:
        return []

    # Clerk v1 wraps collection responses in {data, total_count}. Older/alt
    # shapes may return a bare list — accept both defensively.
    memberships = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(memberships, list):
        return []

    result: list[dict] = []
    for m in memberships:
        if not isinstance(m, dict):
            continue
        org = m.get("organization") or {}
        org_id = org.get("id")
        if not org_id:
            continue
        result.append(
            {
                "id": org_id,
                "slug": org.get("slug") or "",
                "name": org.get("name") or "",
                "role": m.get("role") or "",
            }
        )
    return result


async def find_user_by_email(email: str) -> dict | None:
    """Return the first Clerk user matching `email`, or None.

    Used by the invite-creation gate to detect "is this email already a
    Clerk user?" before forwarding to Clerk's createInvitation API.
    Stubs to None when CLERK_SECRET_KEY is unset.
    """
    if not settings.CLERK_SECRET_KEY:
        return None

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    params = {"email_address": email, "limit": 1}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(f"{_CLERK_API_BASE}/users", headers=headers, params=params)
    except Exception as e:  # noqa: BLE001
        logger.warning("clerk_admin.find_user_by_email network error: %s", e)
        return None

    if response.status_code >= 400:
        logger.warning(
            "clerk_admin.find_user_by_email HTTP %s for %s",
            response.status_code,
            email,
        )
        return None

    users = response.json() or []
    return users[0] if users else None


async def create_organization_invitation(
    *,
    org_id: str,
    email: str,
    role: str,
    inviter_user_id: str,
) -> dict:
    """Create a Clerk org invitation. Returns the invitation dict on success.

    Raises HTTPException via the caller for non-201 responses — we want
    Clerk's error to surface to the org admin, not a silent no-op.
    `role` is a Clerk role key (e.g. "org:admin", "org:member").
    """
    if not settings.CLERK_SECRET_KEY:
        # Local-dev stub: return a fake invitation so the test path works.
        return {"id": f"orginv_stub_{email}", "stubbed": True}

    headers = {
        "Authorization": f"Bearer {settings.CLERK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "email_address": email,
        "role": role,
        "inviter_user_id": inviter_user_id,
    }

    # Lazy import keeps `fastapi` out of this module's import graph except
    # on the error paths that genuinely need it.
    from fastapi import HTTPException

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.post(
                f"{_CLERK_API_BASE}/organizations/{org_id}/invitations",
                headers=headers,
                json=body,
            )
    except httpx.TransportError as e:
        # Transport-level failure — covers all of httpx's transport
        # subclasses: TimeoutException, NetworkError (DNS, connection
        # reset, read errors), ProtocolError (malformed request/response
        # framing), ProxyError, UnsupportedProtocol. Catching the parent
        # class instead of an enumerated tuple prevents future httpx
        # additions from leaking out as bare 500s.
        #
        # Convert to a 503 HTTPException so:
        #   1. The orgs router's `except HTTPException` catch fires the
        #      `orgs.invitation.failed` metric (observability).
        #   2. The org admin sees "Clerk API unavailable" instead of a
        #      bare 500 from the framework.
        logger.warning(
            "clerk_admin.create_organization_invitation transport error org=%s email=%s err=%s",
            org_id,
            email,
            e,
        )
        raise HTTPException(status_code=503, detail="Clerk API unavailable") from e

    if response.status_code >= 400:
        logger.warning(
            "clerk_admin.create_organization_invitation HTTP %s org=%s email=%s body=%s",
            response.status_code,
            org_id,
            email,
            response.text,
        )
        # Surface Clerk's error verbatim so the admin sees real causes
        # (duplicate invitation, invalid role, etc.). Caller wraps in
        # HTTPException with the same status code.
        raise HTTPException(status_code=response.status_code, detail=response.text)

    return response.json()


async def list_pending_invitations_for_user(user_id: str) -> list[dict]:
    """List a Clerk user's pending org invitations. Empty list if none.

    Used by Gate B (personal trial-checkout) to refuse a personal
    subscription when the caller has unaccepted org invitations.
    Stubs to [] when CLERK_SECRET_KEY is unset.
    """
    if not settings.CLERK_SECRET_KEY:
        return []

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    params = {"status": "pending", "limit": 100}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(
                f"{_CLERK_API_BASE}/users/{user_id}/organization_invitations",
                headers=headers,
                params=params,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("clerk_admin.list_pending_invitations_for_user network error: %s", e)
        return []

    if response.status_code >= 400:
        logger.warning(
            "clerk_admin.list_pending_invitations_for_user HTTP %s user=%s",
            response.status_code,
            user_id,
        )
        return []

    payload = response.json()
    # Clerk paginated responses have shape {data: [...], total_count: int}
    # for invitation listings — extract data list when present.
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"] or []
    return payload if isinstance(payload, list) else []
