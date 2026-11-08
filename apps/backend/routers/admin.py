"""Admin dashboard router — /api/v1/admin/*

Every endpoint is gated by Depends(require_platform_admin). The surface is for
human Isol8 operators authenticated by Clerk; customer agents do not receive a
parallel credential path into platform administration.
Mutation endpoints, and the per-user reads under /users/{user_id}/*, are
wrapped with @audit_admin_action so a row lands in DDB synchronously
(fail-closed). Container + billing actions also
accept an Idempotency-Key header to short-circuit double clicks.

Read endpoints delegate to admin_service (composition layer).
Mutation endpoints call the underlying service (hermes_provisioning,
billing_service, clerk_admin) directly — admin_service
intentionally doesn't compose those because each is a distinct side-effect.
"""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.auth import AuthContext, is_org_owner_id, require_platform_admin
from core.repositories import (  # noqa: F401 — admin_actions_repo is patch target for tests
    admin_actions_repo,
    container_repo,
    user_repo,
)
from core.services import (
    admin_service,
    billing_service,
    clerk_admin,
    system_health,
)
from core.services.account_wipe import (
    OrgWipeUnsupportedError,
    preview_wipe,
    resolve_account_emails,
    resolve_workspace_memberships,
    wipe_account,
)
from core.services.workspace_wipe import (
    AmbiguousWorkspaceError,
    StaleWorkspacePreviewError,
    WorkspaceConfirmMismatchError,
    WorkspaceWipeError,
    preview_workspace_wipe,
    resolve_workspace_org,
    wipe_workspace,
)
from core.services.admin_audit import audit_admin_action, extract_client_ip
from core.services.admin_service import resolve_admin_owner_id
from core.services.runtime_rpc import config_patch_roster
from core.services.webhook_dedup import idempotency
from core.services.provision_dispatch import dispatch_provision, dispatch_reconcile
from schemas.config import ConfigPatchRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _workspace_repo():
    """Local import so ``patch("core.repositories.workspace_repo.WorkspaceRepo", ...)``
    (the pattern every existing test double in this codebase already uses)
    keeps working -- mirrors ``admin_service``'s own local-import idiom
    (``core/services/admin_service.py:264-273``, itself mirroring
    ``readiness_state.py:102-110``).
    """
    from core.repositories.workspace_repo import WorkspaceRepo

    return WorkspaceRepo()


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class IssueCreditRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, description="Credit amount in cents")
    reason: str = Field(..., min_length=1, description="Reason for credit (audited)")
    request_id: str = Field(
        ...,
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "One id per admin submission. A retry of the same submission reuses it and is "
            "deduplicated; a second deliberate credit sends a new one and is a new credit. Required: "
            "without it two identical credits would merge into one Stripe write."
        ),
    )


class MarkInvoiceResolvedRequest(BaseModel):
    invoice_id: str = Field(..., description="Stripe invoice id")


class WipeConfirmRequest(BaseModel):
    confirm_email: str = Field(
        ...,
        description="Type-to-confirm: must match the account's email on file, checked server-side.",
    )
    confirm_workspace: str | None = Field(
        None,
        description=(
            "Required when the account belongs to a workspace: must match the (first) workspace's "
            "name (its org id when Clerk has no name), checked server-side."
        ),
    )
    confirm_teardown: list[str] = Field(
        default_factory=list,
        description=(
            "Required for every workspace the preview marks `last_member`: that workspace's name "
            "(org id when unnamed). Removing its last member tears it down -- the subscription is "
            "cancelled and the runtime deleted -- so each one is confirmed on its own."
        ),
    )


class WorkspaceWipePreviewRequest(BaseModel):
    layers: list[str] | None = Field(
        None,
        description="Subset of wipe layers to enumerate (default: all but billing).",
    )
    include_billing: bool = Field(
        False,
        description="Also enumerate the billing layer (shown preserved/skipped unless set).",
    )
    delete_shared_members: bool = Field(
        False,
        description=(
            "Also delete the accounts of members who belong to another org. Default: they keep "
            "their account and only lose this workspace's membership."
        ),
    )


class WorkspaceWipeConfirmRequest(BaseModel):
    confirm_org: str = Field(
        ...,
        description="Type-to-confirm: must equal the workspace org id in the path, checked server-side.",
    )
    layers: list[str] | None = Field(
        None,
        description="Subset of wipe layers to execute (default: all but billing).",
    )
    include_billing: bool = Field(
        False,
        description="Arm the billing layer (requires confirm_billing). Default preserves billing/Stripe.",
    )
    confirm_billing: str | None = Field(
        None,
        description="Must equal 'DELETE-BILLING' when include_billing is set.",
    )
    preview_fingerprint: str = Field(
        ...,
        description="Fingerprint from the preview report being confirmed; execute aborts on drift.",
    )
    delete_shared_members: bool = Field(
        False,
        description=(
            "Also delete the accounts of members who belong to another org. Default: they keep "
            "their account and only lose this workspace's membership."
        ),
    )


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
@audit_admin_action("user.read.overview")
async def admin_user_overview(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    return await admin_service.get_overview(user_id)


@router.get(
    "/users/{user_id}/agents",
    description="List a user's agents via gateway RPC. Returns container_status when not running.",
)
@audit_admin_action("user.read.agents")
async def admin_user_agents(
    user_id: str,
    request: Request,
    cursor: str | None = Query(None),
    limit: int = Query(50, le=200),
    auth: AuthContext = Depends(require_platform_admin),
):
    return await admin_service.list_user_agents(user_id, cursor=cursor, limit=limit)


@router.get(
    "/users/{user_id}/agents/{agent_id}",
    description="Full agent detail (config, sessions, skills) with secret redaction. 409 when container not running.",
)
@audit_admin_action("user.read.agent_detail")
async def admin_agent_detail(
    user_id: str,
    request: Request,
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
@audit_admin_action("user.read.posthog")
async def admin_user_posthog(
    user_id: str,
    request: Request,
    limit: int = Query(100, le=500),
    auth: AuthContext = Depends(require_platform_admin),
):
    return await admin_service.get_posthog_timeline(user_id, limit=limit)


@router.get(
    "/users/{user_id}/logs",
    description="Inline CloudWatch Logs filtered to the user with cursor pagination.",
)
@audit_admin_action("user.read.logs")
async def admin_user_logs(
    user_id: str,
    request: Request,
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
@audit_admin_action("user.read.cloudwatch_url")
async def admin_user_cloudwatch_url(
    user_id: str,
    request: Request,
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
    description="Reconcile the workspace with billing: re-derives the owner's credentials, "
    "rewrites every agent profile and re-specs the AgentCore runtime on drift "
    "(or dispatches the provision funnel when there is no live runtime). Runs as a durable "
    "Step Functions execution. Idempotency-Key supported.",
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
    # Durable: an idle-session wait can outlast the request, so a Step Functions reconcile
    # execution does the work. dispatch_reconcile never raises.
    await dispatch_reconcile(owner_id)
    return {"status": "reconciling", "owner_id": owner_id}


# ---------------------------------------------------------------------------
# Provisioning (read + retry/reset-bootstrap levers)
# ---------------------------------------------------------------------------


@router.get(
    "/users/{user_id}/provisioning",
    description=(
        "Provisioning truth for one owner: readiness contract "
        "(compute_status), containers-row error/lease fields, workspace "
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
        "error-row retry AND the wedge rescue (a 'provisioning' row whose SFN "
        "execution closed without a terminal write); a no-op while an execution "
        "is RUNNING. Unlike the "
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
    if not await dispatch_provision(owner_id):
        raise HTTPException(status_code=502, detail="Provisioning could not be started — nothing was dispatched")
    fresh = await container_repo.get_by_owner_id(owner_id)
    return {
        "ok": True,
        "status": (fresh or {}).get("status"),
        "substatus": (fresh or {}).get("substatus"),
    }


# ---------------------------------------------------------------------------


# A billing action that changed nothing must not read as "succeeded": the admin
# UI prints the 409 detail, and the audit row records an error instead of a success.
_BILLING_NOOP_REASONS = {
    "voice_plan_managed": "Manage voice-plan billing separately; no pause was applied.",
    "no_billing_account": "This workspace has no billing account — nothing was changed.",
    "no_subscription": "This workspace has no Stripe subscription — nothing was changed.",
    "no_customer": "This workspace has no Stripe customer — nothing was changed.",
    "invoice_not_found": "Stripe has no invoice with that id — nothing was changed.",
    "invoice_customer_mismatch": (
        "That invoice belongs to a different Stripe customer than this workspace — nothing was changed."
    ),
    "already_paid": "That invoice is already paid — nothing was changed.",
    "already_paused": "This subscription is already paused — nothing was changed.",
    "pause_used": "This workspace has already used its one 30-day pause — nothing was changed.",
    "not_paused": "This subscription is not paused — nothing was changed.",
}


def _refuse_billing_noop(result: dict) -> dict:
    reason = _BILLING_NOOP_REASONS.get(result.get("status"))
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    return result


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
    return _refuse_billing_noop(await billing_service.cancel_subscription_for_owner(owner_id))


@router.post(
    "/users/{user_id}/billing/pause-subscription",
    description="Pause Stripe subscription billing (mark_uncollectible). Undo with resume-subscription.",
)
@idempotency()
@audit_admin_action("billing.pause_subscription")
async def admin_billing_pause_subscription(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    return _refuse_billing_noop(await billing_service.pause_subscription_for_owner(owner_id))


@router.post(
    "/users/{user_id}/billing/resume-subscription",
    description="Resume Stripe subscription billing paused by pause-subscription.",
)
@idempotency()
@audit_admin_action("billing.resume_subscription")
async def admin_billing_resume_subscription(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    return _refuse_billing_noop(await billing_service.resume_subscription_for_owner(owner_id))


@router.post(
    "/users/{user_id}/billing/founding-team",
    description="Mark the workspace a founding team: a two-year price lock on its subscription.",
)
@idempotency()
@audit_admin_action("billing.founding_team")
async def admin_billing_founding_team(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    owner_id, _ = await resolve_admin_owner_id(user_id)
    return _refuse_billing_noop(await billing_service.mark_founding_team(owner_id))


@router.post(
    "/users/{user_id}/billing/pause-30-days",
    description="The one 30-day pause per company: extend the trial, or pause collection on a live subscription.",
)
@idempotency()
@audit_admin_action("billing.pause_30_days")
async def admin_billing_pause_30_days(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    from core.services.trial_lifecycle import pause_once

    owner_id, _ = await resolve_admin_owner_id(user_id)
    return _refuse_billing_noop(await pause_once(owner_id))


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
    return _refuse_billing_noop(
        await billing_service.issue_credit_for_owner(
            owner_id, amount_cents=body.amount_cents, reason=body.reason, request_id=body.request_id
        )
    )


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
    return _refuse_billing_noop(await billing_service.mark_invoice_resolved(owner_id, body.invoice_id))


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
    description="Fan a config patch out to the user's roster profiles. Patch field redacted in audit row.",
)
@audit_admin_action("config.patch", redact_paths=["patch"])
async def admin_config_patch(
    user_id: str,
    body: ConfigPatchRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    # For org members owner_id is the org_id — patching {user_id} would target a
    # workspace that does not exist (admin-org-owner-id P1).
    owner_id, _ = await resolve_admin_owner_id(user_id)
    # M6 row #13: same S3-profile fan-out as the other two config-patch doors.
    await config_patch_roster(owner_id, body.patch, None)
    return {"status": "patched"}


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
    from core.services.agent_lifecycle import AgentLifecycleConflict, delete_agent_everywhere

    owner_id, _ = await resolve_admin_owner_id(user_id)
    try:
        return await delete_agent_everywhere(owner_id=owner_id, agent_id=agent_id)
    except AgentLifecycleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
        "and would be torn down by a wipe (workspace membership, "
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
    return report.to_dict()


@router.post(
    "/users/{owner_id}/wipe",
    description=(
        "Execute the full, audited account teardown. Requires `confirm_email` "
        "to match one of the account's email addresses — looked up server-side "
        "from Clerk (the account's email addresses), matched case-insensitively, "
        "never trusted from the client — else 400 email_mismatch and nothing is "
        "deleted. When the account belongs to a workspace, `confirm_workspace` "
        "must also match the (first) workspace's name, else 400 "
        "workspace_mismatch; the wipe keeps every company and archives only "
        "this member's memberships. Partial-failure-tolerant: a "
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

    # A member's company is the whole workspace's. The wipe keeps it regardless;
    # this makes the admin name the workspace they are touching, the way they
    # already name the account.
    memberships = await resolve_workspace_memberships(owner_id)
    if memberships:
        first = memberships[0]
        expected = (first["workspace_name"] or first["org_id"]).strip().lower()
        if (body.confirm_workspace or "").strip().lower() != expected:
            raise HTTPException(status_code=400, detail="workspace_mismatch")
    typed_teardown = {name.strip().lower() for name in body.confirm_teardown}
    for m in memberships:
        if m.get("last_member") and (m["workspace_name"] or m["org_id"]).strip().lower() not in typed_teardown:
            raise HTTPException(status_code=400, detail="teardown_unconfirmed")

    started = time.monotonic()
    try:
        report = await wipe_account(owner_id, actor_admin_id=auth.user_id)
    except OrgWipeUnsupportedError:
        raise HTTPException(status_code=400, detail="org_wipe_unsupported")
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


# ---------------------------------------------------------------------------
# Workspace wipe (org-keyed; core.services.workspace_wipe)
# ---------------------------------------------------------------------------
#
# The user-keyed wipe above snapshots rows keyed by the USER id and misses an
# org workspace's runtime (containers/billing/messaging all live under the ORG
# id) — the 2026-09-21 half-wipe. These doors snapshot by ORG id instead.
# Same audit posture as the user wipe: preview writes no audit row, execute
# writes one with the full report (action="account_wipe_workspace").


@router.post(
    "/workspaces/{org_id}/wipe/preview",
    description=(
        "Read-only dry-run: enumerate every resource under the workspace org id "
        "that a workspace wipe would tear down (AgentCore runtime + S3 profiles "
        "under the ORG id, the workspace and member rows, rooms, "
        "secrets/tokens, member Clerk users, Clerk org). Calls no delete method. "
        "Billing is shown preserved/skipped unless include_billing is set."
    ),
)
async def admin_workspace_wipe_preview(
    org_id: str,
    body: WorkspaceWipePreviewRequest | None = None,
    auth: AuthContext = Depends(require_platform_admin),
):
    body = body or WorkspaceWipePreviewRequest()
    try:
        report = await preview_workspace_wipe(
            org_id,
            layers=body.layers,
            include_billing=body.include_billing,
            delete_shared_members=body.delete_shared_members,
        )
    except WorkspaceWipeError as e:
        raise HTTPException(status_code=400, detail=f"workspace_wipe_refused: {e}")
    return report.to_dict()


@router.post(
    "/workspaces/{org_id}/wipe",
    description=(
        "Execute the audited workspace teardown for the selected layers. Requires "
        "`confirm_org` to equal the workspace org id, and `preview_fingerprint` "
        "from the preview being confirmed (execute re-snapshots and aborts on "
        "drift). Billing/Stripe is preserved by default; arming it needs "
        "`include_billing` + `confirm_billing='DELETE-BILLING'`. Partial-failure-"
        "tolerant like the user wipe; writes an admin-actions audit row with the "
        "full report."
    ),
)
async def admin_workspace_wipe_execute(
    org_id: str,
    body: WorkspaceWipeConfirmRequest,
    request: Request,
    auth: AuthContext = Depends(require_platform_admin),
):
    # Error details are "<code>: <message>"; the admin UI keys on the code
    # before the separator.
    if not body.preview_fingerprint.strip():
        raise HTTPException(status_code=400, detail="workspace_wipe_refused: preview_fingerprint required")
    started = time.monotonic()
    try:
        report = await wipe_workspace(
            org_id,
            actor_admin_id=auth.user_id,
            confirm_org=body.confirm_org,
            layers=body.layers,
            include_billing=body.include_billing,
            confirm_billing=body.confirm_billing,
            expected_fingerprint=body.preview_fingerprint,
            delete_shared_members=body.delete_shared_members,
        )
    except StaleWorkspacePreviewError as e:
        raise HTTPException(status_code=409, detail=f"stale_preview: {e}")
    except WorkspaceConfirmMismatchError as e:
        raise HTTPException(status_code=400, detail=f"workspace_mismatch: {e}")
    except WorkspaceWipeError as e:
        raise HTTPException(status_code=400, detail=f"workspace_wipe_refused: {e}")
    elapsed_ms = int((time.monotonic() - started) * 1000)

    report_dict = report.to_dict()
    has_failures = any(step["status"] == "failed" for step in report_dict["steps"])

    try:
        await admin_actions_repo.create(
            admin_user_id=auth.user_id,
            target_user_id=org_id,
            action="account_wipe_workspace",
            payload=report_dict,
            result="partial_failure" if has_failures else "success",
            audit_status="written",
            http_status=200,
            elapsed_ms=elapsed_ms,
            error_message=None,
            user_agent=request.headers.get("user-agent", "unknown"),
            ip=extract_client_ip(request),
        )
    except Exception:  # noqa: BLE001 — same fail-open-response posture as the user wipe
        logger.critical(
            "ADMIN_AUDIT_PANIC action=account_wipe_workspace admin=%s target=%s",
            auth.user_id,
            org_id,
            exc_info=True,
        )

    return report_dict


@router.post(
    "/users/{user_id}/wipe/workspace-resolve",
    description=(
        "Resolve which workspace a user-keyed wipe would mean: returns the org "
        "id when the user belongs to exactly one workspace, else 400 "
        "ambiguous_workspace (pass confirm_org to the workspace wipe). "
        "Read-only; writes no audit row."
    ),
)
async def admin_workspace_resolve(
    user_id: str,
    auth: AuthContext = Depends(require_platform_admin),
):
    try:
        org_id = await resolve_workspace_org(user_id)
    except (AmbiguousWorkspaceError, WorkspaceConfirmMismatchError) as e:
        raise HTTPException(status_code=400, detail=f"ambiguous_workspace: {e}")
    return {"user_id": user_id, "org_id": org_id}
