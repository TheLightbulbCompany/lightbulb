"""Per-user Better Auth session manager for the Teams BFF.

Spec §2: every user-scoped API call to Paperclip carries a Better Auth
session cookie obtained by signing in *as the user* using their stored
Fernet-encrypted password. The cookie never leaves the backend.

V1: per-request sign-in. Inside the VPC the round trip is single-digit
ms — same shape the proxy used. V2 (future): short-TTL in-process or
Redis cache keyed by user_id, refreshed on 401.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Protocol

from core.encryption import decrypt
from core.repositories.paperclip_repo import PaperclipCompany

logger = logging.getLogger(__name__)


class UserSessionError(Exception):
    """Raised when a per-user Paperclip session cannot be obtained."""


class _Repo(Protocol):
    async def get(self, user_id: str, *, consistent_read: bool = False) -> PaperclipCompany | None: ...


class _AdminClient(Protocol):
    async def sign_in_user(self, *, email: str, password: str) -> dict: ...


async def get_user_session_cookie(
    *,
    user_id: str,
    repo: _Repo,
    admin_client: _AdminClient,
    clerk_email_resolver: Callable[[str], Awaitable[str]],
) -> str:
    """Sign in to Paperclip as the user and return the Set-Cookie value.

    Raises ``UserSessionError`` if:
      - the user has no provisioned company
      - the company exists but ``status != "active"``
      - the upstream sign-in returns no Set-Cookie
    """
    # Strongly consistent: the brief drain calls this right after the company
    # row flips to active, and a replica-lagged read here raises
    # UserSessionError -- which the drain counts as a FAILED attempt, burning
    # the retry budget on pure timing.
    company = await repo.get(user_id, consistent_read=True)
    if company is None:
        raise UserSessionError(f"team workspace not provisioned for user {user_id}")
    if company.status != "active":
        raise UserSessionError(f"team workspace not active for user {user_id} (status={company.status})")

    # Better Auth's ``user.email`` is written once at provision time and never
    # updated, so the row's recorded address — not the live Clerk one — is the
    # sign-in truth. Fall back to Clerk only for rows written before
    # ``paperclip_email`` existed (or not yet backfilled).
    email = company.paperclip_email or await clerk_email_resolver(user_id)
    password = decrypt(company.paperclip_password_encrypted)

    resp = await admin_client.sign_in_user(email=email, password=password)
    cookie = resp.get("_session_cookie") if isinstance(resp, dict) else None
    if not cookie:
        raise UserSessionError(f"sign-in returned no session cookie for user {user_id}")
    return cookie
