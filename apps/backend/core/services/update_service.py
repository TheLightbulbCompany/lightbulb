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
from core.services.catalog_slice import REMOVED_AGENT_ENTRY_KEYS
from core.services.config_patcher import patch_openclaw_config, strip_removed_agent_keys

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


def reset_worker_stop() -> None:
    """Clear the stop signal (test isolation). The next run_scheduled_worker
    creates a fresh, loop-bound Event."""
    global _worker_stop
    _worker_stop = None


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


# Keys upstream removed from the strict OpenClaw schema that an older
# on-disk openclaw.json may still carry. A container rolled to a newer
# image reuses its existing EFS config, and a removed key the new image's
# strict schema rejects crash-loops the gateway at boot. embeddedHarness was
# dropped from agents.defaults; the AgentEntrySchema set (embeddedHarness,
# embeddedPi, agentRuntime, systemPromptOverride) can also persist under
# existing agents.list[] entries that an older marketplace deploy file-merged
# verbatim — so both surfaces must be scrubbed before the roll.
_REMOVED_DEFAULTS_KEYS: tuple[str, ...] = ("embeddedHarness",)


async def migrate_removed_config_keys(owner_id: str) -> None:
    """Strip removed-upstream keys from an owner's on-disk openclaw.json.

    Run before rolling a container to a newer image so a key the new image's
    strict schema no longer accepts cannot crash-loop the gateway — both the
    dropped agents.defaults key and any removed AgentEntrySchema key still
    persisted under an existing agents.list[] entry (e.g. from an older
    marketplace deploy). Idempotent and safe to call on any owner.
    """
    await strip_removed_agent_keys(
        owner_id,
        defaults_keys=_REMOVED_DEFAULTS_KEYS,
        entry_keys=REMOVED_AGENT_ENTRY_KEYS,
    )


# Top-level subtrees that runtime writers CO-OWN with the config generator:
# the generator emits the subtree's skeleton, but other writers add entries
# inside it after provision that the generator does not know about —
# channel pairing accounts/allowFrom (channel_link_service), marketplace
# skill entries/allowBundled (deploy's skills_patch), deployed-agent MCP
# servers (catalog deploy slice). Only these subtrees get the recursive
# "preserve what the regen omits" treatment. Everywhere else a fresh render
# WINS WHOLESALE: recursing into arbitrary generator-owned dicts would
# resurrect stale fields the current generator deliberately no longer emits
# (e.g. the pre-per-provider Titan ``memorySearch.model``) — the exact stale
# state a regen exists to clear.
_RUNTIME_CO_OWNED_SUBTREES: frozenset[str] = frozenset({"channels", "skills", "mcp"})


def _runtime_only_delta(existing: dict | None, fresh: dict | None) -> dict:
    """Diff ``existing`` against a freshly regenerated ``fresh`` config and
    return only the runtime-appended state the regen dropped.

    The config generator (``build_openclaw_config_dict``) emits an
    authoritative config for every key it knows about, but some
    openclaw.json state is written at runtime AFTER provision and a full
    overwrite drops it. Two shapes of that state exist, with different
    rules:

    * **Top-level keys the generator never emits** (boot-time ``identity``,
      ``bindings`` on configs where the generator did not re-inject them)
      are preserved whole — absent from ``fresh`` means the regen does not
      own them.
    * **Entries inside co-owned subtrees** (``_RUNTIME_CO_OWNED_SUBTREES``:
      ``channels`` pairing accounts, marketplace ``skills`` entries,
      deployed-agent ``mcp`` servers) are preserved via full recursion —
      within those subtrees, anything the regen omits is runtime state.

    Every other key present in ``fresh`` is owned by the regen WHOLESALE —
    no recursion. Recursing into arbitrary generator-owned dicts would
    resurrect stale nested fields the current generator deliberately
    stopped emitting (a removed ``memorySearch.model``, a dropped
    plugin/feature block), which is the exact stale state a regen exists to
    clear. The trade-off is explicit: a NEW runtime-only surface nested
    under a generator-owned subtree needs adding to
    ``_RUNTIME_CO_OWNED_SUBTREES`` to survive a regen.

    Because the delta contains only keys the regen did not set, deep-merging
    it back over the fresh config can never revert a regenerated value.
    Lists are never descended into (the regen rebuilds e.g. ``agents.list``
    wholesale). Returns an empty dict when nothing runtime-only was dropped.
    """
    if not isinstance(existing, dict) or not isinstance(fresh, dict):
        return {}

    delta: dict = {}
    for key, existing_value in existing.items():
        if key not in fresh:
            # The regen does not emit this key at all — preserve it whole.
            delta[key] = existing_value
            continue
        fresh_value = fresh[key]
        if key in _RUNTIME_CO_OWNED_SUBTREES and isinstance(existing_value, dict) and isinstance(fresh_value, dict):
            nested = _co_owned_delta(existing_value, fresh_value)
            if nested:
                delta[key] = nested
        # else: the regen owns this key wholesale — exclude it so the
        # restore can't revert or pollute the fresh value.
    return delta


def _co_owned_delta(existing: dict, fresh: dict) -> dict:
    """Unrestricted recursive diff used INSIDE co-owned subtrees: preserve
    every nested key the regen omits (pairing accounts at any depth, skill
    entries, MCP server blocks)."""
    delta: dict = {}
    for key, existing_value in existing.items():
        if key not in fresh:
            delta[key] = existing_value
            continue
        fresh_value = fresh[key]
        if isinstance(existing_value, dict) and isinstance(fresh_value, dict):
            nested = _co_owned_delta(existing_value, fresh_value)
            if nested:
                delta[key] = nested
    return delta


async def reapply_owner_config(owner_id: str) -> dict:
    """Regenerate an owner's ``openclaw.json`` on EFS from the current config
    generator, preserving runtime-appended state.

    Existing containers keep their PROVISION-TIME config on EFS; a backend
    deploy that changes the generator does not touch it. This pushes the
    regenerated config onto an existing owner via the SAME write path
    provisioning uses (``ecs_manager.write_user_configs`` — which resolves
    the live Paperclip agent list and connected integration MCP servers),
    then deep-diffs the pre-regen snapshot against what landed and re-merges
    only the subtrees the regen dropped (channel pairing accounts, route
    bindings, boot-time identity, and any future runtime-only subtree).

    Config-only: does not restart the task. A running container picks the
    file up via OpenClaw's watcher; changes that need a gateway restart to
    activate (e.g. plugin enablement) land on the next task roll.

    Raises ``LookupError`` when the owner has no container row, and
    ``ValueError`` for legacy rows without a ``gateway_token`` (minting a
    fresh token here would diverge from the token baked into the running
    task-def and break gateway auth — that path needs a full reprovision).
    """
    # Module-attribute imports at call time: dodges import cycles and keeps
    # these seams patchable at their source modules (the reapply script's
    # tests patch them there and exercise this function through the CLI).
    from core.containers import get_ecs_manager
    from core.containers import config as container_config
    from core.repositories import container_repo
    from core.services import config_patcher, container_provision

    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        raise LookupError(f"No container row for owner {owner_id} — nothing to reapply")

    gateway_token = container.get("gateway_token")
    if not gateway_token:
        raise ValueError(
            f"Container row for {owner_id} has no gateway_token — refusing to "
            "reapply (a fresh token would diverge from the running task-def; "
            "run a full reprovision instead)"
        )

    hook_token = container.get("hook_token")
    owner_type = container.get("owner_type") or "personal"
    # Per-owner AIP map, persisted on the containers row at provision time
    # (same pattern as gateway_token/hook_token). The generator hard-requires
    # it on both Bedrock paths; without threading it through, every
    # bedrock_claude/bedrock_trial owner would fail the regen. Read-only
    # here — minting AIPs (ensure_owner_aips) is the provisioning path's job.
    aip_arns = container.get("bedrock_aip_arns")
    provider_choice, byo_provider = await container_provision.resolve_provider_choice(owner_id)

    # Snapshot the FULL on-disk config BEFORE the regen overwrites it, so the
    # runtime-only subtrees the fresh render drops can be recovered.
    existing = await container_config.read_openclaw_config_from_efs(owner_id)

    await get_ecs_manager().write_user_configs(
        owner_id,
        gateway_token,
        provider_choice=provider_choice,
        byo_provider=byo_provider,
        hook_token=hook_token,
        owner_type=owner_type,
        aip_arns=aip_arns,
    )

    fresh = await container_config.read_openclaw_config_from_efs(owner_id)
    restore_patch = _runtime_only_delta(existing, fresh)

    # The trial generator DELIBERATELY omits carried-forward channel accounts
    # and their route bindings (trial containers must not re-arm channel
    # bots). Restoring them here would undo that lockdown — mirror the
    # generator's own conditional and drop both from the restore.
    if provider_choice == container_config.BEDROCK_TRIAL_CHOICE:
        restore_patch.pop("channels", None)
        restore_patch.pop("bindings", None)

    if restore_patch:
        await config_patcher.patch_openclaw_config(owner_id, restore_patch)

    restored = sorted(restore_patch.keys())
    logger.info(
        "Reapplied openclaw.json for owner=%s provider=%s restored_subtrees=%s",
        owner_id,
        provider_choice,
        restored or "-",
    )
    return {
        "owner_id": owner_id,
        "provider_choice": provider_choice,
        "byo_provider": byo_provider,
        "restored_subtrees": restored,
    }


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
        # Strongly consistent: we just conditionally flipped this row to
        # "applying", and callers can create-then-immediately-apply (admin
        # force-apply), so an eventually-consistent read could miss the row we
        # just wrote — which would return False here while leaving it stuck in
        # "applying" and blocking the owner's future rolls.
        response = await run_in_thread(
            table.query,
            KeyConditionExpression=Key("owner_id").eq(owner_id) & Key("update_id").eq(update_id),
            ConsistentRead=True,
        )
        items = response.get("Items", [])
        if not items:
            # Should be unreachable after the strongly-consistent read above, but
            # if it ever happens the row was already flipped to "applying" — clear
            # it back to a terminal state so it can't wedge the owner's guard.
            logger.error("Update %s not found after acquiring lock (owner=%s)", update_id, owner_id)
            await update_repo.set_status_conditional(owner_id, update_id, "failed", ["applying"])
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

            ecs = get_ecs_manager()

            # An image roll reuses the container's existing on-disk
            # openclaw.json. Strip keys the new image's strict schema no
            # longer accepts so the rolled gateway doesn't crash-loop at
            # boot. No-op on a config already free of them.
            if new_image:
                await migrate_removed_config_keys(owner_id)

            # An image-only roll carries NO size — queue_image_update /
            # queue_fleet_image_update write {"new_image": X} only. Passing the
            # resulting None/None straight through would default the re-register
            # to the standard PER_USER_* box and silently shrink an Ultra Pro
            # owner's container on every routine image bump / fleet-force-apply
            # — the exact revert plan-derived sizing exists to prevent. Fill the
            # gap from the owner's plan ONLY when the change specifies neither
            # dimension; a real resize (explicit cpu/memory) is left untouched.
            if new_cpu is None and new_memory is None:
                new_cpu, new_memory = await ecs._size_for_owner(owner_id)

            await ecs.resize_user_container(
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

        # Step 4: mark as applied. The ECS/config changes above already took
        # effect, so the apply itself succeeded regardless of this flip — but if
        # the status write is rejected, surface it rather than silently leaving
        # the row stuck in "applying" (the invisible-failure trap this hit when
        # mark_applied's condition did not accept the "applying" state).
        marked = await update_repo.mark_applied(owner_id, update_id)
        if not marked:
            logger.warning(
                "Update %s changes took effect but status was not flipped to "
                "'applied' (owner=%s, type=%s); row left in 'applying'",
                update_id,
                owner_id,
                update_type,
            )
        else:
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


async def run_scheduled_worker() -> None:
    """Background worker: poll for due scheduled updates and apply them.

    Runs every 60 seconds indefinitely. Each iteration also runs the
    Paperclip retry pass (every loop) and the Paperclip purge pass
    (once per ``_PURGE_PASS_INTERVAL_SECONDS`` -- 24h).
    """
    logger.info("Scheduled update worker started")
    global _worker_stop
    _worker_stop = asyncio.Event()  # bind to the loop running this worker
    last_purge_pass_at: float = 0.0  # epoch seconds; 0 forces a run on first iteration
    while not _worker_stop.is_set():
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

        # Interruptible inter-iteration wait: wakes immediately on stop request.
        try:
            await asyncio.wait_for(_worker_stop.wait(), timeout=_WORKER_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
