"""Keep Paperclip's ``GITHUB_TOKEN`` company secret equal to the workspace's GitHub connection.

Paperclip resolves GitHub pull requests and issues (the external objects shown on a task) with
a COMPANY SECRET named ``GITHUB_TOKEN`` (``server/src/services/git-credentials.ts``,
``github-external-object-provider.ts`` in the fork). Without one its lookups are unauthenticated:
a shared 60/hour, and a private repository answers a terminal "Not found".

The workspace already has a GitHub token: the one the owner granted in Settings -> Connections.
``sync_github_token`` makes Paperclip match it -- create, rotate, or delete -- and is idempotent,
so it runs on connect, on disconnect, on every runtime reconcile and, for every workspace with a
Paperclip company, at every backend start (``sweep``).

Three rules:

* **Only the token string leaves.** The stored credential dict also carries the platform OAuth
  app's ``client_secret``; nothing but ``token`` / ``access_token`` is ever sent.
* **The token is never logged**, never put in an exception message, a metric dimension or the
  returned outcome. ``PaperclipApiError.__str__`` appends the response body, so the best-effort
  wrapper logs the exception TYPE and status only.
* **Only our own secret is touched.** Ours has a description that STARTS WITH
  ``SECRET_DESCRIPTION``. A ``GITHUB_TOKEN`` with any other description is one a member set by
  hand: it is left alone (``foreign_secret``).

The description also carries a fingerprint of the token it was last synced with
(`` [fp:<12 hex>]``), which is how a sync with an unchanged token knows to do nothing -- no new
secret version, no ``secret.rotated`` row in the company's activity.
"""

from __future__ import annotations

import asyncio
import hashlib
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
#: The ownership marker, as a PREFIX. Paperclip's secret list returns ``description`` verbatim, so
#: "starts with this" is "Lightbulb created this". Changing the text orphans every secret synced
#: under the old one (they would read as foreign), so treat it as stored data.
SECRET_DESCRIPTION = (
    "Synced from the workspace's GitHub connection by Lightbulb. Do not edit; reconnect GitHub in Settings."
)


def fingerprint(token: str) -> str:
    """First 12 hex of the token's SHA-256: enough to tell "same token" from "new token", and
    not reversible for a high-entropy OAuth token. Safe to log; the token is not."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def _description(token: str) -> str:
    return f"{SECRET_DESCRIPTION} [fp:{fingerprint(token)}]"


#: How many times one sync re-plans when the workspace's token moves underneath it.
_MAX_PASSES = 3


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
            return row, not str(row.get("description") or "").startswith(SECRET_DESCRIPTION)
    return None, False


async def _sync(
    admin: PaperclipAdminClient, cookie: str, owner_id: str, company_id: str, token: str | None, dry_run: bool
) -> str | None:
    """One pass, planned from ``token``. Returns None -- having written nothing more -- when the
    workspace's token is found to have moved right before a write: the plan is stale."""

    def did(verb: str) -> str:
        return f"would_{verb}" if dry_run else f"{verb}d"

    async def stale() -> bool:
        return not dry_run and await github_token(owner_id) != token

    secret, foreign = await _our_secret(admin, cookie, company_id)
    if foreign:
        return "foreign_secret"
    if not token:
        if secret is None:
            return "absent"
        if await stale():
            return None
        if not dry_run:
            try:
                await admin.delete_secret(session_cookie=cookie, secret_id=secret["id"])
            except PaperclipApiError as exc:
                if exc.status_code != 404:
                    raise
                # A concurrent sync (another backend task's sweep) deleted it first.
        return did("delete")
    if secret is None:
        if dry_run:
            return did("create")
        if await stale():
            return None
        try:
            await admin.create_company_secret(
                session_cookie=cookie,
                company_id=company_id,
                name=SECRET_NAME,
                value=token,
                description=_description(token),
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
    if secret.get("description") == _description(token):
        return "unchanged"
    # A new token, or a secret from before the fingerprint existed. Rotate FIRST, then record
    # the fingerprint: a failure between the two leaves a stale fingerprint, which costs one
    # extra rotate next time -- never a skipped one.
    if await stale():
        return None
    if not dry_run:
        await admin.rotate_secret(session_cookie=cookie, secret_id=secret["id"], value=token)
        await admin.update_secret_description(
            session_cookie=cookie, secret_id=secret["id"], description=_description(token)
        )
    return did("rotate")


async def sync_github_token(owner_id: str, *, dry_run: bool = False) -> str:
    """Make Paperclip's ``GITHUB_TOKEN`` secret match the owner's GitHub connection.

    Returns a short outcome: ``no_company`` (no active Paperclip company yet), ``created``,
    ``unchanged`` (ours already holds this token; nothing written), ``rotated``, ``deleted``
    (Lightbulb holds no token any more), ``absent`` (no token, no secret), ``foreign_secret``
    (someone else's ``GITHUB_TOKEN``; untouched), ``inactive_secret`` (ours, but a member
    disabled it; untouched), ``conflict``, or ``unsettled`` (the token kept changing under
    ``_MAX_PASSES`` passes; the next reconcile's sync repairs it). With ``dry_run`` the three
    writes read ``would_create`` / ``would_rotate`` / ``would_delete`` and only the Paperclip
    LIST call is made. Raises on a Paperclip or Secrets Manager failure.
    """
    company_id = await resolve_company_id_for_owner(
        owner_id=owner_id, repo=PaperclipRepo(table_name="paperclip-companies")
    )
    if company_id is None:
        return "no_company"
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        admin = PaperclipAdminClient(http_client=http)

        async def one_pass(token: str | None) -> str | None:
            try:
                return await _sync(admin, await get_admin_session_cookie(http), owner_id, company_id, token, dry_run)
            except PaperclipApiError as exc:
                if exc.status_code != 401:
                    raise
            # A stale cached admin session: sign in again and retry once.
            invalidate_admin_session()
            return await _sync(admin, await get_admin_session_cookie(http), owner_id, company_id, token, dry_run)

        # Connect and disconnect each run this AFTER releasing the owner lifecycle lease, so two
        # of them can overlap and a pass can act on a token the other request has since replaced
        # or removed. Rather than hold the lease across Paperclip calls, each pass revalidates
        # around its write: the token is re-read right before the write (stale -> plan again)
        # and once more after it (moved -> go again). Whichever sync finishes last has therefore
        # seen Lightbulb's state after its own write. What remains: a change landing after that
        # final read -- which belongs to another connect or disconnect, and that request runs
        # its own sync after its own write.
        for _ in range(_MAX_PASSES):
            token = await github_token(owner_id)
            outcome = await one_pass(token)
            if outcome is not None and (dry_run or await github_token(owner_id) == token):
                return outcome
    return "unsettled"


#: Ceiling on what a best-effort sync may add to the request or reconcile it rides on.
_BEST_EFFORT_TIMEOUT_S = 10.0

#: What ``sync_best_effort`` returns when the sync raised or timed out.
FAILED = "failed"
#: Outcomes after which Paperclip may NOT hold what Lightbulb holds. Every other outcome is
#: settled: in step, or deliberately left alone (no company, a member's own or disabled secret).
UNSYNCED = frozenset({FAILED, "unsettled", "conflict"})


async def sync_best_effort(owner_id: str, *, dry_run: bool = False) -> str:
    """``sync_github_token`` for a caller that must not fail because of it (a connect, a
    disconnect, a reconcile, the fleet sweep). Never raises, and gives up after
    ``_BEST_EFFORT_TIMEOUT_S``. Returns the outcome, or ``FAILED`` (logged and counted)."""
    try:
        sync = sync_github_token(owner_id, dry_run=True) if dry_run else sync_github_token(owner_id)
        outcome = await asyncio.wait_for(sync, timeout=_BEST_EFFORT_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 -- the metric is the signal; the caller carries on
        # Type and status only: str() of a PaperclipApiError carries the response body.
        logger.error(
            "paperclip github token sync FAILED for owner=%s: %s status=%s",
            owner_id,
            type(exc).__name__,
            getattr(exc, "status_code", "-"),
        )
        put_metric("paperclip.github_token_sync.error")
        return FAILED
    logger.info("paperclip github token sync for owner=%s: %s", owner_id, outcome)
    return outcome


async def sweep(owner_ids: list[str] | None = None, *, dry_run: bool = False):
    """Sync each owner in turn, yielding ``(owner_id, outcome)``. With no ``owner_ids``: every
    owner with an ACTIVE Paperclip company -- not only the GitHub-connected ones, so an owner
    whose disconnect cleanup failed (Lightbulb holds no token, Paperclip still does) gets the
    stale secret deleted. The backend runs this at every start
    (``paperclip_instance_flags.converge_at_startup``); the ops script is the manual door.

    Safe when several backend tasks sweep at once (2-4 boot together, doubled during a roll):
    every sync converges on the same end state from whatever it finds. Two creates: the loser's
    409 re-lists and finds the fingerprint already current. Two rotates write the same token.
    Two deletes: the loser's 404 reads as deleted. So concurrent sweeps cost duplicate calls,
    never a worse state than one.

    ponytail: sequential, one table scan and ~3 calls per owner, at most
    ``_BEST_EFFORT_TIMEOUT_S`` each. Fine for hundreds of workspaces; past that, sweep under
    a lease so one task does it.
    """
    if owner_ids is None:
        repo = PaperclipRepo(table_name="paperclip-companies")
        owner_ids = sorted(await repo.list_active_workspace_members())
    for owner_id in owner_ids:
        yield owner_id, await sync_best_effort(owner_id, dry_run=dry_run)


#: Pauses before each background retry of a failed disconnect cleanup.
_CLEANUP_RETRY_PAUSES_S: tuple[float, ...] = (30, 120, 300)
#: Strong references: the event loop keeps only weak ones to a running task.
_cleanup_retries: set[asyncio.Task] = set()


async def _retry_cleanup(owner_id: str) -> None:
    for pause in _CLEANUP_RETRY_PAUSES_S:
        await asyncio.sleep(pause)
        # Re-reads the workspace token each time, so a reconnect in between is honoured.
        if await sync_best_effort(owner_id) not in UNSYNCED:
            return
    logger.error("paperclip github token cleanup ABANDONED for owner=%s; the next backend start sweeps it", owner_id)
    put_metric("paperclip.github_token_sync.cleanup_abandoned")


async def sync_after_disconnect(owner_id: str) -> None:
    """The disconnect's sync. A failure here would leave a still-valid token in Paperclip, so
    it is retried in the background (never delaying or failing the response). Bounded retries,
    not a durable queue: a task that dies mid-retry leaves it to the start-up sweep."""
    if await sync_best_effort(owner_id) in UNSYNCED:
        task = asyncio.create_task(_retry_cleanup(owner_id))
        _cleanup_retries.add(task)
        task.add_done_callback(_cleanup_retries.discard)
