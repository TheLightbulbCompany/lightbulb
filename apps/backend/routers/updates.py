"""Container updates router -- pending updates, apply/schedule, admin config patches."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import (
    AuthContext,
    get_current_user,
    require_org_admin,
    require_platform_admin,
    resolve_owner_id,
)
from core.observability.metrics import put_metric
from core.repositories import update_repo
from core.services.config_patcher import patch_openclaw_config, ConfigPatchError
from core.services.update_service import apply_update, queue_fleet_image_update

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
    update_type: str = Field(..., description="Type of update (e.g. 'image_update', 'container_resize')")
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
        "Create a pending update for a specific owner or all owners (fleet-wide image update). "
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


class ConfigPatchRequest(BaseModel):
    patch: dict = Field(..., description="Config patch to deep-merge into openclaw.json")


@router.patch(
    "/config/{owner_id}",
    summary="Patch a single user's config",
    description="Deep-merges the patch into the owner's openclaw.json on EFS. "
    "OpenClaw file watcher hot-reloads immediately. Zero downtime for hot-reloadable fields.",
)
async def patch_single_config(
    owner_id: str,
    body: ConfigPatchRequest,
    auth: AuthContext = Depends(get_current_user),
):
    if auth.is_org_context:
        require_org_admin(auth)

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
    "Runs in the request — for large fleets, consider using the async update system instead.",
)
async def patch_fleet_config(
    body: ConfigPatchRequest,
    auth: AuthContext = Depends(get_current_user),
):
    if auth.is_org_context:
        require_org_admin(auth)

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
