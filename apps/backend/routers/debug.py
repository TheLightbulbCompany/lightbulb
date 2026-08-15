"""
Dev-only container provisioning endpoints.

Bypasses Stripe for local testing — served only in dev/local/test
(see ``assert_debug_enabled``), refused everywhere else.
"""

import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user, get_owner_type, require_org_admin, resolve_owner_id
from core.config import settings
from core.observability.metrics import put_metric
from core.containers import get_ecs_manager, get_workspace
from core.containers.ecs_manager import EcsManagerError
from core.repositories import (
    billing_repo,
    channel_link_repo,
    container_repo,
    integrations_repo,
    update_repo,
    usage_repo,
    user_repo,
)
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import connection_service
from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)

router = APIRouter()


# ALLOWLIST, not a blacklist. These endpoints bypass Stripe, wipe a tenant's
# data and probe EFS, so they may exist only in a developer sandbox — any
# ENVIRONMENT value we don't recognise as one is refused. The old
# `== "prod"` test failed OPEN for every value nobody remembered to add:
# `staging` is a legal deployed value (core/config.py _DEPLOYED_ENVIRONMENTS)
# and would have exposed the whole /debug surface the day a staging stage
# was created. Empty = unset = local dev / CI.
_DEBUG_ENVIRONMENTS = frozenset({"dev", "local", "test", ""})


def assert_debug_enabled(endpoint: str = "debug") -> None:
    """Raise 403 unless ENVIRONMENT is a developer sandbox.

    The single gate for every /debug route. Routes that can use a FastAPI
    dependency go through ``require_non_production``; handlers that need the
    per-endpoint metric label call this directly. Do not re-inline the check.
    """
    if settings.ENVIRONMENT not in _DEBUG_ENVIRONMENTS:
        put_metric("debug.endpoint.prod_hit", dimensions={"endpoint": endpoint})
        raise HTTPException(status_code=403, detail="Not available in this environment")


async def require_non_production() -> None:
    """Route-dependency form of :func:`assert_debug_enabled`."""
    assert_debug_enabled()


@router.post(
    "/provision",
    summary="Provision container (dev only)",
    description=(
        "Manually provisions an ECS Fargate service for the authenticated user. "
        "Only available in non-production environments for local testing."
    ),
    operation_id="debug_provision_container",
    dependencies=[Depends(require_non_production)],
    responses={
        403: {"description": "Not available outside dev/local/test"},
        409: {"description": "Container already running"},
        503: {"description": "Provisioning failed"},
    },
)
async def provision_container(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    owner_type = get_owner_type(auth)

    # Check for existing service
    existing = await container_repo.get_by_owner_id(owner_id)
    if existing and existing.get("status") in ("running", "provisioning"):
        return {
            "status": "already_running",
            "service_name": existing.get("service_name"),
            "owner_id": owner_id,
            "owner_type": owner_type,
        }

    try:
        gateway_token = secrets.token_urlsafe(32)
        ecs_manager = get_ecs_manager()

        # Step 1: Create ECS service (desiredCount=0) — creates access
        # point and EFS dir, but does NOT start the container yet.
        service_name = await ecs_manager.create_user_service(owner_id, gateway_token)

        # Step 2: Write all configs to EFS before the container boots. This
        # is the single source of truth for per-container files — it writes
        # openclaw.json, the node device PEM, and the combined
        # devices/paired.json (with BOTH the node and the operator device
        # entries, KMS-encrypted operator seed persisted to the DynamoDB
        # containers row).
        # provider_choice defaults to bedrock_claude for dev provisioning.
        await ecs_manager.write_user_configs(
            owner_id,
            gateway_token,
            provider_choice="bedrock_claude",
            owner_type=owner_type,
        )

        # Step 3: Now start the container — configs are on EFS.
        # force: this route IS a provision (it just created the service and
        # wrote the configs), and it is a developer tool — 403 in production, so
        # it is not a door a customer can use to wake a capped container.
        await ecs_manager.start_user_service(owner_id, force=True)

        return {
            "status": "provisioned",
            "service_name": service_name,
            "owner_id": owner_id,
            "owner_type": owner_type,
        }
    except EcsManagerError as e:
        logger.error("Dev provision failed for owner %s: %s", owner_id, e)
        raise HTTPException(status_code=503, detail=str(e))


@router.patch(
    "/provision",
    summary="Update config and redeploy (dev only)",
    description=(
        "Rewrites openclaw.json with the latest config template and forces "
        "a new ECS deployment so the gateway picks up the changes."
    ),
    operation_id="debug_redeploy_container",
    dependencies=[Depends(require_non_production)],
    responses={
        403: {"description": "Not available outside dev/local/test"},
        404: {"description": "No container found"},
        503: {"description": "Redeploy failed"},
    },
)
async def redeploy_container(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    owner_type = get_owner_type(auth)

    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        raise HTTPException(status_code=404, detail="No container found")

    try:
        # Under the flat-fee model, openclaw.json is generated wholly from
        # provider_choice — there is no per-tier "backend policy patch" to
        # deep-merge. The dev redeploy path just rewrites the file via
        # write_user_configs and bumps the service.
        ecs_manager = get_ecs_manager()
        # Thread hook_token through so the openclaw.json placeholder lines
        # up with the env-var T2 injects on the per-user task def. Render
        # is the same either way (literal placeholder), but threading
        # keeps the rotate-flow plumbing uniform across provision paths.
        await ecs_manager.write_user_configs(
            owner_id,
            container["gateway_token"],
            provider_choice="bedrock_claude",
            hook_token=container.get("hook_token"),
            owner_type=owner_type,
        )

        # force: dev-only redeploy (403 in prod) — an operator action, not a
        # customer wake. Same escape hatch as the POST route above.
        await ecs_manager.start_user_service(owner_id, force=True)

        return {
            "status": "redeploying",
            "service_name": container["service_name"],
            "owner_id": owner_id,
            "owner_type": owner_type,
        }
    except EcsManagerError as e:
        logger.error("Dev redeploy failed for owner %s: %s", owner_id, e)
        raise HTTPException(status_code=503, detail=str(e))


@router.delete(
    "/provision",
    summary="Remove container (dev only)",
    description="Removes the user's ECS Fargate service. Dev only.",
    operation_id="debug_remove_container",
    dependencies=[Depends(require_non_production)],
    responses={
        403: {"description": "Not available outside dev/local/test"},
        404: {"description": "No container found"},
    },
)
async def remove_container(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    if auth.is_org_context:
        require_org_admin(auth)

    try:
        await get_ecs_manager().delete_user_service(owner_id)
        return {"status": "removed"}
    except EcsManagerError as e:
        logger.error("Dev remove failed for owner %s: %s", owner_id, e)
        raise HTTPException(status_code=503, detail=str(e))


@router.delete(
    "/user-data",
    summary="DEV/TEST ONLY: atomically delete all per-user data",
    description=(
        "Tears down ECS service, EFS access point, EFS folder, all "
        "per-user task-def revisions, and rows from all 9 per-user "
        "DDB tables (including paperclip-companies, whose row carries the "
        "onboarding generation/funnel/bootstrap fields — see INV-10). "
        "Used by the e2e harness at the end of each run. "
        "Caller can only delete their own data (owner_id derived from JWT)."
    ),
    operation_id="debug_delete_user_data",
    responses={
        403: {"description": "Not available outside dev/local/test"},
    },
)
async def delete_user_data(auth: AuthContext = Depends(get_current_user)):
    assert_debug_enabled("user-data")

    # Mirror /debug/provision DELETE — non-admin org members must not be able
    # to wipe shared org-scoped state (containers, billing, api-keys, etc.)
    # via this endpoint just because dev/staging exposes it. Personal context
    # passes through (require_org_admin no-ops when not in org).
    if auth.is_org_context:
        require_org_admin(auth)

    owner_id = resolve_owner_id(auth)
    # In an org context resolve_owner_id returns the org_id, but the `users`
    # and `ws-connections` tables are keyed by the Clerk user_id directly.
    # Without this split, org E2E teardown would target the org id for both,
    # leaving the Clerk user row + WS connection rows behind on every run.
    user_id = auth.user_id
    deleted: dict = {"ecs": False, "efs": False, "ddb": []}
    # Track failures so we can return 500 at the end. Test cleanup needs to
    # know if anything leaked — a green 200 with leaked ECS/EFS state was
    # silently accumulating orphan services on every run.
    # Each step is still best-effort (we attempt every step regardless of
    # earlier failures so the spec sees a complete summary), but we surface
    # a non-success status at the end if any destructive step errored.
    failures: list[str] = []

    # ---- Container teardown -------------------------------------------------
    # Read the row first so we have access_point_id + task_definition_arn
    # available even if the service-delete path doesn't clean them up.
    # NB: ECS teardown is NOT gated on the row's existence — if a previous
    # partial run failed at ECS but successfully wiped the DDB row, every
    # subsequent retry would skip ECS and orphan the service forever. The
    # service name is derived from owner_id alone, so delete_user_service
    # can always attempt the call (it treats ServiceNotFoundException as
    # idempotent success).
    container = await container_repo.get_by_owner_id(owner_id)
    ecs_mgr = get_ecs_manager()
    ecs_succeeded = False
    try:
        await ecs_mgr.delete_user_service(owner_id)
        deleted["ecs"] = True
        ecs_succeeded = True
    except Exception as e:
        logger.warning("ECS teardown failed for %s: %s", owner_id, e)
        failures.append(f"ecs: {e}")
    # Belt-and-suspenders: deregister the per-user task-def revision if we
    # still have the arn from the DDB row. delete_user_service normally
    # handles this; the explicit retry here covers the case where the row
    # was wiped but the task def revision was orphaned. Best-effort.
    if container and container.get("task_definition_arn"):
        try:
            ecs_mgr._deregister_task_definition(container["task_definition_arn"])
        except Exception:
            pass

    # ---- EFS folder rm -rf --------------------------------------------------
    try:
        get_workspace().delete_user_dir(owner_id)
        deleted["efs"] = True
    except Exception as e:
        logger.warning("EFS rm -rf failed for %s: %s", owner_id, e)
        failures.append(f"efs: {e}")

    # ---- Litestream S3 replicas (local-sqlite layout) --------------------
    # Without this, the reprovisioned container's boot script RESTORES the
    # pre-wipe hot state from S3 and the "empty slate" contract silently
    # breaks. No-op on the EFS layout (LITESTREAM_BUCKET unset).
    from core.services.litestream_replicas import delete_replica_prefix

    # Drain first: the service delete above is asynchronous, and a still
    # running container's litestream can upload AFTER the prefix delete — the
    # reprovision would then restore the state this endpoint just wiped. A
    # drain timeout is a failure, not a licence to delete anyway. The budget
    # exceeds the container's stopTimeout (120s).
    if settings.LITESTREAM_BUCKET:
        try:
            drained = await ecs_mgr.wait_until_drained(owner_id, timeout_s=180.0)
        except Exception as e:  # noqa: BLE001 — treated as not-drained below
            logger.warning("drain wait failed for %s: %s", owner_id, e)
            drained = False
        if not drained:
            failures.append("litestream_replicas: container did not drain; replicas left intact")
        elif not await delete_replica_prefix(owner_id):
            failures.append("litestream_replicas: delete failed (stale state will restore)")
        else:
            deleted["litestream_replicas"] = True

    # ---- DynamoDB cleanup, all 8 per-user tables ----------------------------
    # Per-step try/except so one transient throttle on (e.g.) container_repo
    # doesn't abort the rest of the teardown — we want every table attempted,
    # then surface a 500 with the failure list at the end if anything broke.
    # NB: do NOT add ("containers", ...) here unconditionally. If ECS
    # teardown failed, the row still holds the access_point_id +
    # task_definition_arn that the next retry needs to clean up the
    # orphaned resources — wiping the row turns a transient ECS error
    # into an unrecoverable leak. On the happy path delete_user_service has
    # already removed the row, so the explicit delete here is just
    # belt-and-suspenders.
    async def _wipe_integrations() -> None:
        # Sweep both the secret-free status rows AND the per-provider Secrets
        # Manager credentials. List the rows first so the SM secrets can be
        # deleted by name without an account-wide secretsmanager:ListSecrets.
        for row in await integrations_repo.list_for_owner(owner_id):
            provider = row.get("provider")
            if not provider:
                continue
            # `#`-namespaced rows (e.g. google#account#<email>) are actor-refresh
            # sub-records with no Secrets Manager secret, and the `#` would make
            # the SM delete raise InvalidParameterException. Drop the DDB row
            # only; skip the SM call for them.
            if "#" not in provider:
                await integration_secret_store.delete(owner_id, provider)
            await integrations_repo.delete(owner_id, provider)

    async def _wipe_byo_llm_keys() -> None:
        # BYO LLM keys live in Secrets Manager (user-keys/<owner>/<provider>),
        # keyed by the owner id key_service.set_key used. Force-delete both
        # providers' secrets (no-op if absent). Paperclip per-agent keys also
        # live in Secrets Manager but are not enumerable here without the agent
        # ids; they are re-mintable and swept by the clean-slate Paperclip reset.
        from core.services.key_service import KeyService

        svc = KeyService()
        for provider in ("openai", "anthropic"):
            await svc.delete_key(owner_id, provider)

    ddb_steps = []
    if ecs_succeeded:
        ddb_steps.append(("containers", lambda: container_repo.delete(owner_id)))
    ddb_steps.extend(
        [
            ("billing-accounts", lambda: billing_repo.delete(owner_id)),
            ("byo-llm-keys", _wipe_byo_llm_keys),
            ("usage-counters", lambda: usage_repo.delete_all_for_owner(owner_id)),
            ("pending-updates", lambda: update_repo.delete_all_for_owner(owner_id)),
            ("channel-links", lambda: channel_link_repo.delete_all_for_owner(owner_id)),
            ("integrations", _wipe_integrations),
            ("ws-connections", lambda: connection_service.delete_all_for_user(user_id)),
            ("users", lambda: user_repo.delete(user_id)),
            # paperclip-companies is keyed by user_id, NOT owner_id (mirrors
            # "users"/"ws-connections" above) -- for an org, the OWNER row
            # provision_org wrote is keyed by the org creator's user_id, not
            # the org_id. Since this endpoint requires org-admin for org
            # context, the calling admin IS (for the e2e harness's own
            # throwaway orgs) that creator, so deleting by user_id here
            # correctly reaches the row and rides its onboarding
            # generation/funnel/bootstrap fields to zero-orphan (INV-10).
            # This closes a real pre-existing gap: without it, a full
            # /debug/user-data teardown left the paperclip-companies row
            # (and now its onboarding state) behind, so the next fresh
            # provision would RECONCILE-ADOPT the old company instead of
            # minting a new onboarding_generation.
            (
                "paperclip-companies",
                lambda: PaperclipRepo(table_name="paperclip-companies").delete(user_id),
            ),
            # The pending-brief sentinel lives in the SAME table under a
            # synthetic PK, so the row delete above does not reach it, and it
            # is keyed by OWNER id (org_id for an org) rather than user_id.
            # Left behind it outlives the teardown by design -- a pending
            # brief deliberately survives reprovisioning -- and since this
            # endpoint does not delete the Clerk account, the next signup
            # reuses the same owner_id and inherits the previous run's answer:
            # the funnel never renders, and the new company gets seeded from
            # the old brief. That is the zero-orphan (INV-10) break the
            # comment above exists to prevent.
            (
                "paperclip-companies:pending-brief",
                lambda: PaperclipRepo(table_name="paperclip-companies").delete_pending_brief(owner_id),
            ),
        ]
    )
    for table_name, action in ddb_steps:
        try:
            await action()
            deleted["ddb"].append(table_name)
        except Exception as e:
            logger.warning("DDB teardown failed for %s on %s: %s", table_name, owner_id, e)
            failures.append(f"ddb.{table_name}: {e}")

    if failures:
        # 500 with the failure list AND the partial deleted summary so the
        # caller can both fail loudly AND know what state was reached.
        raise HTTPException(
            status_code=500,
            detail={"deleted": deleted, "failures": failures},
        )

    return {"deleted": deleted}


@router.get(
    "/efs-exists",
    summary="DEV/TEST ONLY: check whether an EFS path exists",
    description=(
        "Read-only existence probe used by the e2e teardown verification "
        "pass. The path must live under the workspace mount root "
        "(``settings.EFS_MOUNT_PATH``) — paths outside that prefix are "
        "rejected with 400 to keep this from doubling as an arbitrary "
        "filesystem reader."
    ),
    operation_id="debug_efs_exists",
    responses={
        400: {"description": "Path outside the workspace mount root"},
        403: {"description": "Not available outside dev/local/test"},
    },
)
async def efs_exists(path: str, auth: AuthContext = Depends(get_current_user)):
    assert_debug_enabled("efs-exists")

    # Scope the probe to the caller's own workspace dir. Without this, any
    # authenticated dev/staging user could probe whether files exist in
    # another owner's workspace by passing `<mount>/<other_owner>/...`.
    # Resolve both sides first so traversal segments like
    # `<mount>/<self>/../<other>` can't escape the owner_root either.
    workspace = get_workspace()
    owner_id = resolve_owner_id(auth)
    owner_root = workspace.user_path(owner_id).resolve()
    requested = Path(path).resolve()
    if requested != owner_root and not requested.is_relative_to(owner_root):
        raise HTTPException(
            status_code=400,
            detail=f"path must be under {owner_root}/",
        )
    return {"exists": requested.exists()}


@router.get(
    "/ddb-rows",
    summary="DEV/TEST ONLY: row counts per per-user DDB table for an owner",
    description=(
        "Returns a row count for each of the 8 per-user DynamoDB tables, "
        "scoped to the given ``owner_id``. Used by the e2e teardown "
        "verification pass to assert the DELETE ``/debug/user-data`` call "
        "actually drained every table."
    ),
    operation_id="debug_ddb_rows",
    responses={
        403: {"description": "Not available outside dev/local/test"},
    },
)
async def ddb_rows(
    owner_id: str,
    user_id: str | None = None,
    auth: AuthContext = Depends(get_current_user),
):
    assert_debug_enabled("ddb-rows")

    # Restrict to caller-owned data. The endpoint is meant for the test
    # harness to verify ITS OWN teardown — anyone passing a different
    # owner/user is fishing for cross-tenant info in dev/staging.
    if owner_id != resolve_owner_id(auth):
        raise HTTPException(status_code=403, detail="Cannot query another owner's row counts")
    if user_id is not None and user_id != auth.user_id:
        raise HTTPException(status_code=403, detail="Cannot query another user's row counts")

    # `users` and `ws-connections` are keyed by Clerk user_id even when other
    # tables are scoped by org_id. Caller supplies user_id explicitly in org
    # mode; defaults to owner_id for personal mode where they're the same
    # value.
    effective_user_id = user_id or owner_id

    user = await user_repo.get_by_user_id(effective_user_id)
    container = await container_repo.get_by_owner_id(owner_id)
    billing = await billing_repo.get_by_owner_id(owner_id)
    usage = await usage_repo.count_for_owner(owner_id)
    updates = await update_repo.count_for_owner(owner_id)
    channels = await channel_link_repo.count_for_owner(owner_id)
    ws_conns = await connection_service.count_for_user(effective_user_id)

    return {
        "tables": {
            "users": 1 if user else 0,
            "containers": 1 if container else 0,
            "billing-accounts": 1 if billing else 0,
            "usage-counters": usage,
            "pending-updates": updates,
            "channel-links": channels,
            "ws-connections": ws_conns,
        }
    }
