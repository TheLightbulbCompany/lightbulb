"""User API endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth import get_current_user, resolve_owner_id, AuthContext
from core.repositories import billing_repo, user_repo
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

    if not existing:
        try:
            await user_repo.put(user_id)
            status = "created"
        except Exception as e:
            logger.error("Database error on user sync for %s: %s", user_id, e)
            raise HTTPException(status_code=500, detail="Database operation failed")
    else:
        status = "exists"

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
