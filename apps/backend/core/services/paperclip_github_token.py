"""Keep Paperclip's ``GITHUB_TOKEN`` company secret equal to the workspace's GitHub connection.

Paperclip resolves GitHub pull requests and issues (the external objects shown on a task) with
a COMPANY SECRET named ``GITHUB_TOKEN`` (``server/src/services/git-credentials.ts``,
``github-external-object-provider.ts`` in the fork). Without one its lookups are unauthenticated:
a shared 60/hour, and a private repository answers a terminal "Not found".

The workspace already has a GitHub token: the one the owner granted in Settings -> Connections.
``sync_github_token`` makes Paperclip match it -- create, rotate, or delete -- and is idempotent,
so it runs on connect, on disconnect and on every runtime reconcile.

Three rules:

* **Only the token string leaves.** The stored credential dict also carries the platform OAuth
  app's ``client_secret``; nothing but ``token`` / ``access_token`` is ever sent.
* **The token is never logged**, never put in an exception message, a metric dimension or the
  returned outcome. ``PaperclipApiError.__str__`` appends the response body, so the best-effort
  wrapper logs the exception TYPE and status only.
* **Only our own secret is touched.** Ours carries ``SECRET_DESCRIPTION``. A ``GITHUB_TOKEN`` with
  any other description is one a member set by hand: it is left alone (``foreign_secret``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.credentials import integration_secret_store
from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError
from core.services.paperclip_admin_session import get_admin_session_cookie, invalidate_admin_session
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

logger = logging.getLogger(__name__)

#: The first name Paperclip's resolver probes (``DEFAULT_GITHUB_TOKEN_SECRET_NAMES``).
SECRET_NAME = "GITHUB_TOKEN"
#: The ownership marker. Paperclip's secret list returns ``description`` verbatim, so an exact
#: match is "Lightbulb created this". Changing the text orphans every secret synced under the
#: old one (they would read as foreign), so treat it as stored data.
SECRET_DESCRIPTION = (
    "Synced from the workspace's GitHub connection by Lightbulb. Do not edit; reconnect GitHub in Settings."
)


async def github_token(owner_id: str) -> str | None:
    """The owner's stored GitHub token, or None when not connected. The paste rail stores
    ``{"token": ...}``, the direct_oauth rail stores ``access_token``."""
    credential = await integration_secret_store.get(owner_id, "github")
    if not credential:
        return None
    return credential.get("token") or credential.get("access_token")


async def _our_secret(admin: PaperclipAdminClient, cookie: str, company_id: str) -> tuple[dict[str, Any] | None, bool]:
    """``(secret, foreign)``: the company's live ``GITHUB_TOKEN`` row, and whether someone else
    made it. Paperclip's list already excludes deleted secrets (a delete renames the row)."""
    rows = await admin.list_company_secrets(session_cookie=cookie, company_id=company_id)
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("name") == SECRET_NAME:
            return row, row.get("description") != SECRET_DESCRIPTION
    return None, False


async def _sync(admin: PaperclipAdminClient, cookie: str, company_id: str, token: str | None, dry_run: bool) -> str:
    def did(verb: str) -> str:
        return f"would_{verb}" if dry_run else f"{verb}d"

    secret, foreign = await _our_secret(admin, cookie, company_id)
    if foreign:
        return "foreign_secret"
    if not token:
        if secret is None:
            return "absent"
        if not dry_run:
            await admin.delete_secret(session_cookie=cookie, secret_id=secret["id"])
        return did("delete")
    if secret is None:
        if dry_run:
            return did("create")
        try:
            await admin.create_company_secret(
                session_cookie=cookie,
                company_id=company_id,
                name=SECRET_NAME,
                value=token,
                description=SECRET_DESCRIPTION,
            )
            return did("create")
        except PaperclipApiError as exc:
            if exc.status_code != 409:
                raise
        # 409: a concurrent sync (or a member) created the name between our list and our
        # create. Read it again and treat it like any existing secret.
        secret, foreign = await _our_secret(admin, cookie, company_id)
        if foreign:
            return "foreign_secret"
        if secret is None:
            return "conflict"
    if secret.get("status", "active") != "active":
        # A member disabled or archived it; Paperclip refuses to rotate a non-active secret.
        return "inactive_secret"
    # ponytail: rotates even when the token is unchanged (Paperclip exposes no value or hash to
    # compare), so every reconcile adds one secret version and one "secret.rotated" activity
    # row. Keep a fingerprint on our side if that history gets noisy.
    if not dry_run:
        await admin.rotate_secret(session_cookie=cookie, secret_id=secret["id"], value=token)
    return did("rotate")


async def sync_github_token(owner_id: str, *, dry_run: bool = False) -> str:
    """Make Paperclip's ``GITHUB_TOKEN`` secret match the owner's GitHub connection.

    Returns a short outcome: ``no_company`` (no active Paperclip company yet), ``created``,
    ``rotated``, ``deleted`` (Lightbulb holds no token any more), ``absent`` (no token, no
    secret), ``foreign_secret`` (someone else's ``GITHUB_TOKEN``; untouched),
    ``inactive_secret`` (ours, but a member disabled it; untouched) or ``conflict``. With
    ``dry_run`` the three writes read ``would_create`` / ``would_rotate`` / ``would_delete``
    and only the Paperclip LIST call is made. Raises on a Paperclip or Secrets Manager failure.
    """
    company_id = await resolve_company_id_for_owner(
        owner_id=owner_id, repo=PaperclipRepo(table_name="paperclip-companies")
    )
    if company_id is None:
        return "no_company"
    token = await github_token(owner_id)
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        admin = PaperclipAdminClient(http_client=http)
        try:
            return await _sync(admin, await get_admin_session_cookie(http), company_id, token, dry_run)
        except PaperclipApiError as exc:
            if exc.status_code != 401:
                raise
        # A stale cached admin session: sign in again and retry once.
        invalidate_admin_session()
        return await _sync(admin, await get_admin_session_cookie(http), company_id, token, dry_run)


#: Ceiling on what a best-effort sync may add to the request or reconcile it rides on.
_BEST_EFFORT_TIMEOUT_S = 10.0


async def sync_best_effort(owner_id: str) -> None:
    """``sync_github_token`` for a caller that must not fail because of it (a connect, a
    disconnect, a reconcile). Never raises, and gives up after ``_BEST_EFFORT_TIMEOUT_S``; the
    next reconcile retries."""
    try:
        outcome = await asyncio.wait_for(sync_github_token(owner_id), timeout=_BEST_EFFORT_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 -- the metric is the signal; the caller carries on
        # Type and status only: str() of a PaperclipApiError carries the response body.
        logger.error(
            "paperclip github token sync FAILED for owner=%s: %s status=%s",
            owner_id,
            type(exc).__name__,
            getattr(exc, "status_code", "-"),
        )
        put_metric("paperclip.github_token_sync.error")
        return
    logger.info("paperclip github token sync for owner=%s: %s", owner_id, outcome)
