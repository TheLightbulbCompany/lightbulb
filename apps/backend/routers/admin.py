"""Admin dashboard router — /api/v1/admin/*

Every endpoint is gated by Depends(require_platform_admin). The surface is for
human Isol8 operators authenticated by Clerk; customer agents do not receive a
parallel credential path into platform administration.
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
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.auth import AuthContext, is_org_owner_id, require_platform_admin
from core.containers import get_ecs_manager, get_gateway_pool
from core.repositories import (  # noqa: F401 — admin_actions_repo is patch target for tests
    admin_actions_repo,
    container_repo,
    update_repo,
    user_repo,
)
from core.services import (
    admin_service,
    billing_service,
    clerk_admin,
    system_health,
)
from core.services.account_wipe import (
    BootstrapAccountProtectedError,
    OrgWipeUnsupportedError,
    preview_wipe,
    resolve_account_emails,
    wipe_account,
)
from core.services.admin_audit import audit_admin_action, extract_client_ip
from core.services.admin_service import resolve_admin_owner_id
from core.services.bootstrap_dispatcher import STALE_DISPATCH_AFTER_S, is_dispatch_stale
from core.services.config_patcher import patch_openclaw_config
from core.services.idempotency import idempotency
from core.services.provision_dispatch import dispatch_provision
from core.services.update_service import (
    TASKDEF_ROLL_CHANGE_KEYS,
    apply_update,
    queue_image_update,
    reapply_owner_config,
)
from schemas.config import ConfigPatchRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _paperclip_repo():
    """Local import so ``patch("core.repositories.paperclip_repo.PaperclipRepo", ...)``
    (the pattern every existing test double in this codebase already uses)
    keeps working -- mirrors ``admin_service``'s own local-import idiom
    (``core/services/admin_service.py:264-273``, itself mirroring
    ``readiness_state.py:102-110``).
    """
    from core.repositories.paperclip_repo import PaperclipRepo

    return PaperclipRepo(table_name="paperclip-companies")


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class IssueCreditRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, description="Credit amount in cents")
    reason: str = Field(..., min_length=1, description="Reason for credit (audited)")


class MarkInvoiceResolvedRequest(BaseModel):
    invoice_id: str = Field(..., description="Stripe invoice id")


class WipeConfirmRequest(BaseModel):
    confirm_email: str = Field(
        ...,
        description="Type-to-confirm: must match the account's email on file, checked server-side.",
    )


class QueueImageUpdateRequest(BaseModel):
    new_image: str = Field(
        ...,
        min_length=1,
        description="ECR image URL to queue (e.g. 877352799272.dkr.ecr.us-east-1.amazonaws.com/lightbulb/openclaw-extended:2026.5.12-570e197)",
    )
    description: str | None = Field(
        default=None,
        description="Human-readable change summary shown to the owner in their update banner",
    )
    # NB: deliberately omitting `force_by` — update_repo persists it but the
    # scheduled worker never reads it. Plumbing it through belongs in a
    # change that also wires the worker to honor it.


def _rolls_task_def(changes: object) -> bool:
    """True if a pending update's ``changes`` would re-register the ECS task def.

    The force-apply guard treats a row carrying any task-def-rolling key as a
    conflict regardless of its update_type label (a paperclip_provision retry
    or a pure config_patch hot-reload carries none and is not a conflict).
    The key set is owned by update_service.TASKDEF_ROLL_CHANGE_KEYS.
    """
    return isinstance(changes, dict) and any(k in changes for k in TASKDEF_ROLL_CHANGE_KEYS)


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
async def admin_system_health(auth: AuthContext = Depends(require_platform_admin)):
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
    auth: AuthContext = Depends(require_platform_admin),
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
    auth: AuthContext = Depends(require_platform_admin),
):
    return await admin_service.list_users(q=q, limit=limit, cursor=cursor)


@router.get(
    "/users/{user_id}/overview",
    description="Per-user identity + billing + container + usage merged from Clerk/Stripe/DDB.",
)
async def admin_user_overview(
    user_id: str,
    auth: AuthContext = Depends(require_platform_admin),
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
    auth: AuthContext = Depends(require_platform_admin),
):
    return await admin_service.list_user_agents(user_id, cursor=cursor, limit=limit)


@router.get(
    "/users/{user_id}/agents/{agent_id}",
    description="Full agent detail (config, sessions, skills) with secret redaction. 409 when container not running.",
)
async def admin_agent_detail(
    user_id: str,
    agent_id: str,
    auth: AuthContext = Depends(require_platform_admin),
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
    auth: AuthContext = Depends(require_platform_admin),
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
    auth: AuthContext = Depends(require_platform_admin),
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
    return await ecs.stop_user_service(owner_id, reason="admin")


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
    description="Re-apply the owner's canonical task profile (sized by plan: standard 1 vCPU / 4 GB, Ultra Pro 2 vCPU / 8 GB). Recovery action for containers wedged on a stale task def. No body.",
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
        "Resolve an accepted registry image to its immutable release and queue "
        "that release for the user. Resolves user_id to owner_id (so org members "
        "route to their org's container). The owner applies via "
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
    try:
        item = await queue_image_update(owner_id, body.new_image, body.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "queued", "update": item}


@router.post(
    "/users/{user_id}/container/apply-image-update",
    description=(
        "Resolve an accepted registry image and force-apply its recoverable release "
        "NOW instead of waiting for the owner. The release journal captures an "
        "immutable recovery set, migrates config, rolls the digest-pinned task "
        "definition, validates health, and compensates on failure. "
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

    # Per-owner mutual exclusion. A fresh row alone does NOT stop a second
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
    try:
        item = await queue_image_update(
            owner_id,
            body.new_image,
            description,
            force_by=auth.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    update_id = item["update_id"]
    applied = await apply_update(owner_id, update_id)
    if not applied:
        raise HTTPException(
            status_code=502,
            detail=(
                f"The release did not complete (update_id={update_id}). It may be "
                "reserved for journal recovery; inspect the release row before retrying."
            ),
        )
    return {
        "status": "applied",
        "owner_id": owner_id,
        "update_id": update_id,
        "new_image": body.new_image,
    }


@router.post(
    "/users/{user_id}/container/reapply-config",
    description=(
        "Regenerate the owner's openclaw.json on EFS from the CURRENT config "
        "generator (the dashboard twin of scripts/reapply_openclaw_config.py). "
        "Unlike the deep-merge config PATCH — which preserves stale sibling "
        "keys — this re-renders the full config via the provisioning write "
        "path, then restores only the runtime-appended subtrees the regen "
        "drops (channel pairing accounts, route bindings, boot-time "
        "identity). Config-only and non-destructive: no task restart, EFS "
        "workspace/agent state untouched. A running container picks the file "
        "up via OpenClaw's watcher; changes needing a gateway restart to "
        "activate (e.g. plugin enablement) land on the next image roll — "
        "pair with 'Apply now' for an immediate restart. Returns 409 for "
        "legacy rows without a gateway_token (those need a full "
        "reprovision). Idempotency-Key supported."
    ),
)
@idempotency()
@audit_admin_action("container.reapply_config")
async def admin_container_reapply_config(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    try:
        result = await reapply_owner_config(owner_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "reapplied", **result}


# ---------------------------------------------------------------------------
# Provisioning (read + retry/reset-bootstrap levers)
# ---------------------------------------------------------------------------


@router.get(
    "/users/{user_id}/provisioning",
    description=(
        "Provisioning truth for one owner: readiness contract "
        "(compute_status), containers-row error/lease fields, Paperclip "
        "company + funnel/bootstrap state, provision-gate decision "
        "(metric-free probe), and the SFN execution status. Read-only."
    ),
)
async def admin_user_provisioning(
    user_id: str,
    auth: AuthContext = Depends(require_platform_admin),
):
    return await admin_service.get_provisioning(user_id)


@router.post(
    "/users/{user_id}/provisioning/retry",
    description=(
        "Re-dispatch provisioning through the PROVISION executor. Covers the "
        "error-row retry AND the stale-claim wedge rescue (a 'provisioning' "
        "row whose SFN execution closed without a terminal write) — "
        "dispatch_provision's Branch A/B logic decides which. Unlike the "
        "owner /container/retry, no billing gate: this is an operator "
        "break-glass, audited. Idempotency-Key supported."
    ),
)
@idempotency()
@audit_admin_action("provision.retry")
async def admin_provision_retry(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    row = await container_repo.get_by_owner_id(owner_id)
    if not row:
        raise HTTPException(status_code=404, detail="No container row for this owner")
    status = row.get("status")
    if status == "running":
        raise HTTPException(status_code=409, detail="Container is running — nothing to retry")
    if status == "provisioning" and not container_repo.is_provision_claim_stale(row):
        raise HTTPException(
            status_code=409,
            detail="A provision attempt is in flight (fresh claim); refusing a concurrent dispatch",
        )
    await dispatch_provision(owner_id)
    fresh = await container_repo.get_by_owner_id(owner_id)
    return {
        "ok": True,
        "status": (fresh or {}).get("status"),
        "substatus": (fresh or {}).get("substatus"),
        "provision_claim_id": (fresh or {}).get("provision_claim_id"),
    }


@router.post(
    "/users/{user_id}/provisioning/reset-bootstrap",
    description=(
        "Re-arm a stuck onboarding kickoff: conditional bootstrap_state "
        "->not_started fenced on the row's current onboarding_generation, "
        "resetting the attempt counter. Accepts a 'failed' row at any age, "
        "and a 'dispatched' row whose claim has EXPIRED (its process died "
        "mid-dispatch) — including the ambiguous send-attempted-never-acked "
        "case the dispatcher will not retry on its own, which may "
        "double-deliver. Refuses a dispatch still in flight. The next owner "
        "/container/status poll re-dispatches the kickoff (delivery needs "
        "the owner's chat surface online). Idempotency-Key supported."
    ),
)
@idempotency()
@audit_admin_action("provision.reset_bootstrap")
async def admin_reset_bootstrap(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    repo = _paperclip_repo()
    company = await repo.get(owner_id)
    if company is None:
        raise HTTPException(status_code=404, detail="No Paperclip company row for this owner")
    if not company.onboarding_generation:
        raise HTTPException(status_code=409, detail="Row carries no onboarding_generation — nothing to reset")
    # A still-'dispatched' row is resettable ONLY once its claim is provably
    # dead by the SAME threshold the dispatcher's lazy expiry uses — resetting
    # a live claim mid-flight would let the next poll dispatch a second
    # kickoff. Fencing the write on the observed dispatch_id closes the
    # read-then-write window (a re-armed row re-claims on the next poll,
    # seconds later).
    expected_dispatch_id = None
    if company.bootstrap_state == "dispatched":
        if not is_dispatch_stale(company.bootstrap_dispatched_at):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Bootstrap dispatch is still in flight (claimed under "
                    f"{STALE_DISPATCH_AFTER_S // 60} minutes ago) — refusing to reset"
                ),
            )
        expected_dispatch_id = company.bootstrap_dispatch_id
        if not expected_dispatch_id:
            raise HTTPException(
                status_code=409,
                detail="Dispatched row carries no bootstrap_dispatch_id to fence the reset on",
            )
    elif company.bootstrap_state != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"Bootstrap is '{company.bootstrap_state}', not 'failed' or an expired 'dispatched' — nothing to reset",
        )
    landed = await repo.reset_bootstrap_for_retry(
        owner_id,
        onboarding_generation=company.onboarding_generation,
        expected_dispatch_id=expected_dispatch_id,
    )
    if not landed:
        raise HTTPException(
            status_code=409,
            detail="Reset did not land — row changed under us (new generation or state); re-read and retry",
        )
    return {"ok": True}


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
    description="Delete an agent while preserving its durable company work.",
)
@audit_admin_action("agent.delete")
async def admin_agent_delete(
    user_id: str,
    agent_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    from core.services.paperclip_agent_lifecycle import delete_agent_everywhere

    owner_id, _ = await resolve_admin_owner_id(user_id)
    try:
        return await delete_agent_everywhere(owner_id=owner_id, agent_id=agent_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


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


# ---------------------------------------------------------------------------
# Account wipe (docs/superpowers/specs/2026-07-14-admin-account-wipe-design.md)
# ---------------------------------------------------------------------------
#
# Deliberately NOT wrapped with @audit_admin_action like the rest of this
# file — that decorator's audit payload is built from the *request* (the
# body/capture_params), but the design spec wants the wipe's audit row to
# carry the *report* (what actually happened, per step). The execute
# endpoint below writes the admin-actions row itself, after the teardown
# runs, with the report as the payload. The preview endpoint is read-only
# and intentionally writes no audit row at all — it deletes nothing.


@router.post(
    "/users/{owner_id}/wipe/preview",
    description=(
        "Read-only dry-run: enumerate every resource that exists for the owner "
        "and would be torn down by a wipe (Paperclip company + Aurora user, "
        "ECS service + EFS, Bedrock AIP(s) + IAM role, Metronome, Stripe, "
        "DDB rows, Clerk user). Calls no delete method. Personal owners only — "
        "org owners get 400 org_wipe_unsupported."
    ),
)
async def admin_wipe_preview(
    owner_id: str,
    auth: AuthContext = Depends(require_platform_admin),
):
    try:
        report = await preview_wipe(owner_id)
    except OrgWipeUnsupportedError:
        raise HTTPException(status_code=400, detail="org_wipe_unsupported")
    except BootstrapAccountProtectedError:
        raise HTTPException(status_code=400, detail="bootstrap_account_protected")
    return report.to_dict()


@router.post(
    "/users/{owner_id}/wipe",
    description=(
        "Execute the full, audited account teardown. Requires `confirm_email` "
        "to match one of the account's email addresses — looked up server-side "
        "from Clerk (the account's email addresses), matched case-insensitively, "
        "never trusted from the client — else 400 email_mismatch and nothing is "
        "deleted. Partial-failure-tolerant: a "
        "step failing does not abort the others or fail this request; the "
        "returned report is the source of truth for what remains and what to "
        "re-run. Writes an admin-actions audit row with the full report. "
        "Personal owners only — org owners get 400 org_wipe_unsupported."
    ),
)
async def admin_wipe_execute(
    owner_id: str,
    body: WipeConfirmRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    # Refuse org owners up front — before the email match — so a direct API
    # caller gets the documented org_wipe_unsupported, not a misleading
    # email_mismatch. (resolve_account_emails can't produce a matching email
    # for an org id: clerk_admin.get_user 404s on org_... and there's no users
    # row, so the match below would always fail first. preview_wipe already
    # refuses orgs before anything else; mirror that here.)
    if is_org_owner_id(owner_id):
        raise HTTPException(status_code=400, detail="org_wipe_unsupported")

    # Authoritative email(s) from Clerk (the users-row email is written by an
    # unreliable webhook — see resolve_account_emails). The typed confirm must
    # match one of them, case-insensitively; never trusted from the client alone.
    account_emails = await resolve_account_emails(owner_id)
    typed = body.confirm_email.strip().lower()
    if not typed or not any(typed == e.strip().lower() for e in account_emails):
        raise HTTPException(status_code=400, detail="email_mismatch")

    started = time.monotonic()
    try:
        report = await wipe_account(owner_id, actor_admin_id=auth.user_id, account_emails=account_emails)
    except OrgWipeUnsupportedError:
        raise HTTPException(status_code=400, detail="org_wipe_unsupported")
    except BootstrapAccountProtectedError:
        raise HTTPException(status_code=400, detail="bootstrap_account_protected")
    elapsed_ms = int((time.monotonic() - started) * 1000)

    report_dict = report.to_dict()
    has_failures = any(step["status"] == "failed" for step in report_dict["steps"])

    try:
        await admin_actions_repo.create(
            admin_user_id=auth.user_id,
            target_user_id=owner_id,
            action="account_wipe",
            payload=report_dict,
            result="partial_failure" if has_failures else "success",
            audit_status="written",
            http_status=200,
            elapsed_ms=elapsed_ms,
            error_message=None,
            user_agent=request.headers.get("user-agent", "unknown"),
            ip=extract_client_ip(request),
        )
    except Exception:  # noqa: BLE001 — fail-closed like @audit_admin_action (S1)
        # The teardown already ran (irreversible side effects) -- an audit
        # write failure must not mask that by 500ing an otherwise-successful
        # response. Log loudly and still return the report; wipe_account is
        # idempotent, so a re-run (which would also re-attempt the audit
        # write) is safe if the operator wants to confirm it landed.
        logger.critical(
            "ADMIN_AUDIT_PANIC action=account_wipe admin=%s target=%s",
            auth.user_id,
            owner_id,
            exc_info=True,
        )

    return report_dict
