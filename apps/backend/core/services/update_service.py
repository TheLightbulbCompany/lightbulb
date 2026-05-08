"""Update service -- image-update queue + scheduled worker.

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
"""

import asyncio
import logging
import time

from core.observability.metrics import put_metric
from core.repositories import update_repo
from core.services.config_patcher import patch_openclaw_config

logger = logging.getLogger(__name__)

# Canonical Paperclip update_type marker. Single source of truth: both
# ``routers/webhooks.py`` (which enqueues retries) and this module
# (which consumes them) import the same constant. If this string ever
# changes, the retry pass below would silently stop picking up rows
# the webhook is still writing, so keeping it in one file forever is
# the right amount of paranoia. Public name (no underscore) since both
# the webhook router and tests import it.
PAPERCLIP_RETRY_KIND = "paperclip_provision"

# Daily purge pass cadence, in seconds.
_PURGE_PASS_INTERVAL_SECONDS = 24 * 60 * 60


async def queue_image_update(owner_id: str, new_image: str, description: str | None = None) -> dict:
    """Queue a container image update for a single owner."""
    desc = description or f"Image update to {new_image}"
    item = await update_repo.create(
        owner_id=owner_id,
        update_type="image_update",
        description=desc,
        changes={"new_image": new_image},
    )
    logger.info("Queued image update for owner=%s image=%s", owner_id, new_image)
    return item


async def queue_fleet_image_update(new_image: str, description: str | None = None) -> int:
    """Queue an image update for every owner in the billing-accounts table.

    Scans the billing-accounts DynamoDB table and creates one pending update per owner.
    Returns the count of updates created.
    """
    from core.dynamodb import get_table, run_in_thread

    table = get_table("billing-accounts")
    count = 0
    last_key = None

    while True:
        scan_kwargs: dict = {}
        if last_key:
            scan_kwargs["ExclusiveStartKey"] = last_key

        response = await run_in_thread(table.scan, **scan_kwargs)
        items = response.get("Items", [])

        for item in items:
            owner_id = item.get("owner_id")
            if not owner_id:
                continue
            await queue_image_update(owner_id, new_image, description)
            count += 1

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break

    logger.info("Queued fleet image update for %d owners, image=%s", count, new_image)
    return count


async def apply_update(owner_id: str, update_id: str) -> bool:
    """Apply a single pending update.

    1. Conditionally set status to 'applying' (prevents double-apply).
    2. Apply config patch if present.
    3. Apply ECS changes if present (resize_user_container).
    4. Mark as applied on success, failed on error.

    Returns True if applied, False if already being applied or failed precondition.
    """
    # Step 1: conditional write
    acquired = await update_repo.set_status_conditional(owner_id, update_id, "applying", ["pending", "scheduled"])
    if not acquired:
        logger.warning("Could not acquire update %s for owner %s (already applying?)", update_id, owner_id)
        return False

    try:
        # Re-fetch the update to get its details
        from core.dynamodb import get_table, run_in_thread
        from boto3.dynamodb.conditions import Key

        table = get_table("pending-updates")
        response = await run_in_thread(
            table.query,
            KeyConditionExpression=Key("owner_id").eq(owner_id) & Key("update_id").eq(update_id),
        )
        items = response.get("Items", [])
        if not items:
            logger.error("Update %s not found after acquiring lock (owner=%s)", update_id, owner_id)
            return False

        update = items[0]
        changes = update.get("changes", {})
        update_type = update.get("update_type", "")

        # Step 2: apply config patch if present
        config_patch = changes.get("config_patch")
        if config_patch:
            await patch_openclaw_config(owner_id, config_patch)
            logger.info("Applied config patch for update %s (owner=%s)", update_id, owner_id)

        # Step 3: apply ECS changes if present
        new_image = changes.get("new_image")
        new_cpu = changes.get("new_cpu")
        new_memory = changes.get("new_memory")

        if new_image or new_cpu or new_memory:
            from core.containers import get_ecs_manager

            await get_ecs_manager().resize_user_container(
                user_id=owner_id,
                new_cpu=new_cpu,
                new_memory=new_memory,
                new_image=new_image,
            )
            logger.info(
                "ECS update applied for owner=%s image=%s cpu=%s mem=%s (update=%s)",
                owner_id,
                new_image,
                new_cpu,
                new_memory,
                update_id,
            )

        # Step 4: mark as applied
        await update_repo.mark_applied(owner_id, update_id)
        logger.info("Update %s applied successfully (owner=%s, type=%s)", update_id, owner_id, update_type)
        return True

    except Exception:
        logger.exception("Failed to apply update %s (owner=%s)", update_id, owner_id)
        # Mark as failed
        try:
            await update_repo.set_status_conditional(owner_id, update_id, "failed", ["applying"])
        except Exception:
            logger.exception("Failed to mark update %s as failed", update_id)
        return False


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
    # prefix once. Mirrors ``api_key_repo`` and every other repo in
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
            try:
                await update_repo.mark_applied(owner_id, update_id)
            except Exception:
                logger.exception(
                    "paperclip_provision_retry_pass: mark_applied failed for %s/%s",
                    owner_id,
                    update_id,
                )
            else:
                succeeded += 1
                logger.info(
                    "paperclip_provision_retry_pass: succeeded op=%s owner=%s",
                    op,
                    owner_id,
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


async def run_scheduled_worker() -> None:
    """Background worker: poll for due scheduled updates and apply them.

    Runs every 60 seconds indefinitely. Each iteration also runs the
    Paperclip retry pass (every loop) and the Paperclip purge pass
    (once per ``_PURGE_PASS_INTERVAL_SECONDS`` -- 24h).
    """
    logger.info("Scheduled update worker started")
    last_purge_pass_at: float = 0.0  # epoch seconds; 0 forces a run on first iteration
    while True:
        try:
            put_metric("update.scheduled_worker.heartbeat")
            due_updates = await update_repo.get_due_scheduled()
            if due_updates:
                logger.info("Found %d due scheduled updates", len(due_updates))
            for update in due_updates:
                owner_id = update["owner_id"]
                update_id = update["update_id"]
                try:
                    await apply_update(owner_id, update_id)
                except Exception:
                    logger.exception("Error applying scheduled update %s (owner=%s)", update_id, owner_id)

            # T13: Paperclip retry pass — runs every loop so retryable
            # failures recover within a minute of the upstream issue clearing.
            try:
                await _paperclip_provision_retry_pass()
            except Exception:
                logger.exception("paperclip_provision_retry_pass crashed")

            # T13: Paperclip purge pass — runs once per day. The first
            # iteration after process start always runs (last_purge_pass_at=0.0
            # is the sentinel; time.monotonic() is process uptime, which on a
            # fresh container is small so the delta-check alone wouldn't
            # trigger).
            now = time.monotonic()
            if last_purge_pass_at == 0.0 or now - last_purge_pass_at >= _PURGE_PASS_INTERVAL_SECONDS:
                try:
                    await _paperclip_purge_pass()
                except Exception:
                    logger.exception("paperclip_purge_pass crashed")
                last_purge_pass_at = now
        except Exception:
            put_metric("update.scheduled_worker.error")
            logger.exception("Error in scheduled update worker loop")

        await asyncio.sleep(60)
