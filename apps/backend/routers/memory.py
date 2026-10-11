"""/api/v1/memory -- a person reviews what the workspace's agents remember (memory-scopes phase 2-4).

Lists the caller's OWN private scope (their 1:1 DMs with agents) and the team scope, and deletes
one fact. Owner and user ids come from auth only. Any member may delete a team fact (decision D2:
the same rule as editing an agent's USER.md). See core.services.workspace_memory.
"""

import re
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Response

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.services import workspace_memory

router = APIRouter()

_RECORD_ID = re.compile(r"mem-[A-Za-z0-9_-]{36,46}")  # botocore: mem-[a-zA-Z0-9-_]*, 40-50 chars


async def _act(fn: Callable[..., Awaitable], auth: AuthContext, record_id: str):
    if not _RECORD_ID.fullmatch(record_id):
        raise HTTPException(status_code=404, detail="memory not found")
    try:
        return await fn(resolve_owner_id(auth), auth.user_id, record_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="memory not found") from None
    except RuntimeError:
        raise HTTPException(status_code=502, detail="memory service failed; try again") from None


@router.get("")
async def list_memory(auth: AuthContext = Depends(get_current_user)) -> dict:
    owner_id = resolve_owner_id(auth)
    try:
        return await workspace_memory.list_memories(owner_id, auth.user_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="memory not found") from None
    except RuntimeError:
        raise HTTPException(status_code=502, detail="memory service failed; try again") from None


@router.delete("/{record_id}", status_code=204)
async def delete_memory(record_id: str, auth: AuthContext = Depends(get_current_user)) -> Response:
    await _act(workspace_memory.delete_memory, auth, record_id)
    return Response(status_code=204)


@router.post("/{record_id}/share")
async def share_memory(record_id: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    """Phase 3: one of the caller's private facts becomes a confirmed team fact."""
    return {"id": await _act(workspace_memory.share_memory, auth, record_id)}


@router.post("/{record_id}/confirm")
async def confirm_memory(record_id: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    """Phase 4: a person vouches for a team fact (any member: D2)."""
    return {"id": await _act(workspace_memory.confirm_memory, auth, record_id)}
