"""Organization management endpoints — invite creation gate (Gate A)."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user, require_org_admin
from core.observability.metrics import put_metric
from core.repositories import billing_repo
from core.services import clerk_admin
from core.services.provision_gate import has_always_on_entitlement
from core.tenancy_codes import PERSONAL_USER_EXISTS
from schemas.orgs import CreateInvitationRequest, CreateInvitationResponse

logger = logging.getLogger(__name__)

router = APIRouter()


def _has_active_personal_tenancy(account: dict) -> bool:
    """A `billing_accounts` row counts as an active personal tenancy when it
    carries the PAID entitlement (``active``/``trialing``, or the pre-backfill
    legacy row: ``stripe_subscription_id`` set with no status). Deliberately
    excludes ``trial_local`` — a no-card trial isn't a paid tenancy that
    should block joining an org. We do NOT call Stripe to verify the live
    status: conservatively block the invite and let the user cancel via the
    Stripe portal first. Same status set as always-on entitlement, so it
    delegates to the canonical predicate.
    """
    return has_always_on_entitlement(account)


@router.post(
    "/{org_id}/invitations",
    response_model=CreateInvitationResponse,
    status_code=201,
    summary="Create an org invitation",
    description=(
        "Refuses with 409 if the invitee already has an active or trialing "
        "personal Isol8 subscription. Otherwise forwards to Clerk's "
        "createInvitation API."
    ),
    operation_id="create_org_invitation",
)
async def create_invitation(
    org_id: str,
    body: CreateInvitationRequest,
    auth: AuthContext = Depends(get_current_user),
) -> CreateInvitationResponse:
    # Caller must be an admin of THIS org. require_org_admin only checks
    # the role within the active org; the path-vs-token org_id match
    # closes the cross-org admin escalation gap.
    require_org_admin(auth)
    if auth.org_id != org_id:
        raise HTTPException(403, "Cannot invite to a different org")

    invitee_email = body.email.lower()

    # Tenancy invariant: refuse if invitee has an active personal sub.
    existing = await clerk_admin.find_user_by_email(invitee_email)
    if existing is not None:
        account = await billing_repo.get_by_owner_id(existing["id"])
        if account and _has_active_personal_tenancy(account):
            put_metric("orgs.invitation.blocked", dimensions={"reason": PERSONAL_USER_EXISTS})
            logger.info(
                "orgs.invitation.blocked owner_id=%s personal_status=%s legacy_sub=%s",
                existing["id"],
                account.get("subscription_status"),
                bool(account.get("stripe_subscription_id")),
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": PERSONAL_USER_EXISTS,
                    # body.email preserves the inviter's typed casing for the
                    # human-readable message; invitee_email (above) is lowercased
                    # for the Clerk lookup itself.
                    "message": (
                        f"{body.email} already has an active personal Lightbulb AI "
                        "subscription. They must cancel it before they can "
                        "be invited to an organization."
                    ),
                },
            )

    try:
        invite = await clerk_admin.create_organization_invitation(
            org_id=org_id,
            email=invitee_email,
            role=body.role,
            inviter_user_id=auth.user_id,
        )
    except HTTPException:
        put_metric("orgs.invitation.failed", dimensions={"reason": "clerk_error"})
        raise
    put_metric("orgs.invitation.created", dimensions={"role": body.role})
    logger.info(
        "orgs.invitation.created org_id=%s invitee=%s role=%s invitation_id=%s",
        org_id,
        invitee_email,
        body.role,
        invite["id"],
    )

    # Seats include pending invitations ("no free members"), so the bill must
    # move now. There is no organizationInvitation.* webhook handler at all,
    # and Clerk has no webhook endpoint configured anyway (#1465), so this is
    # the only place in the system that observes an invite being sent.
    #
    # Fire-and-forget, matching the reconcile in routers/container.py: the
    # invite already succeeded in Clerk, the response carries no seat data,
    # and both /billing/account and team-checkout recompute the seat count
    # LIVE from Clerk rather than reading the Stripe quantity this updates —
    # so nothing downstream can observe a stale value. Awaiting it would only
    # add two Clerk reads plus a Stripe write to the admin's invite latency.
    from core.services.billing_service import sync_team_seat_quantity

    asyncio.create_task(sync_team_seat_quantity(org_id))

    return CreateInvitationResponse(invitation_id=invite["id"])
