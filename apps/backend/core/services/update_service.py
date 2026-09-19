"""Update service -- pending-update applier + scheduled worker.

The scheduled worker also runs two Paperclip-related passes (T13):

* ``_paperclip_purge_pass`` — once per day, scan paperclip-companies
  for ``status="disabled"`` rows whose ``scheduled_purge_at <= now``
  and call ``PaperclipProvisioning.purge`` for each.
* ``_paperclip_provision_retry_pass`` — every loop iteration, consume
  pending-updates rows with ``update_type="paperclip_provision"``
  (enqueued by ``_handle_organization_membership_deleted`` in
  ``routers/webhooks.py`` for ``op="archive_member"``) and re-call
  the matching provisioning entry-point. Successful rows are marked
  applied; non-retryable failures get marked failed with the truncated
  reason; retryable failures stay pending and are picked up on the
  next iteration.

Plus the P0c owner-day rollup pass (``_rollup_pass``) — once per hour,
refresh today+yesterday rollup rows for every owner with a bound company
Stripe account (``rollup_service.refresh_owner_day``).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from core.observability.metrics import put_metric
from core.repositories import billing_repo, container_repo, update_repo

logger = logging.getLogger(__name__)

# The pending-update ``update_type`` the Paperclip retry pass owns. ``routers/webhooks.py``
# imports it so the webhook that enqueues a retry and the pass that drains it can never
# disagree on the label.
PAPERCLIP_RETRY_KIND = "paperclip_provision"

# Daily purge pass cadence, in seconds.
_PURGE_PASS_INTERVAL_SECONDS = 24 * 60 * 60

# Hourly owner-day rollup pass cadence, in seconds (P0c).
_ROLLUP_PASS_INTERVAL_SECONDS = 60 * 60

# Inter-iteration wait for run_scheduled_worker, in seconds.
_WORKER_INTERVAL_S = 60


# Cooperative stop signal for run_scheduled_worker. Created LAZILY inside
# run_scheduled_worker so the Event binds to the loop that actually runs the
# worker. A module-level asyncio.Event() binds to the import-time loop and
# raises "bound to a different event loop" under anything that spins up a
# fresh loop (e.g. the ASGI test client per contract test). request_worker_stop
# lets a graceful shutdown ask the worker to finish its current iteration and
# exit instead of hard-cancelling mid-pass.
_worker_stop: asyncio.Event | None = None


def request_worker_stop() -> None:
    """Ask run_scheduled_worker to finish its current iteration and exit."""
    if _worker_stop is not None:
        _worker_stop.set()


# Keys upstream removed from the strict OpenClaw schema that an older
# on-disk openclaw.json may still carry. A container rolled to a newer
# image reuses its existing EFS config, and a removed key the new image's
# strict schema rejects crash-loops the gateway at boot. embeddedHarness was
# dropped from agents.defaults; the AgentEntrySchema set (embeddedHarness,
# embeddedPi, agentRuntime, systemPromptOverride) can also persist under
# existing agents.list[] entries that an older marketplace deploy file-merged
# verbatim — so both surfaces must be scrubbed before the roll.
_REMOVED_DEFAULTS_KEYS: tuple[str, ...] = ("embeddedHarness",)


def _build_paperclip_provisioning():
    """Construct a PaperclipProvisioning + its underlying httpx client.

    Returns a ``(provisioning, http_client)`` tuple. The caller MUST
    close ``http_client`` (via ``await http_client.aclose()``) once the
    pass finishes — this mirrors the per-request construction pattern
    used by the webhook handlers (``core.services.paperclip_provisioning.build_paperclip_provisioning``)
    and avoids leaking sockets across daily/per-loop pass runs.

    Imports are local so this module can be loaded in environments
    without the Paperclip integration configured (e.g. local pytest
    runs that don't need to exercise the Paperclip passes).
    """
    import httpx

    from core.config import settings
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_provisioning import PaperclipProvisioning

    http = httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=15.0,
    )
    admin = PaperclipAdminClient(http_client=http)
    # Short name only — ``core.dynamodb.get_table`` prepends the env
    # prefix once. Mirrors ``channel_link_repo`` and every other repo in
    # ``core/repositories/``.
    repo = PaperclipRepo(table_name="paperclip-companies")
    provisioning = PaperclipProvisioning(admin, repo, env_name=settings.ENVIRONMENT)
    return provisioning, http, repo


async def _paperclip_purge_pass() -> None:
    """Daily pass: hard-delete disabled Paperclip rows past their grace
    window.

    Scans ``paperclip-companies`` via the ``by-status-purge-at`` GSI
    for rows with ``status="disabled"`` AND
    ``scheduled_purge_at <= now``, then calls
    ``PaperclipProvisioning.purge(user_id=...)`` for each. Per-row
    failures are logged and swallowed so one bad row doesn't break
    the whole pass.
    """
    from datetime import datetime, timezone

    try:
        provisioning, http, repo = _build_paperclip_provisioning()
    except Exception:
        logger.exception("paperclip_purge_pass: failed to build provisioning service")
        return

    try:
        try:
            due = await repo.scan_purge_due(datetime.now(timezone.utc))
        except Exception:
            logger.exception("paperclip_purge_pass: scan_purge_due failed")
            return

        if not due:
            return

        logger.info("paperclip_purge_pass: %d disabled rows due for purge", len(due))
        purged = 0
        failed = 0
        for company in due:
            try:
                await provisioning.purge(user_id=company.user_id)
                logger.info("paperclip_purge_pass: deleted user_id=%s", company.user_id)
                purged += 1
            except Exception:
                logger.exception("paperclip_purge_pass: failed for user_id=%s", company.user_id)
                failed += 1
        put_metric("paperclip.purge_pass.purged", value=purged)
        if failed:
            put_metric("paperclip.purge_pass.failed", value=failed)
    finally:
        try:
            await http.aclose()
        except Exception:
            logger.exception("paperclip_purge_pass: http client close failed")


async def _paperclip_provision_retry_pass() -> None:
    """Per-loop pass: consume pending-updates rows of type
    ``paperclip_provision`` and re-call the matching provisioning op.

    Dispatch on ``changes["op"]``:

      * ``archive_member`` -> ``PaperclipProvisioning.archive_member``

    Historical note: this pass also serviced ``provision_org``,
    ``provision_member`` and ``disable`` ops back when sign-up /
    membership-created webhooks eagerly provisioned Paperclip rows.
    Those eager-provision paths were removed (see Tasks 6 + 7 of the
    paperclip-openclaw-agent-pairing plan) so nothing enqueues those
    ops anymore. The ``disable`` branch was always dead — no enqueue
    site existed. Only ``archive_member`` (enqueued by
    ``_handle_organization_membership_deleted`` in
    ``routers/webhooks.py``) remains live.

    On success, mark the row applied (``mark_applied``). On a
    non-retryable failure (or unknown op), mark failed
    (``mark_failed``). On a retryable failure (``exc.retryable=True``,
    set by ``OrgNotProvisionedError`` and 5xx/429 ``PaperclipApiError``),
    leave the row pending so the next iteration retries it.
    """
    try:
        pending = await update_repo.list_pending_by_type(PAPERCLIP_RETRY_KIND)
    except Exception:
        logger.exception("paperclip_provision_retry_pass: list_pending_by_type failed")
        return

    if not pending:
        return

    logger.info("paperclip_provision_retry_pass: %d pending rows", len(pending))
    try:
        provisioning, http, _ = _build_paperclip_provisioning()
    except Exception:
        logger.exception("paperclip_provision_retry_pass: failed to build provisioning service")
        return

    succeeded = 0
    retryable_left = 0
    nonretryable = 0
    try:
        for row in pending:
            owner_id = row.get("owner_id", "")
            update_id = row.get("update_id", "")
            changes = row.get("changes") or {}
            op = changes.get("op")

            try:
                if op == "archive_member":
                    await provisioning.archive_member(user_id=changes["user_id"])
                else:
                    logger.error(
                        "paperclip_provision_retry_pass: unknown op=%r (owner=%s, update=%s)",
                        op,
                        owner_id,
                        update_id,
                    )
                    await update_repo.mark_failed(owner_id, update_id, reason=f"unknown op {op!r}")
                    nonretryable += 1
                    continue
            except Exception as exc:
                if getattr(exc, "retryable", False):
                    logger.warning(
                        "paperclip_provision_retry_pass: retryable failure op=%s owner=%s: %s",
                        op,
                        owner_id,
                        exc,
                    )
                    retryable_left += 1
                else:
                    logger.exception(
                        "paperclip_provision_retry_pass: non-retryable failure op=%s owner=%s",
                        op,
                        owner_id,
                    )
                    try:
                        await update_repo.mark_failed(owner_id, update_id, reason=str(exc))
                    except Exception:
                        logger.exception("paperclip_provision_retry_pass: mark_failed failed")
                    nonretryable += 1
                continue

            # Success: mark the row as applied so we don't re-run it.
            # mark_applied swallows ConditionalCheckFailed and returns False
            # rather than raising, so a try/except/else can't tell a rejected
            # flip from a real one — check the return value and only count a
            # genuine flip as succeeded.
            try:
                marked = await update_repo.mark_applied(owner_id, update_id)
            except Exception:
                logger.exception(
                    "paperclip_provision_retry_pass: mark_applied failed for %s/%s",
                    owner_id,
                    update_id,
                )
                marked = False
            if marked:
                succeeded += 1
                logger.info(
                    "paperclip_provision_retry_pass: succeeded op=%s owner=%s",
                    op,
                    owner_id,
                )
            else:
                logger.warning(
                    "paperclip_provision_retry_pass: mark_applied did not flip %s/%s to applied",
                    owner_id,
                    update_id,
                )
    finally:
        try:
            await http.aclose()
        except Exception:
            logger.exception("paperclip_provision_retry_pass: http client close failed")

    if succeeded:
        put_metric("paperclip.retry_pass.succeeded", value=succeeded)
    if retryable_left:
        put_metric("paperclip.retry_pass.retryable", value=retryable_left)
    if nonretryable:
        put_metric("paperclip.retry_pass.failed", value=nonretryable)


async def _rollup_pass() -> None:
    """P0c: refresh today+yesterday owner-day rollup rows.

    Iterates owners with a bound company Stripe account and recomputes
    their rollup rows from provider truth. Per-owner try/except so one
    owner's provider failure never starves the rest. Idempotent by
    construction (whole-row recompute-and-replace), so the transient
    ×2 worker during deploys is harmless.
    """
    from core.services import rollup_service  # local: keeps worker import light

    owners = await billing_repo.list_company_stripe_owners()
    today = datetime.now(timezone.utc).date()
    days = (today.isoformat(), (today - timedelta(days=1)).isoformat())
    for owner_id in owners:
        try:
            for day in days:
                await rollup_service.refresh_owner_day(owner_id, day)
        except Exception:
            logger.exception("rollup refresh failed (owner=%s)", owner_id)


async def admitted(name: str, *, interval_s: int) -> bool:
    """Pass admission plus its metric. See container_repo.try_claim_pass."""
    won = await container_repo.try_claim_pass(name, interval_s=interval_s)
    put_metric("lifecycle.pass.claimed", dimensions={"pass": name, "outcome": "won" if won else "lost"})
    return won


async def run_scheduled_worker() -> None:
    """Background worker for the three fleet-wide reconciliation passes.

    Runs every 60 seconds indefinitely. Each iteration enters its passes (Paperclip retry,
    Paperclip purge, owner-day rollup) only on the instance that wins ``admitted()`` for that
    pass's window that tick -- at desiredCount >= 2 the loser scans nothing, instead of every
    instance re-scanning the same rows. Cadence for the daily purge and hourly rollup lives on
    the admission row (``interval_s``), not a process-local clock, so a deploy no longer forces
    an immediate run.

    The fourth pass -- applying due Track-2 container updates (image rolls, resizes,
    task-definition refreshes) and recovering their stranded claims -- went with the ECS
    runtime. Only the heartbeat metric below is deliberately un-admitted (this loop's own
    liveness canary).
    """
    logger.info("Scheduled update worker started")
    global _worker_stop
    _worker_stop = asyncio.Event()  # bind to the loop running this worker
    while not _worker_stop.is_set():
        try:
            put_metric("update.scheduled_worker.heartbeat")
            # T13: Paperclip retry pass — admitted every loop so retryable
            # failures recover within a minute of the upstream issue clearing.
            if await admitted("paperclip_retry", interval_s=_WORKER_INTERVAL_S):
                try:
                    await _paperclip_provision_retry_pass()
                except Exception:
                    logger.exception("paperclip_provision_retry_pass crashed")

            # T13: Paperclip purge pass — admitted once per
            # _PURGE_PASS_INTERVAL_SECONDS (24h), fleet-wide.
            if await admitted("paperclip_purge", interval_s=_PURGE_PASS_INTERVAL_SECONDS):
                try:
                    await _paperclip_purge_pass()
                except Exception:
                    logger.exception("paperclip_purge_pass crashed")

            # P0c: owner-day rollup pass — admitted once per
            # _ROLLUP_PASS_INTERVAL_SECONDS (1h), fleet-wide.
            if await admitted("owner_day_rollup", interval_s=_ROLLUP_PASS_INTERVAL_SECONDS):
                try:
                    await _rollup_pass()
                except Exception:
                    logger.exception("rollup_pass crashed")
        except Exception:
            put_metric("update.scheduled_worker.error")
            logger.exception("Error in scheduled update worker loop")

        # Interruptible inter-iteration wait: wakes immediately on stop request.
        try:
            await asyncio.wait_for(_worker_stop.wait(), timeout=_WORKER_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
