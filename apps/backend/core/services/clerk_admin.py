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


async def delete_organization(org_id: str) -> None:
    """Permanently delete a Clerk organization — workspace-wipe identity step.

    Mirrors ``delete_user``'s contract: RAISES on unexpected failure (the wipe
    orchestrator records per-step ok/failed itself), treats 404 (already
    deleted) as success so a re-run after a partial wipe is safe, and no-ops
    when ``CLERK_SECRET_KEY`` is unset (local-dev stub posture). Always runs
    LAST in a workspace wipe, after every member Clerk user is deleted.
    """
    if not settings.CLERK_SECRET_KEY:
        return

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        response = await client.delete(f"{_CLERK_API_BASE}/organizations/{org_id}", headers=headers)

    if response.status_code == 404:
        return
    if response.status_code >= 400:
        raise RuntimeError(f"clerk_admin.delete_organization failed: http_{response.status_code} body={response.text}")


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


async def get_organization(org_id: str, *, include_members_count: bool = False) -> dict | None:
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
            response = await client.get(
                f"{_CLERK_API_BASE}/organizations/{org_id}",
                headers=headers,
                params={"include_members_count": "true"} if include_members_count else None,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("clerk_admin.get_organization network error: %s", e)
        return None

    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        logger.warning("clerk_admin.get_organization HTTP %s", response.status_code)
        return None
    return response.json()


async def list_user_organizations(user_id: str, *, limit: int = 25, strict: bool = False) -> list[dict]:
    """Return the orgs a Clerk user belongs to.

    Each element: {"id": "org_...", "slug": "...", "name": "...", "role": "org:admin" | "org:member" | ...}
    The role field comes from the membership, not the org itself.
    Returns [] if the user is in no orgs, if the Clerk key is unset, or on
    network / HTTP error (logged as warning).

    Uses Clerk Backend API GET /v1/users/{user_id}/organization_memberships,
    which returns an envelope {data: [...memberships], total_count: N}. Each
    membership has shape {id, role, organization: {id, slug, name, ...}, ...}.

    ``strict=True`` RAISES on a network / HTTP error instead of returning [] —
    for callers (the workspace wipe) where "no other orgs" decides whether an
    account is deleted, so a Clerk blip must fail closed.
    """
    if not settings.CLERK_SECRET_KEY:
        if strict:
            raise RuntimeError("clerk_admin.list_user_organizations is not configured")
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
        if strict:
            raise
        logger.warning("clerk_admin.list_user_organizations network error: %s", e)
        return []

    if response.status_code == 404:
        return []
    if response.status_code >= 400:
        if strict:
            raise RuntimeError(f"clerk_admin.list_user_organizations failed: http_{response.status_code}")
        logger.warning("clerk_admin.list_user_organizations HTTP %s", response.status_code)
        return []

    try:
        payload = response.json()
    except ValueError:
        if strict:
            raise RuntimeError("clerk_admin.list_user_organizations malformed response") from None
        return []

    # Clerk v1 wraps collection responses in {data, total_count}. Older/alt
    # shapes may return a bare list — accept both defensively.
    memberships = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(memberships, list):
        if strict:
            raise RuntimeError("clerk_admin.list_user_organizations malformed memberships")
        return []
    total = payload.get("total_count") if isinstance(payload, dict) else None
    if strict and isinstance(total, int) and total > len(memberships):
        # ponytail: one page (≤100); a strict caller decides deletions from this
        # list, so a truncated one must fail closed. Paginate if a user ever
        # legitimately belongs to more than 100 workspaces.
        raise RuntimeError(f"clerk_admin.list_user_organizations truncated: {len(memberships)} of {total}")

    result: list[dict] = []
    for m in memberships:
        if not isinstance(m, dict):
            if strict:
                raise RuntimeError("clerk_admin.list_user_organizations malformed membership")
            continue
        org = m.get("organization") or {}
        org_id = org.get("id")
        if not org_id:
            if strict:
                raise RuntimeError("clerk_admin.list_user_organizations missing organization")
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


_ORG_MEMBERS_PAGE = 100


async def list_organization_members(org_id: str, *, strict: bool = False) -> list[dict]:
    """Return every member of a Clerk organization.

    Each element: {"user_id", "first_name", "last_name", "email", "image_url", "role"}
    — the membership's ``public_user_data`` flattened (``identifier`` is the
    member's primary email). Returns [] if the Clerk key is unset or on any
    network / HTTP error (logged as warning) — never a partial page, because a
    silently short roster is exactly the bug the picker had.

    Uses Clerk Backend API GET /v1/organizations/{org_id}/memberships, paged
    by limit/offset; the envelope is {data: [...memberships], total_count: N}.

    ``strict=True`` RAISES instead of returning [] on a network / HTTP error —
    the workspace wipe finds members only through this list, so a Clerk blip
    must fail closed rather than read as "no members".
    """
    if not settings.CLERK_SECRET_KEY:
        return []

    headers = {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
    members: list[dict] = []
    offset = 0
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            while True:
                response = await client.get(
                    f"{_CLERK_API_BASE}/organizations/{org_id}/memberships",
                    headers=headers,
                    params={"limit": _ORG_MEMBERS_PAGE, "offset": offset},
                )
                if response.status_code == 404:
                    # The org is gone: no members. A re-run after a wipe already
                    # deleted the Clerk org must not fail closed on that.
                    return []
                if response.status_code >= 400:
                    if strict:
                        raise RuntimeError(f"clerk_admin.list_organization_members failed: http_{response.status_code}")
                    logger.warning("clerk_admin.list_organization_members HTTP %s", response.status_code)
                    return []
                payload = response.json()
                rows = payload.get("data") if isinstance(payload, dict) else payload
                if not isinstance(rows, list):
                    # A malformed 200 is not "no members": strict callers (the workspace wipe,
                    # the last-member teardown) must fail closed on it like on any HTTP error.
                    if strict:
                        raise RuntimeError("clerk_admin.list_organization_members failed: malformed payload")
                    return []
                for m in rows:
                    pud = (m.get("public_user_data") or {}) if isinstance(m, dict) else {}
                    user_id = pud.get("user_id")
                    if not user_id:
                        continue
                    members.append(
                        {
                            "user_id": user_id,
                            "first_name": pud.get("first_name") or "",
                            "last_name": pud.get("last_name") or "",
                            "email": pud.get("identifier") or "",
                            "image_url": pud.get("image_url"),
                            "role": m.get("role") or "",
                        }
                    )
                if len(rows) < _ORG_MEMBERS_PAGE:
                    return members
                offset += _ORG_MEMBERS_PAGE
    except Exception as e:  # noqa: BLE001
        if strict:
            raise
        logger.warning("clerk_admin.list_organization_members network error: %s", e)
        return []


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


async def create_organization_membership(
    *,
    org_id: str,
    user_id: str,
    role: str = "org:member",
) -> dict:
    """Add an EXISTING Clerk user to an org directly — no email invitation.

    This is the Clerk call behind a workspace JOIN LINK: the redeemer is
    already signed in, so there is nobody to email and nothing to accept. It
    replaced ``create_organization_invitation`` on 2026-09-14, when the
    email-invite endpoint was removed with per-seat billing. Email invitations
    came BACK on 2026-10-08 (``create_organization_invitation`` below), next to
    the join link rather than instead of it: billing is per workspace now, so
    an invitation no longer models a seat, and "invite my cofounder by email"
    is where people look first. Invitees always join as ``org:member``.

    Returns ``{"status": "joined" | "already_member", "membership_id": str | None}``.
    Already-a-member is a SUCCESS: Clerk rejects the duplicate with a 4xx, and
    the join flow must be idempotent (the owner clicking their own link).

    Unlike the read helpers in this module, failures RAISE rather than degrade
    to an empty dict: a silently-swallowed failure here would tell someone they
    joined a workspace they cannot see. Clerk's own error body is logged but
    never surfaced — the caller is an arbitrary signed-in user, not an admin.
    """
    if not settings.CLERK_SECRET_KEY:
        # Local-dev stub, matching this module's posture elsewhere.
        return {"status": "joined", "membership_id": None, "stubbed": True}

    headers = {
        "Authorization": f"Bearer {settings.CLERK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    body = {"user_id": user_id, "role": role}

    # Lazy import keeps `fastapi` out of this module's import graph except on
    # the error paths that genuinely need it.
    from fastapi import HTTPException

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.post(
                f"{_CLERK_API_BASE}/organizations/{org_id}/memberships",
                headers=headers,
                json=body,
            )
    except httpx.TransportError as e:
        # Catch the transport PARENT class, not an enumerated tuple, so future
        # httpx additions can't leak out as bare 500s (same reasoning the
        # removed invitation helper carried).
        logger.warning(
            "clerk_admin.create_organization_membership transport error org=%s user=%s err=%s",
            org_id,
            user_id,
            e,
        )
        raise HTTPException(status_code=503, detail="Clerk API unavailable") from e

    if response.status_code >= 400:
        if _is_already_member(response):
            return {"status": "already_member", "membership_id": None}
        if _has_error_code(response, _MEMBER_CAP_CODE):
            # The org's `max_allowed_memberships` is full. A 502 here told a
            # blocked joiner to "try again", which can never work; say what it
            # is so the join page renders it as terminal (2026-09-16 incident:
            # every prod org was capped at 1 or 5 members).
            logger.warning(
                "clerk_admin.create_organization_membership member cap reached org=%s user=%s",
                org_id,
                user_id,
            )
            raise HTTPException(status_code=409, detail=MEMBER_CAP_REACHED)
        logger.warning(
            "clerk_admin.create_organization_membership HTTP %s org=%s user=%s body=%s",
            response.status_code,
            org_id,
            user_id,
            response.text,
        )
        raise HTTPException(status_code=502, detail="Could not add you to that workspace.")

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return {"status": "joined", "membership_id": payload.get("id")}


# Clerk has used more than one code for "this user is already in the org"
# across API versions, and the join flow must treat every one of them as an
# idempotent success rather than a failed join.
_ALREADY_MEMBER_CODES = frozenset(
    {
        "already_a_member_in_organization",
        "organization_membership_exists",
        "duplicate_record",
    }
)


# Clerk's code when the org's `max_allowed_memberships` (members PER ORG — not
# orgs per user) is full, "including outstanding invitations".
_MEMBER_CAP_CODE = frozenset({"organization_membership_quota_exceeded"})
MEMBER_CAP_REACHED = "This workspace has reached its member limit and can't take new members right now."


def _has_error_code(response: httpx.Response, codes: frozenset[str]) -> bool:
    """True when a Clerk error body carries any of ``codes``."""
    try:
        payload = response.json()
    except ValueError:
        return False
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list):
        return False
    return any(isinstance(e, dict) and e.get("code") in codes for e in errors)


def _is_already_member(response: httpx.Response) -> bool:
    """True when a Clerk 4xx means "already a member of this org"."""
    return _has_error_code(response, _ALREADY_MEMBER_CODES)


class SlugTakenError(Exception):
    """Raised by ``create_organization`` when Clerk 422s because the
    requested slug is already in use by another organization.

    Distinct from the module's usual "swallow and return None/[]" posture:
    a slug collision is retryable (pick another slug), unlike a generic
    failure, so callers need to tell the two apart rather than getting a
    flat None for both.
    """


# Clerk has used more than one code for "this identifier (e.g. slug) is
# already taken" — mirrors `_ALREADY_MEMBER_CODES`'s defensive posture.
_SLUG_TAKEN_CODES = frozenset({"form_identifier_exists", "duplicate_record"})


def _is_slug_taken(response: httpx.Response) -> bool:
    """True when a Clerk 422 means "that slug is already taken"."""
    try:
        payload = response.json()
    except ValueError:
        return False
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list) or not errors:
        return False
    first = errors[0]
    return isinstance(first, dict) and first.get("code") in _SLUG_TAKEN_CODES


async def create_organization(*, name: str, created_by: str, slug: str | None = None) -> dict | None:
    """Create a Clerk organization with ``created_by`` as its admin.

    ``POST /v1/organizations`` with body ``{name, slug, created_by}`` — Clerk
    makes the ``created_by`` user the org's initial admin member, which is
    exactly the workspace-first shape (one human, one workspace, admin role).

    Returns the created org dict on 2xx. Returns ``None`` (logged as a
    warning) for a network error, an unset ``CLERK_SECRET_KEY``, or any
    non-2xx response — EXCEPT a 422 slug collision, which raises
    ``SlugTakenError`` instead so a caller can retry with a different slug
    rather than treating it as a generic, unretryable failure.
    """
    if not settings.CLERK_SECRET_KEY:
        return None

    headers = {
        "Authorization": f"Bearer {settings.CLERK_SECRET_KEY}",
        "Content-Type": "application/json",
        # api.clerk.com sits behind Cloudflare, which rejects bare client UAs
        # (Python's default gets a 403 code 1010); every write here names itself.
        "User-Agent": "lightbulb-backend/1.0",
    }
    # Slugs are an instance feature; this instance has them DISABLED and Clerk
    # 403s ``organization_slugs_disabled`` on any create that names one.
    body: dict = {"name": name, "created_by": created_by}
    if slug:
        body["slug"] = slug

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.post(f"{_CLERK_API_BASE}/organizations", headers=headers, json=body)
    except Exception as e:  # noqa: BLE001
        logger.warning("clerk_admin.create_organization network error: %s", e)
        return None

    if response.status_code == 422 and _is_slug_taken(response):
        raise SlugTakenError(slug)

    if response.status_code >= 400:
        logger.warning(
            "clerk_admin.create_organization HTTP %s slug=%s body=%s",
            response.status_code,
            slug,
            response.text,
        )
        return None

    try:
        return response.json()
    except ValueError:
        return None


# --- Organization invitations (email) ----------------------------------------
#
# Back since 2026-10-08, next to the join link (see ``workspace_invitations``).
# Unlike the lenient read helpers above, these RAISE: an admin managing who is
# invited must see a failure, not an empty list or a fake "sent".

INVITE_ROLE = "org:member"
ALREADY_MEMBER = "That person is already in this workspace."
ALREADY_INVITED = "That email already has a pending invitation."
INVITATIONS_UNAVAILABLE = "Could not update invitations right now. Try again in a moment."

# Fallback mapping only — ``workspace_invitations`` checks members and pending
# invitations itself before calling Clerk, because Clerk does not document the
# codes it answers a duplicate with. ``duplicate_record`` / ``form_identifier_exists``
# are Clerk's generic "this identifier already exists" codes (the slug collision
# above uses them), which on this endpoint means the email already has an invite.
_ALREADY_INVITED_CODES = frozenset({"duplicate_record", "form_identifier_exists"})
_INVITE_ALREADY_MEMBER_CODES = _ALREADY_MEMBER_CODES - _ALREADY_INVITED_CODES


async def _invitations_call(method: str, path: str, **kwargs) -> httpx.Response:
    """One Clerk call for the invitation helpers; a transport error is a 502."""
    from fastapi import HTTPException

    headers = {
        "Authorization": f"Bearer {settings.CLERK_SECRET_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "lightbulb-backend/1.0",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            return await client.request(method, f"{_CLERK_API_BASE}{path}", headers=headers, **kwargs)
    except httpx.TransportError as e:
        logger.warning("clerk_admin %s %s transport error: %s", method, path, e)
        raise HTTPException(status_code=502, detail=INVITATIONS_UNAVAILABLE) from e


def _invitations_failed(op: str, org_id: str, response: httpx.Response):
    """Log Clerk's status (not its body, which echoes the invitee's email) and 502."""
    from fastapi import HTTPException

    logger.warning("clerk_admin.%s HTTP %s org=%s", op, response.status_code, org_id)
    return HTTPException(status_code=502, detail=INVITATIONS_UNAVAILABLE)


async def create_organization_invitation(*, org_id: str, email: str, inviter_user_id: str, redirect_url: str) -> dict:
    """Email an invitation to join ``org_id`` as ``org:member``. Returns Clerk's invitation.

    Clerk sends the email; its link lands on ``redirect_url`` (our ``/sign-up``),
    where the prebuilt ``<SignUp>`` consumes the ``__clerk_ticket`` and creates the
    membership. ``organizationMembership.created`` does the rest, as for a join link.

    Raises 409 for an existing member / pending invite / full member cap, 502 for
    anything else. The email is never logged.
    """
    if not settings.CLERK_SECRET_KEY:
        # Local-dev stub, matching this module's posture elsewhere.
        return {"id": "orginv_stub", "email_address": email, "status": "pending", "created_at": 0, "stubbed": True}

    from fastapi import HTTPException

    response = await _invitations_call(
        "POST",
        f"/organizations/{org_id}/invitations",
        json={
            "email_address": email,
            "inviter_user_id": inviter_user_id,
            "role": INVITE_ROLE,
            "redirect_url": redirect_url,
        },
    )
    if response.status_code >= 400:
        if _has_error_code(response, _INVITE_ALREADY_MEMBER_CODES):
            raise HTTPException(status_code=409, detail=ALREADY_MEMBER)
        if _has_error_code(response, _ALREADY_INVITED_CODES):
            raise HTTPException(status_code=409, detail=ALREADY_INVITED)
        if _has_error_code(response, _MEMBER_CAP_CODE):
            # Clerk counts outstanding invitations against the cap.
            raise HTTPException(status_code=409, detail=MEMBER_CAP_REACHED)
        raise _invitations_failed("create_organization_invitation", org_id, response)
    return response.json()


async def list_pending_organization_invitations(org_id: str) -> list[dict]:
    """Every pending invitation for ``org_id`` (Clerk invitation dicts). Raises 502 on failure."""
    if not settings.CLERK_SECRET_KEY:
        return []
    response = await _invitations_call(
        "GET", f"/organizations/{org_id}/invitations", params={"status": "pending", "limit": 100}
    )
    if response.status_code >= 400:
        raise _invitations_failed("list_pending_organization_invitations", org_id, response)
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else payload
    # ponytail: one page of 100 pending invites; paginate if a workspace ever has more.
    return [r for r in rows or [] if isinstance(r, dict)]


async def revoke_organization_invitation(*, org_id: str, invitation_id: str, requesting_user_id: str) -> None:
    """Revoke a pending invitation. Idempotent.

    An invitation that is missing, or no longer pending (already revoked,
    accepted, expired), is a success: the admin wanted it gone and it is.
    Clerk does not document the 4xx it returns for those, so a 4xx is
    confirmed with a GET of the invitation rather than guessed from a code;
    only an invitation that is STILL pending afterwards is a failure.
    """
    if not settings.CLERK_SECRET_KEY:
        return
    path = f"/organizations/{org_id}/invitations/{invitation_id}"
    response = await _invitations_call("POST", f"{path}/revoke", json={"requesting_user_id": requesting_user_id})
    if response.status_code < 400 or response.status_code == 404:
        return
    if 400 <= response.status_code < 500:
        current = await _invitations_call("GET", path)
        if current.status_code == 404:
            return
        if current.status_code < 400 and (current.json() or {}).get("status") != "pending":
            return
    raise _invitations_failed("revoke_organization_invitation", org_id, response)


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
