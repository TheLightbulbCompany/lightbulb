"""User API endpoints."""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from html import escape
from pydantic import BaseModel

from core.auth import get_current_user, resolve_owner_id, AuthContext
from core.repositories import admin_actions_repo, billing_repo, user_repo
from core.services.admin_audit import extract_client_ip
from schemas.user_schemas import SyncUserRequest, SyncUserResponse, UserPrefs, UserPrefsUpdate

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


def _prefs(row: dict | None) -> UserPrefs:
    return UserPrefs(
        timezone=user_repo.timezone_of(row),
        dailySummary=user_repo.daily_summary_on(row),
        awayEmail=user_repo.away_email_on(row),
        chatTheme=user_repo.chat_theme_of(row),
        sessionsReachableFromBackgroundWork=user_repo.sessions_reachable_from_background_work(row),
        sessionNotifications=user_repo.session_notifications(row),
    )


@router.get("/me/prefs", response_model=UserPrefs, operation_id="get_my_prefs")
async def get_my_prefs(auth: AuthContext = Depends(get_current_user)) -> UserPrefs:
    """The caller's per-human prefs: IANA ``timezone``, the ``dailySummary`` digest
    toggle, the account's ``chatTheme`` (null when never set), and the agent-session
    switch ``sessionsReachableFromBackgroundWork`` (on by default)."""
    return _prefs(await user_repo.get(auth.user_id))


@router.put("/me/prefs", response_model=UserPrefs, operation_id="update_my_prefs")
async def update_my_prefs(body: UserPrefsUpdate, auth: AuthContext = Depends(get_current_user)) -> UserPrefs:
    """Set any of ``timezone`` / ``dailySummary`` / ``chatTheme``; omitted fields
    are unchanged."""
    if not await user_repo.get(auth.user_id):
        await user_repo.put(auth.user_id, auth.email)
    await user_repo.set_prefs(
        auth.user_id,
        timezone=body.timezone,
        daily_summary=body.dailySummary,
        away_email=body.awayEmail,
        chat_theme=body.chatTheme,
        sessions_reachable_from_background_work=body.sessionsReachableFromBackgroundWork,
        session_notifications=body.sessionNotifications.model_dump() if body.sessionNotifications else None,
    )
    return _prefs(await user_repo.get(auth.user_id))


def _unsubscribe_page(message: str, *, form_token: str | None = None, status_code: int = 200) -> HTMLResponse:
    form = (
        f'<form method="post" action="?token={escape(form_token)}"><button type="submit">Unsubscribe</button></form>'
        if form_token
        else ""
    )
    return HTMLResponse(
        f'<!doctype html><meta name="viewport" content="width=device-width"><title>Lightbulb</title>'
        f'<div style="font-family:sans-serif;max-width:480px;margin:48px auto;padding:0 16px">'
        f"<p>{escape(message)}</p>{form}</div>",
        status_code=status_code,
    )


@router.get("/digest/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
async def digest_unsubscribe_page(token: str = Query(..., max_length=2048)) -> HTMLResponse:
    """The email footer link. A GET never changes anything (link scanners
    prefetch it); it shows a one-button form that POSTs the same token."""
    from core.services.daily_digest import verify_unsubscribe_token

    if verify_unsubscribe_token(token) is None:
        return _unsubscribe_page("This unsubscribe link is invalid or has expired.", status_code=400)
    return _unsubscribe_page("Stop the Lightbulb daily summary email and notification?", form_token=token)


@router.post("/digest/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
async def digest_unsubscribe(token: str = Query(..., max_length=2048)) -> HTMLResponse:
    """RFC 8058 one-click target (``List-Unsubscribe-Post``). Unauthenticated: the
    signed, expiring, purpose-scoped token is the only credential, and all it can
    do is turn the "Daily summary" pref off. Idempotent."""
    from core.services.daily_digest import verify_unsubscribe_token

    user_id = verify_unsubscribe_token(token)
    if user_id is None:
        return _unsubscribe_page("This unsubscribe link is invalid or has expired.", status_code=400)
    await user_repo.set_prefs(user_id, daily_summary=False)
    return _unsubscribe_page("You're unsubscribed from the daily summary. Turn it back on in Settings.")


@router.post("/me/active", status_code=204, operation_id="mark_me_active")
async def mark_me_active(auth: AuthContext = Depends(get_current_user)) -> None:
    """Presence for the away email (T15): web and the phone call this every two minutes
    while the app is visible / in the foreground, never from a hidden tab."""
    await user_repo.touch_active(auth.user_id)


@router.get("/away-email/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
async def away_email_unsubscribe_page(token: str = Query(..., max_length=2048)) -> HTMLResponse:
    """The away email's footer link (T15). A GET changes nothing; it shows the POST form."""
    from core.services.needs_you_email import verify_unsubscribe_token

    if verify_unsubscribe_token(token) is None:
        return _unsubscribe_page("This unsubscribe link is invalid or has expired.", status_code=400)
    return _unsubscribe_page("Stop emails when something needs you and you're away?", form_token=token)


@router.post("/away-email/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
async def away_email_unsubscribe(token: str = Query(..., max_length=2048)) -> HTMLResponse:
    """RFC 8058 one-click target. The token can only turn the away email off. Idempotent."""
    from core.services.needs_you_email import verify_unsubscribe_token

    user_id = verify_unsubscribe_token(token)
    if user_id is None:
        return _unsubscribe_page("This unsubscribe link is invalid or has expired.", status_code=400)
    await user_repo.set_prefs(user_id, away_email=False)
    return _unsubscribe_page("You won't get these emails any more. Turn them back on in Settings.")


async def _org_blocker(user_id: str, *, active_org_id: str | None, clerk_admin) -> str | None:
    """Why deleting ``user_id`` would orphan a workspace, or None.

    Per org the caller belongs to: sole member — fine only for the active org
    (that workspace is wiped with the account), refused for any other; the
    last admin with other members — refused; a plain member — fine.
    """
    for org in await clerk_admin.list_user_organizations(user_id, limit=100, strict=True):
        members = await clerk_admin.list_organization_members(org["id"], strict=True)
        others = [m for m in members if m.get("user_id") != user_id]
        name = org.get("name") or org["id"]
        if not others:
            if org["id"] != active_org_id:
                return f"You're the only member of {name}. Delete or hand over that workspace first."
        elif org.get("role") == "org:admin" and not any(m.get("role") == "org:admin" for m in others):
            return f"You're the admin of {name}. Make someone else an admin, or delete the workspace first."
    return None


class DeleteAccountRequest(BaseModel):
    """Typed-confirm body for self-serve deletion (mirrors the admin wipe)."""

    confirm_email: str


@router.post(
    "/me/delete",
    summary="Delete the caller's account",
    description=(
        "Self-serve, irreversible account deletion (App Store guideline "
        "5.1.1(v)). In a workspace with other members only the caller "
        "goes (their membership is archived; the workspace keeps running). "
        "The sole member takes the workspace with them, subscription "
        "included. The Clerk user is always deleted last."
    ),
    operation_id="delete_my_account",
    responses={
        400: {"description": "Email confirmation mismatch or protected account"},
        409: {"description": "Workspace changed mid-delete; retry"},
    },
)
async def delete_my_account(
    body: DeleteAccountRequest,
    request: Request,
    auth: AuthContext = Depends(get_current_user),
):
    # Lazy import: the wipe orchestrators pull the whole teardown surface
    # (runtime/Stripe clients) — keep it off users.py import time.
    from core.services import clerk_admin
    from core.services.account_wipe import (
        BootstrapAccountProtectedError,
        OrgWipeUnsupportedError,
        resolve_account_emails,
        wipe_account,
    )
    from core.services.workspace_wipe import (
        OtherMembersRemainError,
        SelfServeBlockedError,
        WorkspaceWipeError,
        wipe_own_workspace,
    )

    owner_id = auth.user_id

    # Authoritative email(s) from Clerk; the typed confirm must match one,
    # case-insensitively — same guard the admin wipe uses.
    account_emails = await resolve_account_emails(owner_id)
    typed = body.confirm_email.strip().lower()
    if not typed or not any(typed == e.strip().lower() for e in account_emails):
        raise HTTPException(status_code=400, detail="email_mismatch")

    # strict everywhere: a Clerk blip must fail, never read as "nobody else here".
    blocker = await _org_blocker(owner_id, active_org_id=auth.org_id, clerk_admin=clerk_admin)
    if blocker:
        raise HTTPException(status_code=409, detail=blocker)

    started = time.monotonic()
    try:
        if not auth.is_org_context:
            report = await wipe_account(owner_id, actor_admin_id=owner_id, account_emails=account_emails)
        else:
            try:
                # The wipe's own snapshot decides "sole member" (one member source).
                report = await wipe_own_workspace(auth.org_id, user_id=owner_id)
            except OtherMembersRemainError:
                # Others remain: the user-keyed wipe archives only this membership
                # (the organizationMembership.deleted path — the workspace's
                # runtime, company and billing are untouched) and deletes the
                # caller's own rows and Clerk user.
                report = await wipe_account(owner_id, actor_admin_id=owner_id, account_emails=account_emails)
    except OrgWipeUnsupportedError:
        raise HTTPException(status_code=403, detail="org_deletion_unsupported")
    except BootstrapAccountProtectedError:
        raise HTTPException(status_code=400, detail="account_protected")
    except SelfServeBlockedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except WorkspaceWipeError:
        # Refused before any step ran (e.g. the workspace changed mid-delete): safe to retry.
        logger.warning("self-delete: workspace wipe refused for %s", owner_id, exc_info=True)
        raise HTTPException(status_code=409, detail="deletion_incomplete")
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
