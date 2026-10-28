"""/api/v1/memory -- a person reviews what the workspace's agents remember (memory-scopes phase 2-4).

Lists the caller's OWN private scope (their 1:1 DMs with agents) and the team scope, and deletes
one fact. Owner and user ids come from auth only. Any member may delete a team fact (decision D2:
the same rule as editing an agent's USER.md). See core.services.workspace_memory.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.services import container_auth, evaluator, memory_api, memory_sync, workspace_memory
from routers.sessions import caller

logger = logging.getLogger(__name__)

router = APIRouter()
# The Hermes plugin's channel, mounted at /api/v1/internal/memory and guarded by the owner's hook token.
internal_router = APIRouter()

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
    owner_id = resolve_owner_id(auth)
    # The entries mirror and the log entry are found through the facts record's link row.
    entry = await memory_sync.entry_for(owner_id, record_id) if _RECORD_ID.fullmatch(record_id) else None
    if entry:  # first, so a failed record delete leaves a retry that still finds the link
        await _guard(memory_api.forget(owner_id, entry))
    await _act(workspace_memory.delete_memory, auth, record_id)
    if entry:
        try:
            await memory_sync.drop_mirror(owner_id, entry)
        except Exception as exc:  # noqa: BLE001 -- the forget's tombstone removes the mirror on the next sync
            logger.warning("memory mirror cleanup failed owner=%s error=%s", owner_id, type(exc).__name__)
    return Response(status_code=204)


async def _vouch(fn: Callable[..., Awaitable], auth: AuthContext, record_id: str) -> dict:
    try:
        return {"id": await _act(fn, auth, record_id)}
    except ValueError:
        raise HTTPException(status_code=422, detail="This memory is too long to share or confirm.") from None


@router.post("/{record_id}/share")
async def share_memory(record_id: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    """Phase 3: one of the caller's private facts becomes a confirmed team fact."""
    return await _vouch(workspace_memory.share_memory, auth, record_id)


@router.post("/{record_id}/confirm")
async def confirm_memory(record_id: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    """Phase 4: a person vouches for a team fact (any member: D2)."""
    return await _vouch(workspace_memory.confirm_memory, auth, record_id)


async def _guard(call: Awaitable):
    try:
        return await call
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except RuntimeError:
        raise HTTPException(status_code=502, detail="memory service failed; try again") from None


class RecallBody(BaseModel):
    question: str
    as_of: datetime | None = None
    budget_tokens: int = Field(default=8000, ge=1, le=50_000)
    limit: int = Field(default=100, ge=1, le=200)


class RememberBody(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    fact: str = Field(min_length=1, max_length=16_000)
    kind: Literal["fact", "decision", "lesson", "preference"]
    scope: Literal["me", "team"] = "me"
    replaces: list[str] | None = Field(default=None, max_length=50)


class InternalRememberBody(RememberBody):
    on_behalf_of: str | None = Field(default=None, max_length=200)


@router.post("/recall")
async def recall(body: RecallBody, auth: AuthContext = Depends(get_current_user)) -> dict:
    """The evidence page for a question, with the caller's own private scope included."""
    owner_id = resolve_owner_id(auth)
    return await _guard(memory_api.recall_page(owner_id, auth.user_id, **body.model_dump()))


@router.post("/remember")
async def remember(body: RememberBody, auth: AuthContext = Depends(get_current_user)) -> dict:
    owner_id = resolve_owner_id(auth)
    return await _guard(memory_api.remember(owner_id, **body.model_dump(), on_behalf_of=auth.user_id))


class ApproveBody(BaseModel):
    scope: Literal["me", "team"] | None = None  # default: team in a workspace, me for a solo person


async def _draft(draft_id: str, auth: AuthContext) -> dict:
    """The caller's still-proposed draft; anyone else's (or a settled one) is 404, never 403."""
    d = await evaluator.find_draft(draft_id, user_id=auth.user_id, workspace_id=auth.org_id)
    if not d or d.get("status") != "proposed":
        raise HTTPException(status_code=404, detail="draft not found")
    return d


@router.get("/drafts")
async def list_drafts(auth: AuthContext = Depends(caller)) -> dict:
    return {"drafts": await evaluator.list_drafts(user_id=auth.user_id, workspace_id=auth.org_id)}


@router.post("/drafts/{draft_id}/approve")
async def approve_draft(draft_id: str, body: ApproveBody | None = None, auth: AuthContext = Depends(caller)) -> dict:
    d = await _draft(draft_id, auth)
    if d["kind"] != "lesson":  # a finding may hold a secret: it can only be dismissed
        raise HTTPException(status_code=422, detail="Only lessons can be approved.")
    scope = (body and body.scope) or ("team" if auth.org_id else "me")
    # The evaluator keeps no repo or title on a draft, so the subject is its kind.
    subject = d["kind"]
    if auth.org_id:
        out = await _guard(
            memory_api.remember(
                auth.org_id, subject=subject, fact=d["text"], kind="lesson", scope=scope, on_behalf_of=auth.user_id
            )
        )
        await evaluator.set_draft_status(d, "approved")  # only once remember returned
        return {"queued": out["queued"]}
    await evaluator.set_draft_status(d, "approved")
    evidence = d.get("evidence") or []
    source = {"harness": "workspace", "on_behalf_of": None, "repo": None, "session": evidence[0] if evidence else None}
    return {"subject": subject, "fact": d["text"], "kind": "lesson", "scope": scope, "source": source}


@router.post("/drafts/{draft_id}/dismiss", status_code=204)
async def dismiss_draft(draft_id: str, auth: AuthContext = Depends(caller)) -> Response:
    await evaluator.set_draft_status(await _draft(draft_id, auth), "dismissed")
    return Response(status_code=204)


@internal_router.post("/recall/{owner_id}")
async def internal_recall(owner_id: str, body: RecallBody, request: Request) -> dict:
    """Recall for the workspace's agents: team scope only, never a person's private one."""
    await container_auth._require_hook_token(owner_id, request)
    return await _guard(memory_api.recall_page(owner_id, None, **body.model_dump()))


@internal_router.post("/remember/{owner_id}")
async def internal_remember(owner_id: str, body: InternalRememberBody, request: Request) -> dict:
    await container_auth._require_hook_token(owner_id, request)
    return await _guard(memory_api.remember(owner_id, **body.model_dump()))
