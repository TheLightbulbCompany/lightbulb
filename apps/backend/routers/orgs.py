"""Workspace join-link endpoints.

Four routes, mounted at ``/api/v1/orgs``:

* ``GET    /{org_id}/join-link``  — admin: is there a live link, and when was it made
* ``POST   /{org_id}/join-link``  — admin: mint/replace it, returns the plaintext ONCE
* ``DELETE /{org_id}/join-link``  — admin: revoke it
* ``POST   /{org_id}/join``       — any signed-in user: redeem a link

This replaced ``POST /{org_id}/invitations`` (Clerk email invitations, with an
``org:admin``/``org:member`` role select). That endpoint was removed, not left
dangling: the frontend form that called it was rejected before it shipped, the
model was wrong for the product (billing is per workspace, not per seat), and
its only Clerk helper went with it. Clerk's own email invitations still exist in
the Clerk dashboard for anyone who wants that flow; we just do not front it.

Authorization:

* The three admin routes reuse the invitation endpoint's exact pattern —
  ``require_org_admin(auth)`` for the role, plus an explicit
  ``auth.org_id != org_id`` check, because ``require_org_admin`` only proves the
  caller is an admin of their ACTIVE org and would otherwise let an admin of one
  workspace mint a link into another.
* ``POST /{org_id}/join`` requires only a signed-in Clerk user. That is
  deliberate and it is the security boundary the link leans on: the link is not
  an auth bypass, it is an authorization grant to somebody who already
  authenticated. The redeemer's active org context is irrelevant — they may be
  in personal mode, or in a different workspace entirely.

Rate limiting: this repo has NO rate-limiting primitive to reuse (no limiter
middleware, no per-caller counter), so none is invented here. The token is 256
CSPRNG bits checked with a single hashed lookup, which is not a guessable
surface; every rejection emits ``orgs.join_link.rejected`` so a burst is visible
in CloudWatch. If a limiter ever lands, the redeem route is where it goes.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user, require_org_admin
from core.services import workspace_join
from schemas.orgs import (
    CreateJoinLinkResponse,
    JoinLinkStatus,
    RedeemJoinLinkRequest,
    RedeemJoinLinkResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_admin_of(auth: AuthContext, org_id: str) -> None:
    """Admin of THIS org, not merely an admin somewhere.

    ``require_org_admin`` checks the role inside the caller's active org (and
    409s a personal caller via ``resolve_owner_id``); the path match closes the
    cross-org escalation gap.
    """
    require_org_admin(auth)
    if auth.org_id != org_id:
        raise HTTPException(403, "Cannot manage a different workspace")


@router.get(
    "/{org_id}/join-link",
    response_model=JoinLinkStatus,
    summary="Whether this workspace has a live join link",
    operation_id="get_org_join_link",
)
async def get_join_link(
    org_id: str,
    auth: AuthContext = Depends(get_current_user),
) -> JoinLinkStatus:
    _require_admin_of(auth, org_id)
    meta = await workspace_join.describe_join_link(org_id)
    if not meta:
        return JoinLinkStatus(exists=False)
    return JoinLinkStatus(exists=True, **meta)


@router.post(
    "/{org_id}/join-link",
    response_model=CreateJoinLinkResponse,
    status_code=201,
    summary="Mint a workspace join link (replaces any previous one)",
    operation_id="create_org_join_link",
)
async def create_join_link(
    org_id: str,
    auth: AuthContext = Depends(get_current_user),
) -> CreateJoinLinkResponse:
    """Regenerating is the revoke: the previous link stops working immediately."""
    _require_admin_of(auth, org_id)
    minted = await workspace_join.mint_join_link(org_id, created_by=auth.user_id)
    return CreateJoinLinkResponse(**minted)


@router.delete(
    "/{org_id}/join-link",
    status_code=204,
    summary="Revoke this workspace's join link",
    operation_id="delete_org_join_link",
)
async def delete_join_link(
    org_id: str,
    auth: AuthContext = Depends(get_current_user),
) -> None:
    _require_admin_of(auth, org_id)
    await workspace_join.revoke_join_link(org_id)


@router.post(
    "/{org_id}/join",
    response_model=RedeemJoinLinkResponse,
    summary="Redeem a workspace join link",
    operation_id="redeem_org_join_link",
)
async def redeem_join_link(
    org_id: str,
    body: RedeemJoinLinkRequest,
    auth: AuthContext = Depends(get_current_user),
) -> RedeemJoinLinkResponse:
    """Join the workspace the link points at, as ``org:member``.

    No admin approval step (owner's decision: open join). Everything downstream
    — the container the new member rides, their Paperclip membership, their
    rooms — is driven by the Clerk membership this creates, via the existing
    ``organizationMembership.created`` webhook and the next
    ``/container/status`` poll. Nothing new is provisioned here.
    """
    result = await workspace_join.redeem_join_link(org_id, body.token, user_id=auth.user_id)
    return RedeemJoinLinkResponse(**result)
