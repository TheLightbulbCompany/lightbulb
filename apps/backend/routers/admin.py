"""Admin dashboard router — /api/v1/admin/*

Every endpoint is gated by Depends(require_platform_admin), except a
curated set of READ endpoints on require_admin_read_access (see
core/services/admin_access.py) which additionally accept the founder
agent's platform_ops token. The token can inspect debug state but never
mutate; /admin/me (the frontend admin gate) and the posthog timeline
(session-replay links are for humans) stay Clerk-only.
Mutation endpoints are wrapped with @audit_admin_action so a row lands
in DDB synchronously (fail-closed). Container + billing actions also
accept an Idempotency-Key header to short-circuit double clicks.

Read endpoints delegate to admin_service (composition layer).
Mutation endpoints call the underlying service (ecs_manager,
billing_service, clerk_admin, gateway_pool, config_patcher) directly —
admin_service intentionally doesn't compose those because each is a
distinct side-effect.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.auth import AuthContext, require_platform_admin
from core.containers import get_ecs_manager, get_gateway_pool
from core.repositories import admin_actions_repo, update_repo  # noqa: F401 — admin_actions_repo is patch target for tests
from core.services import (
    admin_service,
    billing_service,
    clerk_admin,
    system_health,
)
from core.services.admin_access import require_admin_read_access
from core.services.admin_audit import audit_admin_action
from core.services.admin_service import resolve_admin_owner_id
from core.services.config_patcher import patch_openclaw_config
from core.services.idempotency import idempotency
from core.services.update_service import apply_update
from schemas.config import ConfigPatchRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class IssueCreditRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, description="Credit amount in cents")
    reason: str = Field(..., min_length=1, description="Reason for credit (audited)")


class MarkInvoiceResolvedRequest(BaseModel):
    invoice_id: str = Field(..., description="Stripe invoice id")


class QueueImageUpdateRequest(BaseModel):
    new_image: str = Field(
        ...,
        min_length=1,
        description="ECR image URL to queue (e.g. 877352799272.dkr.ecr.us-east-1.amazonaws.com/isol8/openclaw-extended:2026.5.12-570e197)",
    )
    description: str | None = Field(
        default=None,
        description="Human-readable change summary shown to the owner in their update banner",
    )
    # NB: deliberately omitting `force_by` — update_repo persists it but the
    # scheduled worker never reads it. Plumbing it through belongs in a
    # change that also wires the worker to honor it.


# apply_update feeds these changes.* fields to resize_user_container, which
# re-registers the per-user ECS task definition. Two in-flight updates that both
# carry any of them race on the task-def (last writer wins the image/size), so
# the force-apply guard treats a row carrying any of them as a conflict —
# regardless of its update_type label (a paperclip_provision retry or a pure
# config_patch hot-reload carries none of these and is not a conflict).
_TASKDEF_ROLL_CHANGE_KEYS = ("new_image", "new_cpu", "new_memory")


def _rolls_task_def(changes: object) -> bool:
    """True if a pending update's ``changes`` would re-register the ECS task def."""
    return isinstance(changes, dict) and any(k in changes for k in _TASKDEF_ROLL_CHANGE_KEYS)


# ---------------------------------------------------------------------------
# Auth + system
# ---------------------------------------------------------------------------


@router.get("/me", description="Auth probe: returns the calling admin's profile or 403.")
async def admin_me(auth: AuthContext = Depends(require_platform_admin)):
    return {"user_id": auth.user_id, "email": auth.email, "is_admin": True}


@router.get(
    "/system/health",
    description="Platform health: container fleet counts, upstream probes, background-task state, recent errors.",
)
async def admin_system_health(auth: AuthContext = Depends(require_admin_read_access)):
    return await system_health.get_system_health()


@router.get(
    "/actions",
    description="Admin-action audit viewer. Filter by target_user_id or admin_user_id.",
)
async def admin_actions_audit(
    target_user_id: str | None = Query(None),
    admin_user_id: str | None = Query(None),
    limit: int = Query(50, le=200),
    cursor: str | None = Query(None),
    auth: AuthContext = Depends(require_admin_read_access),
):
    try:
        return await admin_service.get_actions_audit(
            target_user_id=target_user_id,
            admin_user_id=admin_user_id,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# User directory + per-user reads
# ---------------------------------------------------------------------------


@router.get(
    "/users",
    description="Paginated user directory with container status join.",
)
async def admin_list_users(
    q: str = Query(""),
    subscription_status: str | None = Query(None),
    container_status: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(50, le=200),
    auth: AuthContext = Depends(require_admin_read_access),
):
    return await admin_service.list_users(q=q, limit=limit, cursor=cursor)


@router.get(
    "/users/{user_id}/overview",
    description="Per-user identity + billing + container + usage merged from Clerk/Stripe/DDB.",
)
async def admin_user_overview(
    user_id: str,
    auth: AuthContext = Depends(require_admin_read_access),
):
    return await admin_service.get_overview(user_id)


@router.get(
    "/users/{user_id}/agents",
    description="List a user's agents via gateway RPC. Returns container_status when not running.",
)
async def admin_user_agents(
    user_id: str,
    cursor: str | None = Query(None),
    limit: int = Query(50, le=200),
    auth: AuthContext = Depends(require_admin_read_access),
):
    return await admin_service.list_user_agents(user_id, cursor=cursor, limit=limit)


@router.get(
    "/users/{user_id}/agents/{agent_id}",
    description="Full agent detail (config, sessions, skills) with secret redaction. 409 when container not running.",
)
async def admin_agent_detail(
    user_id: str,
    agent_id: str,
    auth: AuthContext = Depends(require_admin_read_access),
):
    result = await admin_service.get_agent_detail(user_id, agent_id)
    if isinstance(result, dict) and result.get("error") == "container_not_running":
        raise HTTPException(status_code=409, detail=result)
    return result


@router.get(
    "/users/{user_id}/posthog",
    description="PostHog Persons API timeline for the user (events + session replay links).",
)
async def admin_user_posthog(
    user_id: str,
    limit: int = Query(100, le=500),
    auth: AuthContext = Depends(require_platform_admin),
):
    return await admin_service.get_posthog_timeline(user_id, limit=limit)


@router.get(
    "/users/{user_id}/logs",
    description="Inline CloudWatch Logs filtered to the user with cursor pagination.",
)
async def admin_user_logs(
    user_id: str,
    level: str = Query("ERROR"),
    hours: int = Query(24, le=168),
    limit: int = Query(20, le=100),
    cursor: str | None = Query(None),
    auth: AuthContext = Depends(require_admin_read_access),
):
    return await admin_service.get_logs(user_id, level=level, hours=hours, limit=limit, cursor=cursor)


@router.get(
    "/users/{user_id}/cloudwatch-url",
    description="Build a CloudWatch Logs Insights deep-link pre-filtered to the user.",
)
async def admin_user_cloudwatch_url(
    user_id: str,
    start: str = Query(...),
    end: str = Query(...),
    level: str = Query("ERROR"),
    auth: AuthContext = Depends(require_admin_read_access),
):
    return {"url": admin_service.get_cloudwatch_url(user_id, start=start, end=end, level=level)}


# ---------------------------------------------------------------------------
# Container actions (D1 idempotency)
# ---------------------------------------------------------------------------


@router.post(
    "/users/{user_id}/container/reprovision",
    description="Reprovision the container: rewrites openclaw.json and redeploys. "
    "Channel accounts and MCP servers are preserved across the rewrite. "
    "Idempotency-Key supported.",
)
@idempotency()
@audit_admin_action("container.reprovision")
async def admin_container_reprovision(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    # Resolve owner_id so org-member targets hit ``openclaw-{org_id}-{hash}``,
    # not the non-existent ``openclaw-{user_id}-{hash}`` (admin-org-owner-id
    # P1). Audit decorator still captures the URL path param (user_id).
    owner_id, _ = await resolve_admin_owner_id(user_id)
    ecs = get_ecs_manager()
    return await ecs.reprovision_for_user(owner_id)


@router.post(
    "/users/{user_id}/container/stop",
    description="Stop the user's ECS service (scale to 0). Idempotency-Key supported.",
)
@idempotency()
@audit_admin_action("container.stop")
async def admin_container_stop(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    ecs = get_ecs_manager()
    return await ecs.stop_user_service(owner_id)


@router.post(
    "/users/{user_id}/container/start",
    description="Start the user's ECS service (scale to 1). Idempotency-Key supported.",
)
@idempotency()
@audit_admin_action("container.start")
async def admin_container_start(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    ecs = get_ecs_manager()
    # force: the operator break-glass. start_user_service refuses a customer
    # start when the free-tier cap is up or a trial->paid conversion owns the
    # row; an admin start is neither, and it is the ONLY manual lever to bring
    # back a container whose conversion marker got stranded (a backend deploy
    # mid-drain leaves substatus="converting" on the row). Platform-admin only
    # and audited by @audit_admin_action above.
    return await ecs.start_user_service(owner_id, force=True)


@router.post(
    "/users/{user_id}/container/resize",
    description="Re-apply the canonical flat-fee task profile (0.5 vCPU / 2 GB). Recovery action for containers wedged on a stale task def. No body.",
)
@idempotency()
@audit_admin_action("container.resize")
async def admin_container_resize(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    ecs = get_ecs_manager()
    return await ecs.reapply_standard_sizing(owner_id)


@router.get(
    "/container/available-images",
    description=(
        "List recently-pushed openclaw-extended ECR tags (newest first). "
        "Powers the admin queue-image-update dropdown. Degrades to an empty "
        "list on ECR errors so the UI can fall back to free-text entry."
    ),
)
async def admin_container_available_images(
    limit: int = Query(25, ge=1, le=100),
    auth: AuthContext = Depends(require_platform_admin),
):
    images = await admin_service.list_extended_image_tags(limit=limit)
    return {
        "images": images,
        "current_dev_tag": admin_service.CURRENT_DEV_TAG,
        "current_prod_tag": admin_service.CURRENT_PROD_TAG,
    }


@router.post(
    "/users/{user_id}/container/queue-image-update",
    description=(
        "Queue a Track 2 image update for the user. Resolves user_id to owner_id "
        "(so org members route to their org's container), then creates the pending "
        "update. The owner sees a banner in chat and applies via "
        "POST /api/v1/container/updates/{id}/apply when ready. Non-destructive: "
        "container keeps running on the old image until accepted. Idempotency-Key "
        "supported."
    ),
)
@idempotency()
@audit_admin_action("container.queue_image_update")
async def admin_container_queue_image_update(
    user_id: str,
    body: QueueImageUpdateRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    description = body.description or f"Image update to {body.new_image}"
    item = await update_repo.create(
        owner_id=owner_id,
        update_type="image_update",
        description=description,
        changes={"new_image": body.new_image},
    )
    return {"status": "queued", "update": item}


@router.post(
    "/users/{user_id}/container/apply-image-update",
    description=(
        "Force-apply a container image update NOW (admin-initiated) instead of "
        "waiting for the owner to accept the Track-2 banner. Resolves user_id to "
        "owner_id, creates the pending image_update row, then applies it via the "
        "SAME apply_update() path the owner banner uses: the container's on-disk "
        "openclaw.json is reused (removed-schema keys stripped first so the newer "
        "image's strict schema can't crash-loop the gateway) and the per-user "
        "task-def is rolled to the new image. Non-destructive — EFS workspace and "
        "agent state are preserved; the container briefly restarts on the new "
        "image. Returns 409 if the update could not be applied (a prior update is "
        "already in progress); the row stays queued for a retry or owner-apply. "
        "Idempotency-Key supported."
    ),
)
@idempotency()
@audit_admin_action("container.force_apply_image_update")
async def admin_container_force_apply_image_update(
    user_id: str,
    body: QueueImageUpdateRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)

    # Per-owner mutual exclusion. apply_update's acquire is keyed per-update_id,
    # so a fresh row we create always acquires — that alone does NOT stop a second
    # roll when the owner already has a container update queued (admin queue button
    # / generic updates API / scheduled worker) or mid-apply (owner banner). Reject
    # up front so only one task-def roll is normally in flight per owner.
    #
    # Conflict is decided by the changes payload, not the update_type label: any row
    # apply_update would feed to resize_user_container (new_image / new_cpu /
    # new_memory) re-registers the task-def and would race this force-apply,
    # last-writer-wins on the resulting image/size. A paperclip_provision retry or a
    # pure config_patch hot-reload does not touch the task-def, so it is not a
    # conflict.
    #
    # This read-then-create is not fully atomic: two force-applies landing within
    # the same instant could both observe no conflict. That residual race is
    # deliberately not locked out here — the per-user ECS service runs
    # SINGLE_WRITER_DEPLOYMENT_CONFIG (maximumPercent=100 → never two tasks), so
    # even two concurrent resize_user_container calls can't put two tasks on the
    # EFS-SQLite state at once; the worst case is a redundant superseded deployment,
    # not corruption or data loss. A distributed lock would add stuck-lock failure
    # modes disproportionate to that bounded, admin-only race.
    in_flight = await update_repo.get_in_flight(owner_id)
    conflicting = [u for u in in_flight if _rolls_task_def(u.get("changes"))]
    if conflicting:
        statuses = ", ".join(sorted({u.get("status", "?") for u in conflicting}))
        raise HTTPException(
            status_code=409,
            detail=(
                f"Another container update is already in flight for this owner "
                f"(status: {statuses}). Wait for it to finish, or clear it, before "
                "force-applying a new image."
            ),
        )

    description = body.description or f"Admin force-apply image update to {body.new_image}"
    item = await update_repo.create(
        owner_id=owner_id,
        update_type="image_update",
        description=description,
        changes={"new_image": body.new_image},
        force_by=auth.user_id,
    )
    update_id = item["update_id"]
    applied = await apply_update(owner_id, update_id)
    if not applied:
        # No update was in flight (guarded above) and the row we just created was
        # 'pending', so apply_update's acquire always succeeds here. A False return
        # therefore means the roll itself raised — apply_update caught it and marked
        # the row 'failed', so it is NOT a live queued row the owner can retry from
        # their banner. Surface it as a server-side failure with the EFS-safe note.
        raise HTTPException(
            status_code=502,
            detail=(
                f"The image roll failed while applying (update_id={update_id}; row "
                "marked failed). The container's EFS workspace/state is unchanged — "
                "check the ECS service / container logs before retrying."
            ),
        )
    return {
        "status": "applied",
        "owner_id": owner_id,
        "update_id": update_id,
        "new_image": body.new_image,
    }


# ---------------------------------------------------------------------------
# Billing actions
# ---------------------------------------------------------------------------


@router.post(
    "/users/{user_id}/billing/cancel-subscription",
    description="Cancel the user's Stripe subscription and revert to free tier.",
)
@idempotency()
@audit_admin_action("billing.cancel_subscription")
async def admin_billing_cancel_subscription(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    # Billing accounts are partitioned on owner_id (= org_id for org members).
    # Without the resolver the mutation targets a personal-key account that
    # doesn't exist for org members (admin-org-owner-id P1).
    owner_id, _ = await resolve_admin_owner_id(user_id)
    return await billing_service.cancel_subscription_for_owner(owner_id)


@router.post(
    "/users/{user_id}/billing/pause-subscription",
    description="Pause Stripe subscription billing (mark_uncollectible). Resumes on unpause.",
)
@idempotency()
@audit_admin_action("billing.pause_subscription")
async def admin_billing_pause_subscription(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    return await billing_service.pause_subscription_for_owner(owner_id)


@router.post(
    "/users/{user_id}/billing/issue-credit",
    description="Apply a credit to the user's Stripe customer balance (cents).",
)
@idempotency()
@audit_admin_action("billing.issue_credit")
async def admin_billing_issue_credit(
    user_id: str,
    body: IssueCreditRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    return await billing_service.issue_credit_for_owner(owner_id, amount_cents=body.amount_cents, reason=body.reason)


@router.post(
    "/users/{user_id}/billing/mark-invoice-resolved",
    description="Mark a Stripe invoice as paid out-of-band (e.g. wire transfer).",
)
@idempotency()
@audit_admin_action("billing.mark_invoice_resolved")
async def admin_billing_mark_invoice_resolved(
    user_id: str,
    body: MarkInvoiceResolvedRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    return await billing_service.mark_invoice_resolved(owner_id, body.invoice_id)


# ---------------------------------------------------------------------------
# Account actions (Clerk)
# ---------------------------------------------------------------------------


@router.post(
    "/users/{user_id}/account/suspend",
    description="Suspend the user — Clerk ban so they can't sign in.",
)
@audit_admin_action("account.suspend")
async def admin_account_suspend(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    return await clerk_admin.ban_user(user_id)


@router.post(
    "/users/{user_id}/account/reactivate",
    description="Reactivate a previously-suspended user (Clerk unban).",
)
@audit_admin_action("account.reactivate")
async def admin_account_reactivate(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    return await clerk_admin.unban_user(user_id)


@router.post(
    "/users/{user_id}/account/force-signout",
    description="Revoke all of the user's active Clerk sessions.",
)
@audit_admin_action("account.force_signout")
async def admin_account_force_signout(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    return await clerk_admin.revoke_sessions(user_id)


@router.post(
    "/users/{user_id}/account/resend-verification",
    description="Resend the email-verification link to the user's primary email.",
)
@audit_admin_action("account.resend_verification")
async def admin_account_resend_verification(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    return await clerk_admin.resend_verification(user_id)


# ---------------------------------------------------------------------------
# Config + agent actions
# ---------------------------------------------------------------------------


@router.patch(
    "/users/{user_id}/config",
    description="Deep-merge a config patch into the user's openclaw.json. Patch field redacted in audit row.",
)
@audit_admin_action("config.patch", redact_paths=["patch"])
async def admin_config_patch(
    user_id: str,
    body: ConfigPatchRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    # EFS workspace lives at /mnt/efs/users/{owner_id}/openclaw.json. For org
    # members owner_id is the org_id — patching {user_id} writes to a non-
    # existent personal dir (admin-org-owner-id P1).
    owner_id, _ = await resolve_admin_owner_id(user_id)
    await patch_openclaw_config(owner_id=owner_id, patch=body.patch)
    return {"status": "patched"}


async def _admin_agent_rpc(
    *,
    user_id: str,
    agent_id: str,
    method: str,
    req_prefix: str,
) -> dict:
    """Send a gateway RPC for an admin-initiated per-agent action.

    Container / gateway pool keyed on owner_id. For org members that's
    the org_id — resolve before the ECS / gateway dispatch so we don't
    send the RPC to a non-existent personal-mode container.

    uuid4 entropy: org members share a single gateway connection whose
    pending-RPC dict is keyed by req_id. Deterministic ids (e.g.
    ``admin-agent-delete-{agent_id}``) collide when two admins click on
    the same shared agent concurrently.
    """
    owner_id, _ = await resolve_admin_owner_id(user_id)
    pool = get_gateway_pool()
    ecs = get_ecs_manager()
    container, ip = await ecs.resolve_running_container(owner_id)
    if not container or not ip:
        raise HTTPException(status_code=409, detail="container_not_running")
    nonce = uuid.uuid4().hex[:8]
    return await pool.send_rpc(
        user_id=owner_id,
        req_id=f"{req_prefix}{agent_id}-{nonce}",
        method=method,
        params={"agent_id": agent_id},
        ip=ip,
        token=container["gateway_token"],
    )


@router.post(
    "/users/{user_id}/agents/{agent_id}/delete",
    description="Delete a user's agent via gateway agents.delete RPC.",
)
@audit_admin_action("agent.delete")
async def admin_agent_delete(
    user_id: str,
    agent_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    return await _admin_agent_rpc(
        user_id=user_id,
        agent_id=agent_id,
        method="agents.delete",
        req_prefix="admin-agent-delete-",
    )


@router.post(
    "/users/{user_id}/agents/{agent_id}/clear-sessions",
    description="Clear an agent's session history via gateway sessions.clear RPC.",
)
@audit_admin_action("agent.clear_sessions")
async def admin_agent_clear_sessions(
    user_id: str,
    agent_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    return await _admin_agent_rpc(
        user_id=user_id,
        agent_id=agent_id,
        method="sessions.clear",
        req_prefix="admin-agent-clear-",
    )
