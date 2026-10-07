"""Leads — `GET /api/v1/company/leads`.

The first backend consumer of a tier-2 (``backend_only``) credential. The
Leads tab needs a table the owner can scan and page, which the agent's
MCP-scoped Apollo grant cannot serve; the pasted ``apollo_api`` key can.

Gating copies ``company_money``: org context requires ``org:admin``, and the
owner is the ORG, never the clicking member. Admin because the key is
workspace-scoped and identifies the workspace's longest-standing admin on
Apollo's side — every search is attributed to them, so the people who can
issue one should be the people it is attributed to.

Two contracts, both mirrored from the Money routes so one state machine
serves both tabs:

* **No key ⇒ 200 ``{connected: false, data: []}``**, never a 404/409. The tab
  renders "connect Apollo (dashboard access)" — a 409 would paint it red for
  every owner who has not pasted a key yet, which today is all of them.
* **A provider failure ⇒ 502.** A list rendering "no people found" during an
  Apollo outage — or on a key revoked since it was pasted — is a lie the
  owner acts on.

This route never enriches. Apollo's free search returns masked surnames and
a ``has_email`` flag, not addresses; un-masking spends the owner's credits
and stays behind the agent + an approval.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.services import company_leads

logger = logging.getLogger(__name__)
router = APIRouter()


def _owner(auth: AuthContext) -> str:
    if auth.is_org_context:
        require_org_admin(auth)
    return resolve_owner_id(auth)


@router.get("/leads")
async def leads(
    auth: Annotated[AuthContext, Depends(get_current_user)],
    q: Annotated[str, Query(min_length=2, max_length=200)],
    page: Annotated[int, Query(ge=1, le=company_leads.MAX_PAGE)] = 1,
) -> dict:
    owner_id = _owner(auth)
    try:
        return await company_leads.search_people(owner_id, query=q, page=page)
    except company_leads.ApolloError as e:
        # 401/403 from Apollo = the pasted key no longer works. Say that, not
        # "temporarily unavailable" — the fix is re-pasting, not waiting.
        if e.status in (401, 403):
            raise HTTPException(
                status_code=502,
                detail={"code": "apollo_key_rejected", "message": "Apollo rejected the API key on file."},
            ) from e
        raise HTTPException(status_code=502, detail={"code": "apollo_unavailable"}) from e
    except (httpx.TransportError, httpx.TimeoutException) as e:
        raise HTTPException(status_code=502, detail={"code": "apollo_unavailable"}) from e
