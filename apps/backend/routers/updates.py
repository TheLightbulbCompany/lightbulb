"""Container updates router -- pending updates, apply/schedule, admin config patches."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from core.auth import (
    AuthContext,
    get_current_user,
    require_org_admin,
    require_platform_admin,
    resolve_owner_id,
)
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import container_repo, update_repo
from core.services.admin_audit import audit_admin_action
from core.services.config_patcher import patch_openclaw_config, ConfigPatchError
from core.services.update_service import (
    TASK_DEFINITION_REFRESH_KIND,
    apply_update,
    queue_fleet_image_update,
    queue_fleet_task_definition_refresh,
    queue_task_definition_refresh_for_owner,
)
from schemas.config import ConfigPatchRequest

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ApplyRequest(BaseModel):
    schedule: str = Field(
        ...,
        description="When to apply: 'now', 'tonight', or 'remind_later'",
        pattern="^(now|tonight|remind_later)$",
    )


class AdminUpdateRequest(BaseModel):
    owner_id: str = Field(..., description="Owner ID or 'all' for fleet-wide")
    update_type: str = Field(
        ...,
        description="Type of update (e.g. 'image_update', 'container_resize', 'task_definition_refresh')",
    )
    description: str = Field(..., description="Human-readable description")
    changes: dict = Field(default_factory=dict, description="Change payload")
    force_by: Optional[str] = Field(default=None, description="ISO timestamp deadline")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/updates",
    summary="Get pending updates",
    description="Returns all pending or scheduled updates for the authenticated owner.",
)
async def get_pending_updates(auth: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(auth)
    items = await update_repo.get_pending(owner_id)
    return {"updates": items}


@router.post(
    "/updates/{update_id}/apply",
    summary="Apply or schedule an update",
    description="Apply an update immediately, schedule for tonight, or snooze.",
)
async def apply_or_schedule_update(
    update_id: str,
    body: ApplyRequest,
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)

    # Require org admin when in org context
    if auth.is_org_context:
        require_org_admin(auth)

    if body.schedule == "now":
        success = await apply_update(owner_id, update_id)
        if not success:
            raise HTTPException(
                status_code=409, detail="Update could not be applied (already in progress or invalid state)"
            )
        return {"status": "applied", "update_id": update_id}

    elif body.schedule == "tonight":
        # Calculate next 2 AM UTC
        now_utc = datetime.now(timezone.utc)
        tonight_2am = now_utc.replace(hour=2, minute=0, second=0, microsecond=0)
        if tonight_2am <= now_utc:
            tonight_2am += timedelta(days=1)
        scheduled_at = tonight_2am.isoformat()

        success = await update_repo.set_scheduled(owner_id, update_id, scheduled_at)
        if not success:
            raise HTTPException(status_code=409, detail="Update could not be scheduled (invalid state)")
        return {"status": "scheduled", "update_id": update_id, "scheduled_at": scheduled_at}

    else:  # remind_later
        success = await update_repo.set_snoozed(owner_id, update_id)
        if not success:
            raise HTTPException(status_code=409, detail="Update could not be snoozed (not found)")
        return {"status": "snoozed", "update_id": update_id}


@router.post(
    "/updates",
    summary="Admin: create updates",
    description=(
        "Create an update for a specific owner, or a fleet image/base-task-definition rollout. "
        "Platform-admin only: targets arbitrary owner_ids (incl. 'all'), so leaving it open to any "
        "signed-in user would let a non-admin queue updates for anyone else's container. The earlier "
        "is_org_context-only guard was effectively unauthenticated in personal context."
    ),
)
async def admin_create_update(
    body: AdminUpdateRequest,
    auth: AuthContext = Depends(require_platform_admin),
):
    if body.owner_id == "all" and body.update_type == "image_update":
        new_image = body.changes.get("new_image", "")
        if not new_image:
            raise HTTPException(status_code=400, detail="changes.new_image is required for fleet image updates")
        count = await queue_fleet_image_update(new_image, body.description)
        return {"status": "queued", "count": count}

    if body.update_type == "release":
        if body.owner_id == "all" or set(body.changes) != {"target_release_id"}:
            raise HTTPException(status_code=400, detail="A release must target one owner and one registered release_id")
        from core.services.openclaw_release import OpenClawReleaseError, release_by_id

        try:
            release = release_by_id(body.changes["target_release_id"])
        except OpenClawReleaseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        item = await update_repo.create(
            owner_id=body.owner_id,
            update_type="release",
            description=body.description,
            changes={"target_release_id": release.release_id},
            force_by=body.force_by,
        )
        return {"status": "created", "update": item}

    if body.owner_id == "all" and body.update_type == TASK_DEFINITION_REFRESH_KIND:
        queued, skipped = await queue_fleet_task_definition_refresh(body.description)
        return {"status": "scheduled", "queued": queued, "skipped": skipped}

    if body.update_type == TASK_DEFINITION_REFRESH_KIND:
        # Single-owner refresh: same eligibility/conflict rules and change
        # marker as the fleet path — a bare create here would queue a row the
        # apply path treats as a no-op.
        row = await container_repo.get_by_owner_id(body.owner_id)
        if not row:
            raise HTTPException(status_code=404, detail="No container for owner")
        item = await queue_task_definition_refresh_for_owner(row, description=body.description)
        if item is None:
            return {"status": "skipped", "reason": "container ineligible or task-definition change already in flight"}
        return {"status": "created", "update": item}

    # Single owner update
    item = await update_repo.create(
        owner_id=body.owner_id,
        update_type=body.update_type,
        description=body.description,
        changes=body.changes,
        force_by=body.force_by,
    )
    return {"status": "created", "update": item}


# ---------------------------------------------------------------------------
# Direct config patching (admin — no notification, immediate apply)
# ---------------------------------------------------------------------------


@router.patch(
    "/config/{owner_id}",
    summary="Patch a single user's config",
    description="Deep-merges the patch into the owner's openclaw.json on EFS. "
    "OpenClaw file watcher hot-reloads immediately. Zero downtime for hot-reloadable fields.",
)
@audit_admin_action("config.patch", target_param="owner_id", redact_paths=["patch"])
async def patch_single_config(
    owner_id: str,
    body: ConfigPatchRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    # Platform-admin only: the owner_id comes from the path, so any non-admin
    # caller could otherwise patch an arbitrary tenant's config. Self-service
    # config patching is served by the owner-scoped PATCH /api/v1/config.
    try:
        await patch_openclaw_config(owner_id, body.patch)
    except ConfigPatchError as e:
        raise HTTPException(status_code=404, detail=str(e))

    put_metric("update.config_patch.applied", dimensions={"scope": "single"})
    return {"status": "patched", "owner_id": owner_id, "keys": list(body.patch.keys())}


@router.patch(
    "/config",
    summary="Patch ALL users' configs (fleet-wide)",
    description="Deep-merges the patch into every active owner's openclaw.json. "
    "Runs in the request — for large fleets, consider using the async update system instead. "
    "Requires the header `X-Confirm-Fleet-Patch: <ENVIRONMENT>` (e.g. 'prod'; 'local' when "
    "ENVIRONMENT is unset) — typing the environment name is the confirmation step for the "
    "fleet's most destructive endpoint.",
    responses={428: {"description": "Missing or wrong X-Confirm-Fleet-Patch header"}},
)
@audit_admin_action("config.patch_fleet", target_user_id_override="__fleet__", redact_paths=["patch"])
async def patch_fleet_config(
    body: ConfigPatchRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
    x_confirm_fleet_patch: str = Header(default=""),
):
    # Platform-admin only: this deep-merges into EVERY active owner's config,
    # so it must never be reachable by an ordinary authenticated user. (The
    # prior is_org_context-only guard passed personal-context callers through.)
    #
    # Typed confirmation: admin-ness alone is not intent. The caller must name
    # the environment they believe they are pointed at, which is what makes a
    # dev-shaped curl fired at api.isol8.co fail instead of rewriting every
    # production container's config. Raised INSIDE the handler on purpose so
    # @audit_admin_action still records the refused attempt (result="error").
    # `or "local"`: ENVIRONMENT is unset on a laptop, and comparing against ""
    # would make the *absent* header the passing case — the confirmation must
    # never be satisfiable by sending nothing.
    expected = settings.ENVIRONMENT or "local"
    if x_confirm_fleet_patch != expected:
        raise HTTPException(
            status_code=428,
            detail=(
                "Fleet config patch rewrites EVERY owner's openclaw.json. "
                f"Resend with header 'X-Confirm-Fleet-Patch: {expected}' to confirm "
                "you mean this environment."
            ),
        )

    put_metric("update.fleet_patch.invoked")

    # Scan all owners from billing-accounts
    from core.dynamodb import get_table, run_in_thread

    table = get_table("billing-accounts")
    owners = []
    last_key = None
    while True:
        scan_kwargs: dict = {"ProjectionExpression": "owner_id"}
        if last_key:
            scan_kwargs["ExclusiveStartKey"] = last_key
        response = await run_in_thread(table.scan, **scan_kwargs)
        owners.extend(item["owner_id"] for item in response.get("Items", []) if "owner_id" in item)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break

    patched = 0
    failed = 0
    for oid in owners:
        try:
            await patch_openclaw_config(oid, body.patch)
            patched += 1
        except ConfigPatchError:
            failed += 1
            logger.warning("Skipped config patch for owner %s (no config file)", oid)
        except Exception:
            failed += 1
            logger.exception("Failed to patch config for owner %s", oid)

    return {"status": "done", "patched": patched, "failed": failed, "total": len(owners)}
