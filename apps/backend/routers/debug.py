"""
Dev-only container provisioning endpoints.

Bypasses Stripe for local testing — disabled in production.
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
    api_key_repo,
    billing_repo,
    channel_link_repo,
    container_repo,
    update_repo,
    usage_repo,
    user_repo,
)
from core.services import connection_service

logger = logging.getLogger(__name__)

router = APIRouter()


async def require_non_production() -> None:
    """Dependency that blocks access in production environments."""
    if settings.ENVIRONMENT == "prod":
        put_metric("debug.endpoint.prod_hit", dimensions={"endpoint": "debug"})
        raise HTTPException(status_code=403, detail="Not available in production")


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
        403: {"description": "Not available in production"},
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
        await ecs_manager.start_user_service(owner_id)

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
        403: {"description": "Not available in production"},
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

        await ecs_manager.start_user_service(owner_id)

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
        403: {"description": "Not available in production"},
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
        "per-user task-def revisions, and rows from all 8 per-user "
        "DDB tables. Used by the e2e harness at the end of each run. "
        "Caller can only delete their own data (owner_id derived from JWT)."
    ),
    operation_id="debug_delete_user_data",
    responses={
        403: {"description": "Disabled in production"},
    },
)
async def delete_user_data(auth: AuthContext = Depends(get_current_user)):
    if settings.ENVIRONMENT == "prod":
        put_metric("debug.endpoint.prod_hit", dimensions={"endpoint": "user-data"})
        raise HTTPException(status_code=403, detail="Disabled in production")

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
    ddb_steps = []
    if ecs_succeeded:
        ddb_steps.append(("containers", lambda: container_repo.delete(owner_id)))
    ddb_steps.extend(
        [
            ("billing-accounts", lambda: billing_repo.delete(owner_id)),
            ("api-keys", lambda: api_key_repo.delete_all_for_owner(owner_id)),
            ("usage-counters", lambda: usage_repo.delete_all_for_owner(owner_id)),
            ("pending-updates", lambda: update_repo.delete_all_for_owner(owner_id)),
            ("channel-links", lambda: channel_link_repo.delete_all_for_owner(owner_id)),
            ("ws-connections", lambda: connection_service.delete_all_for_user(user_id)),
            ("users", lambda: user_repo.delete(user_id)),
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
        403: {"description": "Disabled in production"},
    },
)
async def efs_exists(path: str, auth: AuthContext = Depends(get_current_user)):
    if settings.ENVIRONMENT == "prod":
        put_metric("debug.endpoint.prod_hit", dimensions={"endpoint": "efs-exists"})
        raise HTTPException(status_code=403, detail="Disabled in production")

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
        403: {"description": "Disabled in production"},
    },
)
async def ddb_rows(
    owner_id: str,
    user_id: str | None = None,
    auth: AuthContext = Depends(get_current_user),
):
    if settings.ENVIRONMENT == "prod":
        put_metric("debug.endpoint.prod_hit", dimensions={"endpoint": "ddb-rows"})
        raise HTTPException(status_code=403, detail="Disabled in production")

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
    api_keys = await api_key_repo.count_for_owner(owner_id)
    usage = await usage_repo.count_for_owner(owner_id)
    updates = await update_repo.count_for_owner(owner_id)
    channels = await channel_link_repo.count_for_owner(owner_id)
    ws_conns = await connection_service.count_for_user(effective_user_id)

    return {
        "tables": {
            "users": 1 if user else 0,
            "containers": 1 if container else 0,
            "billing-accounts": 1 if billing else 0,
            "api-keys": api_keys,
            "usage-counters": usage,
            "pending-updates": updates,
            "channel-links": channels,
            "ws-connections": ws_conns,
        }
    }
