"""Pydantic schemas for the organizations API surface.

The email-invitation schemas that used to live here are gone with the endpoint
(``POST /orgs/{org_id}/invitations``). Billing is per workspace, not per seat —
a workspace is one shared container with unlimited members — so an email+role
invitation was modelling a seat grant that does not exist. What replaced it is a
single shareable join link per workspace; everyone who redeems it joins as
``org:member``, which is why no schema here carries a role.
"""

from pydantic import BaseModel, Field


class JoinLinkStatus(BaseModel):
    """Metadata for a workspace's live join link — never the link itself.

    The token is stored hashed, so a minted link can never be redisplayed:
    ``join_url`` is populated ONLY by the mint response
    (``CreateJoinLinkResponse``). ``exists=True`` with no URL is the normal
    steady state, and the UI reads it as "there is a live link; regenerate to
    get a copyable one".
    """

    exists: bool
    created_at: str | None = None
    created_by: str | None = None


class CreateJoinLinkResponse(BaseModel):
    """The one and only time a join link's plaintext crosses the wire."""

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
