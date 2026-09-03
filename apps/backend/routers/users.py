"""User API endpoints."""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.auth import get_current_user, resolve_owner_id, AuthContext
from core.config import settings
from core.repositories import admin_actions_repo, billing_repo, user_repo
from core.services.admin_audit import extract_client_ip
from schemas.user_schemas import SyncUserRequest, SyncUserResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/sync",
    response_model=SyncUserResponse,
    summary="Sync user from Clerk",
    description="Creates or returns the user record based on the authenticated Clerk user. Idempotent.",
    operation_id="sync_user",
    responses={
        401: {"description": "Missing or invalid Clerk JWT token"},
        500: {"description": "Database error"},
    },
)
async def sync_user(
    body: SyncUserRequest | None = None,
    auth: AuthContext = Depends(get_current_user),
):
    user_id = auth.user_id

    existing = await user_repo.get(user_id)

    # The JWT's `email` claim is the same primary address the Clerk
    # `user.created` webhook would send, and until now that webhook was the
    # ONLY writer of this column. `launch_referral._email_for_owner` reads it
    # from inside a Stripe webhook, where there is no JWT to fall back on — so
    # a row without it silently kills referral counting for that owner. Write
    # it here too, and heal rows that predate this (the rebrand left the Clerk
    # webhook pointing at the retired domain, so every signup in that window
    # has an email-less row).
    if not existing:
        try:
            await user_repo.put(user_id, auth.email)
            status = "created"
        except Exception as e:
            logger.error("Database error on user sync for %s: %s", user_id, e)
            raise HTTPException(status_code=500, detail="Database operation failed")
    else:
        status = "exists"
        if not existing.get("email"):
            try:
                await user_repo.backfill_email(user_id, auth.email)
            except Exception:
                # Never fail a sync over the heal: a bug here would 500 sync for
                # every existing user, and sync is on the critical path. The
                # client latches success in sessionStorage on any resolved
                # response, so this will NOT be retried until a new browser
                # session — which is why `launch_referral._email_for_owner`
                # falls back to Clerk rather than trusting this column.
                logger.exception("users/sync: email backfill failed for %s", user_id)

    # The canonical write path for provider_choice is POST
    # /billing/trial-checkout, which persists synchronously to billing_accounts
    # before creating the Stripe Checkout session. /users/sync is a pure
    # user-row sync. The ``body`` parameter is still accepted (so old
    # frontends that send ``provider_choice``/``byo_provider`` keep working),
    # but the fields are silently ignored on the server.

    # Note: no billing-account creation here. /users/sync fires from multiple
    # places (ChatLayout mount, onboarding, settings), and the caller's JWT
    # may be in a transient personal context while Clerk is mid-activation of
    # an org. Creating a billing row here would produce phantom
    # personal-context rows for users who are actually org members. Billing is
    # created lazily by POST /billing/checkout (the explicit "subscribe" signal)
    # which is also gated on require_org_admin, so only the first admin click
    # through Stripe Checkout ever writes the row.
    #
    # Container provisioning is also handled elsewhere (GET /container/status
    # + ProvisioningStepper) for the same reason.

    return {"status": status, "user_id": user_id}


@router.get(
    "/me",
    summary="Get the authenticated user's record",
    description=(
        "Returns the authenticated user's id, plus provider_choice/byo_provider "
        "for backwards compatibility. The canonical store lives on billing_repo "
        "(per-owner); this endpoint reads from billing_repo by resolved owner_id "
        "but still surfaces the fields here so existing clients (ControlSidebar, "
        "LLMPanel, OutOfCreditsBanner, ControlPanelRouter) keep working until "
        "they migrate to GET /billing/account."
    ),
    operation_id="get_me",
    responses={401: {"description": "Missing or invalid Clerk JWT token"}},
)
async def get_me(auth: AuthContext = Depends(get_current_user)) -> dict:
    user = await user_repo.get(auth.user_id)

    # Read provider_choice / byo_provider from billing_accounts (per-owner,
    # not per-user). We surface a copy on /me too for backwards compatibility
    # with frontend code that still gates UI on these fields (e.g.
    # ControlSidebar, ControlPanelRouter, LLMPanel, OutOfCreditsBanner). New
    # clients should prefer GET /billing/account.
    owner_id = resolve_owner_id(auth)
    billing_row = await billing_repo.get_by_owner_id(owner_id)
    provider_choice = (billing_row or {}).get("provider_choice")
    byo_provider = (billing_row or {}).get("byo_provider")

    if not user:
        # Frontend treats absent record as "not yet synced" — return an
        # empty shape so the panel's Loading state can resolve cleanly.
        return {
            "user_id": auth.user_id,
            "provider_choice": provider_choice,
            "byo_provider": byo_provider,
        }
    return {
        "user_id": auth.user_id,
        "provider_choice": provider_choice,
        "byo_provider": byo_provider,
    }


class DeleteAccountRequest(BaseModel):
    """Typed-confirm body for self-serve deletion (mirrors the admin wipe)."""

    confirm_email: str


@router.post(
    "/me/delete",
    summary="Delete the caller's account",
    description=(
        "Self-serve, irreversible account deletion (App Store guideline "
        "5.1.1(v)). Personal accounts only; runs the same ordered teardown "
        "as the admin account wipe — container, workspace, Paperclip, "
        "billing, and finally the Clerk user."
    ),
    operation_id="delete_my_account",
    responses={
        400: {"description": "Email confirmation mismatch or protected account"},
        403: {"description": "Org accounts cannot self-delete"},
    },
)
async def delete_my_account(
    body: DeleteAccountRequest,
    request: Request,
    auth: AuthContext = Depends(get_current_user),
):
    # Lazy import: the wipe orchestrator pulls the whole teardown surface
    # (ECS/EFS/Paperclip/Stripe clients) — keep it off users.py import time.
    from core.services.account_wipe import (
        BootstrapAccountProtectedError,
        OrgWipeUnsupportedError,
        resolve_account_emails,
        wipe_account,
    )

    # Personal owners only — an org teardown has per-member fan-out the wipe
    # orchestrator deliberately refuses (see account_wipe module docstring).
    if auth.is_org_context:
        raise HTTPException(
            status_code=403,
            detail=f"Workspace accounts can't be deleted in-app — contact {settings.EMAIL_SUPPORT_ADDRESS}.",
        )
    owner_id = auth.user_id

    # Authoritative email(s) from Clerk; the typed confirm must match one,
    # case-insensitively — same guard the admin wipe uses.
    account_emails = await resolve_account_emails(owner_id)
    typed = body.confirm_email.strip().lower()
    if not typed or not any(typed == e.strip().lower() for e in account_emails):
        raise HTTPException(status_code=400, detail="email_mismatch")

    started = time.monotonic()
    try:
        report = await wipe_account(owner_id, actor_admin_id=owner_id, account_emails=account_emails)
    except OrgWipeUnsupportedError:
        raise HTTPException(status_code=403, detail="org_deletion_unsupported")
    except BootstrapAccountProtectedError:
        raise HTTPException(status_code=400, detail="account_protected")
    elapsed_ms = int((time.monotonic() - started) * 1000)

    report_dict = report.to_dict()
    has_failures = any(step["status"] == "failed" for step in report_dict["steps"])
    # (Audit below runs BEFORE the failure gate so a partial teardown is
    # always recorded; the raise after it keeps the client honest.)
    # Audit like the admin wipe (actor == target marks it self-serve). An
    # audit-write failure must not mask the already-run teardown.
    try:
        await admin_actions_repo.create(
            admin_user_id=owner_id,
            target_user_id=owner_id,
            action="account_wipe_self",
            payload=report_dict,
            result="partial_failure" if has_failures else "success",
            audit_status="written",
            http_status=200,
            elapsed_ms=elapsed_ms,
            error_message=None,
            user_agent=request.headers.get("user-agent", "unknown"),
            ip=extract_client_ip(request),
        )
    except Exception:  # noqa: BLE001 — mirror the admin wipe's audit posture
        logger.critical("SELF_WIPE_AUDIT_PANIC target=%s", owner_id, exc_info=True)

    # A partial teardown must NOT report success: the Clerk user (always the
    # LAST step) may still exist, so "deleted" would sign the user out of an
    # account that can regrow on the next /container/status poll. The wipe is
    # idempotent — an honest error lets the client simply retry.
    if has_failures:
        raise HTTPException(status_code=502, detail="deletion_incomplete")

    # Never leak the step-by-step infra report to end users.
    return {"status": "deleted"}
