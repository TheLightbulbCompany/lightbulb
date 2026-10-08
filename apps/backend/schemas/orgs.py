"""Pydantic schemas for the organizations API surface.

Two ways in, both as ``org:member`` (which is why no schema here carries a
role): ONE shareable join link per workspace, and email invitations. Email
invitations were removed on 2026-09-10 with per-seat billing (an email+role
invite modelled a seat grant) and came back on 2026-10-08 next to the link:
billing is per workspace now, so that reason is gone.
"""

from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class JoinLinkStatus(BaseModel):
    """A workspace's live join link, so an admin can re-copy it any time.

    ``exists=True`` with ``join_url=None`` is a link minted before the token was
    stored encrypted (or one the current key cannot decrypt); the UI reads it
    as "regenerate once to get a copyable link".
    """

    exists: bool
    join_url: str | None = None
    created_at: str | None = None
    created_by: str | None = None


class CreateJoinLinkResponse(BaseModel):
    """The freshly minted link; ``JoinLinkStatus`` returns the same URL later."""

    join_url: str
    created_at: str
    created_by: str


class RedeemJoinLinkRequest(BaseModel):
    """Body for POST /api/v1/orgs/{org_id}/join.

    The token travels in the BODY, never in the URL: a query string or path
    segment on the API host lands in access logs, and this is a credential.
    """

    token: str = Field(min_length=1, max_length=512)


class RedeemJoinLinkResponse(BaseModel):
    """``already_member`` is a success, not an error — redeeming is idempotent."""

    status: str
    org_id: str
    org_name: str = ""
    org_slug: str = ""


class CreateInvitationRequest(BaseModel):
    """Body for POST /api/v1/orgs/{org_id}/invitations. Lower-cased by the service."""

    email: EmailStr


class Invitation(BaseModel):
    """A pending Clerk email invitation. Timestamps are ISO 8601 UTC."""

    id: str
    email: str
    created_at: str
    expires_at: str | None = None
    status: Literal["pending"] = "pending"


class InvitationList(BaseModel):
    """Pending invitations only, newest first."""

    invitations: list[Invitation]
