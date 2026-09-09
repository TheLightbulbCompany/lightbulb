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

Plus the P0c owner-day rollup pass (``_rollup_pass``) — once per hour,
refresh today+yesterday rollup rows for every owner with a bound company
Stripe account (``rollup_service.refresh_owner_day``).
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from core.observability.metrics import put_metric
from core.repositories import billing_repo, container_repo, update_repo
from core.dynamodb import utc_now_iso
from core.services.catalog_slice import REMOVED_AGENT_ENTRY_KEYS
from core.services import config_schema
from core.services.config_patcher import patch_openclaw_config, strip_removed_agent_keys
from core.services.friday_identity import heal_seeded_files

logger = logging.getLogger(__name__)

# Canonical Paperclip update_type marker. Single source of truth: both
# ``routers/webhooks.py`` (which enqueues retries) and this module
# (which consumes them) import the same constant. If this string ever
# changes, the retry pass below would silently stop picking up rows
# the webhook is still writing, so keeping it in one file forever is
# the right amount of paranoia. Public name (no underscore) since both
# the webhook router and tests import it.
PAPERCLIP_RETRY_KIND = "paperclip_provision"
TASK_DEFINITION_REFRESH_KIND = "task_definition_refresh"
TASK_DEFINITION_REFRESH_CHANGE = "refresh_from_base"
# Any changes.* key that re-registers the per-owner ECS task definition. Two
# in-flight updates carrying any of these race on the task def (last writer
# wins image/size), so queueing and force-apply both treat a row carrying any
# of them as a conflict. Shared with routers/admin.py — keep the sets in sync
# by importing this, not by copying it.
TASKDEF_ROLL_CHANGE_KEYS = ("new_image", "new_cpu", "new_memory", TASK_DEFINITION_REFRESH_CHANGE)

# Daily purge pass cadence, in seconds.
_PURGE_PASS_INTERVAL_SECONDS = 24 * 60 * 60

# Hourly owner-day rollup pass cadence, in seconds (P0c).
_ROLLUP_PASS_INTERVAL_SECONDS = 60 * 60

# Hourly cron-run telemetry sweep cadence, in seconds. Must stay under
# cron_run_telemetry._LOOKBACK_S so a skipped tick leaves no hole.
_CRON_RUNS_PASS_INTERVAL_SECONDS = 60 * 60

# Inter-iteration wait for run_scheduled_worker, in seconds.
_WORKER_INTERVAL_S = 60

# A refresh normally checkpoints every 30 seconds while its ECS waiter is
# active. Three missed checkpoints mean the backend worker is gone and another
# worker may take over the same durable target.
_APPLY_HEARTBEAT_S = 30
_APPLY_STALE_S = 3 * _APPLY_HEARTBEAT_S
# Stale refresh rows reclaimed per worker tick (see the recovery pass).
_RECOVERY_MAX_PER_TICK = 3

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


async def queue_image_update(
    owner_id: str,
    new_image: str,
    description: str | None = None,
    *,
    force_by: str | None = None,
) -> dict:
    """Route an accepted image target through the recoverable release journal."""

    from core.services.openclaw_release import release_by_image

    release = release_by_image(new_image)
    desc = description or f"Image update to {new_image}"
    item = await update_repo.create(
        owner_id=owner_id,
        update_type="release",
        description=desc,
        changes={"target_release_id": release.release_id},
        force_by=force_by,
    )
    logger.info("Queued release for owner=%s release=%s", owner_id, release.release_id)
    return item


async def queue_fleet_image_update(new_image: str, description: str | None = None) -> int:
    """Queue an image update for every owner in the billing-accounts table.

    Scans the billing-accounts DynamoDB table and creates one pending update per owner.
    Returns the count of updates created.
    """
    from core.dynamodb import get_table, run_in_thread
    from core.services.openclaw_release import release_by_image

    # Validate once even for an empty fleet; an empty registry is inert rather
    # than reporting that an unqualified rollout was queued successfully.
    release_by_image(new_image)

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


async def queue_fleet_task_definition_refresh(description: str | None = None) -> tuple[int, int]:
    """Schedule a durable, sequential base-clone refresh for every owner.

    The update worker processes due rows one at a time. Each apply waits for
    the owner's ECS service to become stable, so this is a bounded fleet roll
    rather than a burst of detached deployments. Existing task-definition
    updates are skipped to preserve the per-owner single-roll invariant.
    """

    rows = await container_repo.scan_all()
    scheduled_at = utc_now_iso()
    queued = skipped = 0
    for row in rows:
        item = await queue_task_definition_refresh_for_owner(row, description=description, scheduled_at=scheduled_at)
        if item is None:
            skipped += 1
        else:
            queued += 1

    logger.info("Scheduled task-definition refresh for %d owners (%d skipped)", queued, skipped)
    return queued, skipped


async def queue_task_definition_refresh_for_owner(
    row: dict,
    *,
    description: str | None = None,
    scheduled_at: str | None = None,
) -> dict | None:
    """Queue one owner's base-clone refresh row, or None when ineligible.

    Ineligible means the container row is incomplete, mid-provision, or an
    update carrying any task-definition-rolling change is already in flight
    (the per-owner single-roll invariant). Callers: the fleet queue above and
    the single-owner ``POST /container/updates`` path.
    """
    owner_id = row.get("owner_id")
    status = row.get("status")
    complete = all(row.get(field) for field in ("owner_id", "service_name", "access_point_id", "task_definition_arn"))
    unclaimed_error = (
        status == "error" and not row.get("provision_claim_id") and not row.get("provision_execution_name")
    )
    if not complete or (status not in {"running", "stopped"} and not unclaimed_error) or row.get("needs_provision"):
        return None
    in_flight = await update_repo.get_in_flight(owner_id)
    if any(
        isinstance(update.get("changes"), dict) and any(key in update["changes"] for key in TASKDEF_ROLL_CHANGE_KEYS)
        for update in in_flight
    ):
        return None
    return await update_repo.create(
        owner_id=owner_id,
        update_type=TASK_DEFINITION_REFRESH_KIND,
        description=description or "Refresh task definition for config-lock recovery",
        changes={TASK_DEFINITION_REFRESH_CHANGE: True},
        scheduled_at=scheduled_at or utc_now_iso(),
    )


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


async def reapply_owner_config(owner_id: str, *, deployed_tag: str | None = None) -> dict:
    """Regenerate an owner's ``openclaw.json`` on EFS from the current config
    generator, preserving runtime-appended state.

    ``deployed_tag`` is the tag of the image that will READ the regenerated
    config, for fork-level-gated keys. An image roll passes the image it is
    rolling TO; the admin Regenerate button and the ops script leave it
    ``None`` so the owner's registered task definition is used. A qualified
    owner always keeps its active release tag and schema instead of inheriting
    a mutable fleet-image request.

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

    container = await container_repo.get_by_owner_id(owner_id, consistent=True)
    if not container:
        raise LookupError(f"No container row for owner {owner_id} — nothing to reapply")

    active_release_id = container.get("active_release_id")
    if active_release_id is not None:
        if container.get("desired_release_id") != active_release_id or container.get("release_update_id") is not None:
            raise ValueError("owner release assignment changed before config regeneration")
        from core.services.openclaw_release import release_by_id

        deployed_tag = release_by_id(active_release_id).image.rpartition(":")[2]

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
        deployed_tag=deployed_tag,
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

    # _runtime_only_delta preserves whatever the regen did not emit. Within one
    # upstream version that is exactly right — anything the generator skips is
    # runtime state (identity, bindings). Across an upstream BUMP the same rule
    # also re-adds every key the new schema DELETED, because "the generator
    # stopped emitting it" describes both. Measured 7.2 -> 8.1: the restore
    # brought back root `audit`/`web` and, via the co-owned recursion,
    # `skills.workshop.autonomous.enabled` — turning a clean regen (0 schema
    # errors) back into a crash-loop (2). Drop whatever the deployed schema
    # rejects; runtime state is unaffected because it is schema-valid.
    restore_patch = config_schema.prune_schema_rejected(fresh, restore_patch)

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


async def _heartbeat_apply_claim(
    owner_id: str,
    update_id: str,
    claim_id: str,
    stop: asyncio.Event,
) -> None:
    """Keep a long ECS wait owned; stop if a successor takes the claim."""

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_APPLY_HEARTBEAT_S)
        except asyncio.TimeoutError:
            try:
                held = await update_repo.heartbeat_apply(owner_id, update_id, claim_id)
            except Exception:
                logger.exception(
                    "Apply heartbeat failed (owner=%s update=%s)",
                    owner_id,
                    update_id,
                )
                return
            if not held:
                logger.warning(
                    "Apply claim moved while heartbeat was active (owner=%s update=%s)",
                    owner_id,
                    update_id,
                )
                return


async def _apply_claimed_update(update: dict, claim_id: str) -> bool:
    """Apply a row already fenced by ``claim_id``."""

    owner_id = update["owner_id"]
    update_id = update["update_id"]
    if not await update_repo.claim_ordinary_owner(owner_id, update_id, claim_id):
        await update_repo.finish_claimed_apply(
            owner_id,
            update_id,
            claim_id,
            status="failed",
            failure_reason="owner is reserved by another runtime mutation",
        )
        return False
    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat_apply_claim(owner_id, update_id, claim_id, heartbeat_stop))

    async def fence_effect() -> None:
        if not await update_repo.assert_ordinary_owner(owner_id, update_id, claim_id):
            raise RuntimeError("ordinary update owner claim moved before effect")

    try:
        changes = update.get("changes", {})
        update_type = update.get("update_type", "")

        # Image mutations are never ordinary updates. Registered targets are
        # queued as release rows above; legacy or hand-written rows fail before
        # touching config, ECS, or EFS.
        if changes.get("new_image") is not None:
            raise ValueError("image changes require a registered OpenClaw release")

        # Step 2: apply config patch if present
        config_patch = changes.get("config_patch")
        if config_patch:
            await fence_effect()
            await patch_openclaw_config(owner_id, config_patch)
            logger.info("Applied config patch for update %s (owner=%s)", update_id, owner_id)

        # Step 3: apply ECS changes if present
        new_image = changes.get("new_image")
        new_cpu = changes.get("new_cpu")
        new_memory = changes.get("new_memory")

        refresh_from_base = changes.get(TASK_DEFINITION_REFRESH_CHANGE) is True
        if refresh_from_base and (new_image or new_cpu or new_memory):
            raise ValueError("refresh_from_base cannot be combined with image or size changes")

        if refresh_from_base or new_image or new_cpu or new_memory:
            from core.containers import get_ecs_manager

            ecs = get_ecs_manager()

            if refresh_from_base:
                target_task_definition_arn = update.get("target_task_definition_arn")
                if not target_task_definition_arn:
                    await fence_effect()
                    target_task_definition_arn = await ecs.prepare_config_lock_protocol_task_definition(
                        owner_id,
                        _effect_fence=fence_effect,
                    )
                    stored = await update_repo.set_apply_target(
                        owner_id,
                        update_id,
                        claim_id,
                        target_task_definition_arn,
                    )
                    if not stored:
                        logger.warning(
                            "Prepared task definition %s but apply claim moved (owner=%s update=%s)",
                            target_task_definition_arn,
                            owner_id,
                            update_id,
                        )
                        return False

                # This is the last ownership checkpoint before ECS. If a
                # takeover races after it, both workers still set the SAME
                # target without forceNewDeployment, so the remote mutation is
                # idempotent and terminal DDB writes remain fenced.
                if not await update_repo.heartbeat_apply(owner_id, update_id, claim_id):
                    return False
                await fence_effect()
                await ecs.rollout_config_lock_protocol(
                    owner_id,
                    target_task_definition_arn=target_task_definition_arn,
                    _effect_fence=fence_effect,
                )
                logger.info(
                    "Task-definition refresh reached ECS stability for owner=%s (update=%s target=%s)",
                    owner_id,
                    update_id,
                    target_task_definition_arn,
                )
            else:
                # An image roll used to reuse the container's existing on-disk
                # openclaw.json and merely STRIP keys the new image's strict
                # schema no longer accepts. That worked while every roll was
                # same-upstream (2026.7.2-isol8.N -> isol8.M, identical
                # schema); it does not survive an upstream bump.
                # migrate_removed_config_keys only reaches inside agents.*,
                # and going 7.2 -> 8.1 leaves 12 rejected keys, 10 of them at
                # the root (audit, web, cron.store, memory.backend,
                # models.pricing, hooks.maxBodyBytes, gateway.nodes.
                # allowCommands, browser.localLaunchTimeoutMs /
                # localCdpReadyTimeoutMs / profiles.*.color / tabCleanup.*).
                # Both schema roots are additionalProperties:false, so that is
                # a boot crash-loop, not a degradation.
                #
                # Regenerate instead of scrubbing: the generator is already
                # version-aware, so it emits whatever shape the pinned image
                # wants without anyone maintaining a removal blacklist -- the
                # blacklist is what rotted here. reapply_owner_config restores
                # the runtime-appended subtrees the regen drops (channels /
                # skills / mcp, plus anything the generator never emits such
                # as identity and bindings) via _runtime_only_delta.
                if new_image:
                    await fence_effect()
                    try:
                        # deployed_tag is the image that will READ this config,
                        # i.e. the one we are rolling TO -- NOT the owner's
                        # current task-def, which is what reapply_owner_config
                        # looks up when this is omitted. Omitting it inverts
                        # every fork-level gate on the one write that exists to
                        # satisfy the new image: an owner on isol8.6 rolling to
                        # 8.1 would have browser.sessionState gated OFF by the
                        # image being replaced. It would have looked correct on
                        # any owner already at the pin and been silently wrong
                        # on the 20 prod owners that lag it.
                        await reapply_owner_config(owner_id, deployed_tag=new_image.rpartition(":")[2])
                    except (LookupError, ValueError):
                        # Legacy rows (no gateway_token) cannot be regenerated
                        # -- reapply_owner_config is the same code the admin
                        # Regenerate button 409s them from. Fall back to the
                        # historical strip so a SAME-upstream roll keeps
                        # working for them exactly as before; they need a full
                        # reprovision to cross an upstream bump either way.
                        logger.warning(
                            "update_service: config regen unavailable for owner=%s, falling back to key strip",
                            owner_id,
                        )
                        await fence_effect()
                        await migrate_removed_config_keys(owner_id)

                    # Carry redaction rules onto already-provisioned containers.
                    # Nothing else does: the strip above only REMOVES keys, and
                    # replace_provider_block rebuilds provider/plugin subtrees but
                    # never `logging`. Without this an existing owner keeps the
                    # playbook that reads their Paperclip key while never gaining
                    # the pattern that redacts it — the cohort that needs it most.
                    # Additive and behaviour-free, so it is safe on every owner.
                    from core.containers.config import REDACT_PATTERNS, memory_plugin_entries
                    from core.services.config_patcher import (
                        ensure_plugin_config_keys,
                        ensure_plugin_entries,
                        ensure_redact_patterns,
                    )
                    from core.services.paperclip_credential_rule import PAPERCLIP_CREDS_PLUGIN_PATCH

                    await fence_effect()
                    try:
                        await ensure_redact_patterns(owner_id, REDACT_PATTERNS)
                    except Exception:  # noqa: BLE001 — never block a roll on a safety-net patch
                        logger.exception("update_service: redact-pattern patch failed for owner=%s", owner_id)

                    # Enable the credential plugin on the way in. The roll bakes
                    # the extension but reuses the on-disk config, so without this
                    # an existing owner carries a plugin that never activates —
                    # and any later path that writes the new playbook (a company
                    # import, a marketplace hire) would be claiming credentials
                    # are in the environment when nothing puts them there.
                    # Enabling it alone is harmless: an agent still reading the
                    # key file simply has the credentials twice.
                    # memory-wiki rides along for the same reason: the plugin is
                    # baked in the base image but a roll reuses the on-disk
                    # config, so pre-existing owners would never gain the shared
                    # company-brain vault. ensure_plugin_entries is additive-only,
                    # so an owner who already configured memory-wiki keeps theirs.
                    await fence_effect()
                    try:
                        await ensure_plugin_entries(
                            owner_id,
                            {
                                **PAPERCLIP_CREDS_PLUGIN_PATCH["entries"],
                                "memory-wiki": memory_plugin_entries()["memory-wiki"],
                            },
                        )
                    except Exception:  # noqa: BLE001 — never block a roll on a safety-net patch
                        logger.exception("update_service: plugin-enable patch failed for owner=%s", owner_id)

                    # The bridge keys are what FILL the brain — memory-core's
                    # artifacts only cross into the wiki when vaultMode/bridge
                    # are set. On the regen path above, reapply_owner_config
                    # already wrote them: `plugins` is generator-owned wholesale
                    # (_runtime_only_delta), so this is a no-op there. It exists
                    # for the fallback path — legacy rows regen cannot reach —
                    # where ensure_plugin_entries is additive at the ENTRY level
                    # and skips every owner who already has a memory-wiki entry:
                    # exactly the cohort provisioned before those keys existed.
                    # No product surface lets an owner opt OUT of bridge mode,
                    # so "preserve an explicit isolated choice" is not a contract
                    # either path makes; the deep-fill's never-overwrite rule is
                    # about not clobbering unrelated keys, not about opt-outs.
                    await fence_effect()
                    try:
                        await ensure_plugin_config_keys(
                            owner_id,
                            "memory-wiki",
                            memory_plugin_entries()["memory-wiki"]["config"],
                        )
                    except Exception:  # noqa: BLE001 — never block a roll on a safety-net patch
                        logger.exception("update_service: memory-wiki config patch failed for owner=%s", owner_id)

                    # Image rolls are the fleet's routine touch-point, but they
                    # never rewrite configs — so the seeded-file heal pass that
                    # rides the provision/config-rewrite path would otherwise
                    # never reach an already-provisioned Friday. Run it here so a
                    # promotion carries playbook fixes fleet-wide. Best-effort by
                    # contract (never raises).
                    from core.containers import get_workspace

                    await fence_effect()
                    await heal_seeded_files(owner_id=owner_id, workspace=get_workspace())

                await fence_effect()
                await ecs.resize_user_container(
                    user_id=owner_id,
                    new_cpu=new_cpu,
                    new_memory=new_memory,
                    new_image=new_image,
                    _effect_fence=fence_effect,
                )
                logger.info(
                    "ECS update applied for owner=%s image=%s cpu=%s mem=%s (update=%s)",
                    owner_id,
                    new_image,
                    new_cpu,
                    new_memory,
                    update_id,
                )

        marked = await update_repo.finish_claimed_apply(
            owner_id,
            update_id,
            claim_id,
            status="applied",
        )
        if not marked:
            logger.warning(
                "Update %s changes took effect but its apply claim moved (owner=%s, type=%s)",
                update_id,
                owner_id,
                update_type,
            )
        else:
            logger.info("Update %s applied successfully (owner=%s, type=%s)", update_id, owner_id, update_type)
        return True

    except Exception as exc:
        logger.exception("Failed to apply update %s (owner=%s)", update_id, owner_id)
        try:
            await update_repo.finish_claimed_apply(
                owner_id,
                update_id,
                claim_id,
                status="failed",
                failure_reason=str(exc),
            )
        except Exception:
            logger.exception("Failed to mark update %s as failed", update_id)
        return False
    finally:
        heartbeat_stop.set()
        await heartbeat_task
        await update_repo.release_ordinary_owner(owner_id, update_id, claim_id)


async def apply_update(owner_id: str, update_id: str) -> bool:
    """Apply a single pending update.

    1. Conditionally set status to 'applying' (prevents double-apply).
    2. Apply config patch if present.
    3. Apply ECS changes if present (resize_user_container).
    4. Mark as applied on success, failed on error.

    Returns True if applied, False if already being applied or failed precondition.
    """
    claim_id = uuid.uuid4().hex
    update = await update_repo.claim_apply(owner_id, update_id, claim_id)
    if update is not None:
        return await _apply_claimed_update(update, claim_id)

    candidate = await update_repo.get_by_id(owner_id, update_id, consistent=True)
    if candidate and candidate.get("update_type") == "release":
        from core.services.openclaw_release_executor import apply_owner_release

        return await apply_owner_release(owner_id, update_id)
    logger.warning("Could not acquire update %s for owner %s (already applying?)", update_id, owner_id)
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


async def _recover_stale_task_definition_refreshes() -> None:
    """Resume abandoned fleet refreshes from their durable ECS target."""

    stale_before = int(time.time()) - _APPLY_STALE_S
    updates = await update_repo.list_stale_applying(
        TASK_DEFINITION_REFRESH_KIND,
        stale_before_epoch=stale_before,
    )
    # Each reclaimed row waits for ECS stability (bounded at ~15 min), so an
    # unbounded batch could absorb the worker for hours. Recover a few per
    # tick; the rest stay stale and are picked up on later ticks.
    if len(updates) > _RECOVERY_MAX_PER_TICK:
        logger.warning(
            "Deferring %d stale task-definition refreshes to later worker ticks",
            len(updates) - _RECOVERY_MAX_PER_TICK,
        )
        updates = updates[:_RECOVERY_MAX_PER_TICK]
    for update in updates:
        changes = update.get("changes") or {}
        if changes.get(TASK_DEFINITION_REFRESH_CHANGE) is not True:
            continue
        owner_id = update.get("owner_id")
        update_id = update.get("update_id")
        if not owner_id or not update_id:
            continue
        claim_id = uuid.uuid4().hex
        claimed = await update_repo.claim_stale_apply(
            owner_id,
            update_id,
            claim_id,
            stale_before_epoch=stale_before,
        )
        if claimed is None:
            continue
        logger.warning(
            "Reclaimed stale task-definition refresh (owner=%s update=%s target=%s)",
            owner_id,
            update_id,
            claimed.get("target_task_definition_arn") or "unprepared",
        )
        await _apply_claimed_update(claimed, claim_id)


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


async def run_scheduled_worker() -> None:
    """Background worker: poll for due scheduled updates and apply them.

    Runs every 60 seconds indefinitely. Each iteration also runs the
    Paperclip retry pass (every loop), the Paperclip purge pass
    (once per ``_PURGE_PASS_INTERVAL_SECONDS`` -- 24h), the owner-day
    rollup pass (once per ``_ROLLUP_PASS_INTERVAL_SECONDS`` -- 1h), and the
    cron-run telemetry sweep (once per
    ``_CRON_RUNS_PASS_INTERVAL_SECONDS`` -- 1h).
    """
    logger.info("Scheduled update worker started")
    global _worker_stop
    _worker_stop = asyncio.Event()  # bind to the loop running this worker
    last_purge_pass_at: float = 0.0  # epoch seconds; 0 forces a run on first iteration
    last_rollup_pass_at: float = 0.0  # same sentinel idiom as the purge pass
    last_cron_runs_pass_at: float = 0.0  # same sentinel idiom as the purge pass
    while not _worker_stop.is_set():
        try:
            put_metric("update.scheduled_worker.heartbeat")
            # Isolated like the passes below: a persistent recovery failure
            # (e.g. a scan error) must not starve the due-update or Paperclip
            # passes for the life of the process.
            try:
                await _recover_stale_task_definition_refreshes()
            except Exception:
                logger.exception("task-definition refresh recovery pass crashed")
            try:
                from core.services.openclaw_release_executor import recover_stale_owner_releases

                await recover_stale_owner_releases()
            except Exception:
                logger.exception("owner release recovery pass crashed")
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

            # P0c: owner-day rollup pass — runs once per hour, same
            # monotonic-interval idiom as the purge pass above.
            if last_rollup_pass_at == 0.0 or now - last_rollup_pass_at >= _ROLLUP_PASS_INTERVAL_SECONDS:
                try:
                    await _rollup_pass()
                except Exception:
                    logger.exception("rollup_pass crashed")
                last_rollup_pass_at = now

            # Cron-run telemetry sweep -- once per hour, same monotonic-interval
            # idiom. Recovers agent_run_completed for container-native cron runs,
            # which finish with no live socket for either emit path to see.
            if last_cron_runs_pass_at == 0.0 or now - last_cron_runs_pass_at >= _CRON_RUNS_PASS_INTERVAL_SECONDS:
                try:
                    from core.services.cron_run_telemetry import sweep_cron_runs

                    await sweep_cron_runs()
                except Exception:
                    logger.exception("cron_run_telemetry sweep crashed")
                last_cron_runs_pass_at = now
        except Exception:
            put_metric("update.scheduled_worker.error")
            logger.exception("Error in scheduled update worker loop")

        # Interruptible inter-iteration wait: wakes immediately on stop request.
        try:
            await asyncio.wait_for(_worker_stop.wait(), timeout=_WORKER_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
