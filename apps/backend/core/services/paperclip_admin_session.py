"""Admin session manager for the Paperclip service account.

Maintains a long-lived Better Auth session for the Paperclip bootstrap
admin (``admin+paperclip@isol8.co`` in new envs, ``admin@isol8.co`` in
legacy envs — the email is read from Secrets Manager, never hardcoded
here). The bootstrap admin is the only user with the ``instance_admin``
role on the Paperclip deployment. Used by the prod data-move scripts to
call privileged endpoints (``POST /api/companies``,
``approve_join_request``, etc.) that regular signed-up users can't reach
in Paperclip's ``authenticated`` deployment mode.

**Why a service account instead of granting every user instance_admin:**
the alternative — putting Paperclip in ``local_trusted`` mode — would
make every request implicit-admin and lose Paperclip's per-company
authz isolation entirely. With the service account, regular users
remain non-admin (Paperclip's authz still filters their requests by
company memberships), and only the backend has elevated privileges.

**Credential lifecycle:**
- Read once per process from Secrets Manager (``lightbulb/{env}/paperclip_admin_credentials``)
- Cached for the process lifetime
- Better Auth session re-acquired on first call after process start, on
  401 from any downstream admin call (call ``invalidate_admin_session()``),
  and after ~23h regardless (well below Better Auth's 7-day default TTL)
- Async-safe: concurrent callers serialize on a single asyncio.Lock so
  we don't issue parallel sign-ins on cold start

**Bootstrap dependency:**
This module raises a clear ``RuntimeError`` if the Secrets Manager entry
doesn't exist — that means the operator hasn't run
``apps/backend/scripts/bootstrap_paperclip_admin.py`` yet. Provisioning
fails loudly rather than silently degrading.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import boto3
import httpx

from core.services.paperclip_admin_client import PaperclipAdminClient

logger = logging.getLogger(__name__)

# Refresh well before Better Auth's 7-day default expiry. 23h gives at
# most one refresh per day; on backend redeploys we get a fresh session
# anyway. Tuning knob — if Paperclip's TTL changes, lower this.
_SESSION_TTL_SECONDS = 23 * 3600

# AWS Secrets Manager naming convention. Per-env: dev →
# ``lightbulb/dev/paperclip_admin_credentials``. Created by the bootstrap
# script after sign-up; fields ``email`` + ``password`` (JSON).
_SECRET_NAME_TEMPLATE = "lightbulb/{env}/paperclip_admin_credentials"


class _AdminSessionState:
    """Singleton state for the process. Held outside any class so
    multiple PaperclipAdminClient instances share the same session.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cookie: str | None = None
        self._cookie_acquired_at: float = 0.0
        self._email: str | None = None
        self._password: str | None = None

    def is_cookie_fresh(self) -> bool:
        # monotonic: session age is an in-process elapsed check (stamped in
        # get_admin_session_cookie, compared here), immune to wall-clock steps.
        return self._cookie is not None and (time.monotonic() - self._cookie_acquired_at) < _SESSION_TTL_SECONDS

    def invalidate(self) -> None:
        self._cookie = None
        self._cookie_acquired_at = 0.0


_state = _AdminSessionState()


def _load_credentials_from_secrets_manager(env: str) -> tuple[str, str]:
    """Synchronous Secrets Manager read. boto3 has no async support
    natively; for a one-shot at first sign-in the sync call inside
    ``asyncio.to_thread`` is fine.
    """
    secret_name = _SECRET_NAME_TEMPLATE.format(env=env)
    client = boto3.client("secretsmanager")
    try:
        resp = client.get_secret_value(SecretId=secret_name)
    except client.exceptions.ResourceNotFoundException as e:
        raise RuntimeError(
            f"Paperclip admin credentials not found at {secret_name}. "
            "Run apps/backend/scripts/bootstrap_paperclip_admin.py once per env "
            "to create the admin user and write the secret."
        ) from e
    payload = json.loads(resp["SecretString"])
    email = payload.get("email")
    password = payload.get("password")
    if not email or not password:
        raise RuntimeError(f"Secret {secret_name} is malformed: expected JSON with 'email' and 'password' fields")
    return email, password


async def get_admin_session_cookie(http_client: httpx.AsyncClient) -> str:
    """Return a fresh Better Auth session cookie for the bootstrap admin.

    Returns the bare ``name=value`` cookie line ready to drop into a
    ``Cookie:`` header. Uses the caller-provided httpx client (so the
    lifecycle is owned by whoever's making the provisioning call).
    Caches the cookie; on expiry or invalidation, re-signs-in.
    Async-safe.

    Why cookie not bearer: Paperclip's Better Auth config does not
    enable the ``bearer()`` plugin, so ``Authorization: Bearer`` is
    silently ignored — only the ``Cookie`` path actually authenticates
    the admin against ``instance_user_roles``. See
    ``paperclip_admin_client.py`` module docstring for the full chain.
    """
    async with _state._lock:
        if _state.is_cookie_fresh():
            assert _state._cookie is not None
            return _state._cookie

        if _state._email is None or _state._password is None:
            env = os.environ.get("ENVIRONMENT", "")
            email, password = await asyncio.to_thread(_load_credentials_from_secrets_manager, env)
            _state._email = email
            _state._password = password

        admin = PaperclipAdminClient(http_client=http_client)
        signin = await admin.sign_in_user(
            email=_state._email,
            password=_state._password,
        )
        cookie = signin.get("_session_cookie")
        if not cookie:
            raise RuntimeError("Better Auth sign-in for admin returned no Set-Cookie session token")

        _state._cookie = cookie
        _state._cookie_acquired_at = time.monotonic()
        logger.info(
            "paperclip_admin_session: acquired admin session cookie for %s",
            _state._email,
        )
        return cookie


def invalidate_admin_session() -> None:
    """Force re-acquisition of the admin session on the next call.

    Called by provisioning when an admin-bearing request returns 401
    (session expired, password rotated, etc.). The next
    ``get_admin_session_cookie`` call will sign in fresh.
    """
    _state.invalidate()
    logger.info("paperclip_admin_session: invalidated cached session cookie")
