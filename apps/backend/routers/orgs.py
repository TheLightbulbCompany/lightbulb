"""Workspace invitation endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user, require_org_admin
from core.observability.metrics import put_metric
from core.services import clerk_admin
from schemas.orgs import CreateInvitationRequest, CreateInvitationResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/{org_id}/invitations",
    response_model=CreateInvitationResponse,
    status_code=201,
    summary="Create a workspace invitation",
    description=("Forwards an admin-authorized workspace invitation to Clerk's createInvitation API."),
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

    return CreateInvitationResponse(invitation_id=invite["id"])
