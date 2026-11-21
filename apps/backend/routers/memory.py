"""/api/v1/memory -- a person's recall, and the Hermes plugin's internal channel (recall, board, link, remember). See core.services.workspace_memory."""

import logging
from collections.abc import Awaitable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, require_org, resolve_owner_id
from core.repositories import tasks_repo
from core.services import (
    container_auth,
    effect_doors,
    memory_api,
    memory_links,
    workspace_memory,
)

logger = logging.getLogger(__name__)

router = APIRouter()
# The Hermes plugin's channel, mounted at /api/v1/internal/memory and guarded by the owner's hook token.
internal_router = APIRouter()


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


class AudienceBody(BaseModel):
    """Who the page is for: link visibility is checked against it (spec 07 §5.3)."""

    kind: Literal["room", "person", "workspace"]
    roomId: str | None = Field(default=None, max_length=200)  # noqa: N815 -- the wire name
    userId: str | None = Field(default=None, max_length=200)  # noqa: N815


class InternalRecallBody(RecallBody):
    # The person in a 1:1 DM (the plugin's ``_member``): adds their private scope, as before the log.
    member: str | None = Field(default=None, max_length=200)
    # The run's ``memory`` block (backend-computed, spec 07 §5.2): never a model argument.
    lane: Literal["shared", "private"] | None = None
    audience: AudienceBody | None = None


class LinkBody(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    audience: AudienceBody


def _audience(member: str | None, lane: str | None, audience: AudienceBody | None) -> dict | None:
    """A person audience is only ever the member (L2); a private lane needs both."""
    shown = audience.model_dump(exclude_none=True) if audience else None
    if lane == "private" and not (member and shown and shown["kind"] == "person"):
        raise HTTPException(status_code=422, detail="a private lane needs member and a person audience")
    if shown and shown["kind"] == "person" and shown.get("userId") != member:
        raise HTTPException(status_code=422, detail="a person audience is the member")
    if shown and shown["kind"] == "room" and not shown.get("roomId"):
        raise HTTPException(status_code=422, detail="a room audience names its room")
    return shown


class InternalRememberBody(RememberBody):
    on_behalf_of: str | None = Field(default=None, max_length=200)
    # A Worker run's identity (the memory plugin sends it on a task run, as effect_check gets it).
    task_id: str | None = Field(default=None, max_length=128)
    attempt: int = Field(default=0, ge=0)
    run_id: str = Field(default="", max_length=128)
    run_secret: str = Field(default="", max_length=128)


_RUN_FIELDS = {"task_id", "attempt", "run_id", "run_secret"}


@router.post("/recall")
async def recall(body: RecallBody, auth: AuthContext = Depends(get_current_user)) -> dict:
    """The evidence page for a question, with the caller's own private scope included (the
    super-agent acceptance check a12 reads a private fact through it)."""
    require_org(auth)  # AgentCore memory retrieval
    owner_id = resolve_owner_id(auth)
    return await _guard(memory_api.recall_page(owner_id, auth.user_id, **body.model_dump()))


@internal_router.post("/recall/{owner_id}")
async def internal_recall(owner_id: str, body: InternalRecallBody, request: Request) -> dict:
    """Recall for the workspace's agents: the team scope, plus the private scope of ``member`` (sent
    only from a 1:1 DM with that person). No question expansion and no rerank: the plugin's recall runs
    before the Router's model call, so every model call here adds to every reply (rerank 60-410 ms on dev,
    6 Nov 2026)."""
    await container_auth._require_hook_token(owner_id, request)
    fields = body.model_dump(exclude={"member", "lane", "audience"})
    member = body.member
    if member is not None and not workspace_memory._MEMBER_ID.fullmatch(member):
        raise HTTPException(status_code=422, detail="member must be a user id")
    audience = _audience(member, body.lane, body.audience)
    return await _guard(
        memory_api.recall_page(owner_id, member, **fields, use_expand=False, use_rerank=False, audience=audience)
    )


class BoardBody(BaseModel):
    audience: AudienceBody


@internal_router.post("/board/{owner_id}")
async def internal_board(owner_id: str, body: BoardBody, request: Request) -> dict:
    """The task board a super-agent turn reads instead of Paperclip's, filtered for its audience."""
    await container_auth._require_hook_token(owner_id, request)
    audience = body.audience.model_dump(exclude_none=True)
    if audience["kind"] == "room" and not audience.get("roomId"):
        raise HTTPException(status_code=422, detail="a room audience names its room")
    return {"text": await memory_links.board_text(owner_id, audience)}


@internal_router.post("/link/{owner_id}")
async def internal_link(owner_id: str, body: LinkBody, request: Request) -> dict:
    """The Router's second hop (``follow_link``): one task as ``audience`` may see it; 404 otherwise."""
    await container_auth._require_hook_token(owner_id, request)
    audience = body.audience.model_dump(exclude_none=True)
    if audience["kind"] == "room" and not audience.get("roomId"):
        raise HTTPException(status_code=422, detail="a room audience names its room")
    out = await memory_links.read_link(owner_id, body.id, audience)
    if out is None:
        raise HTTPException(status_code=404, detail="not found")
    return out


@internal_router.post("/remember/{owner_id}")
async def internal_remember(owner_id: str, body: InternalRememberBody, request: Request) -> dict:
    """A phone caller's task never writes workspace memory: what a caller says would be recalled by every agent
    (gate B2). The plugin's remember on a task run names the run, checked like effect_check. The hook token is one
    per workspace, so a call naming no run may still be a Worker's shell: while any phone caller's attempt is live it
    is refused (fails closed, like effect_doors: only the rows are trusted)."""
    await container_auth._require_hook_token(owner_id, request)
    if body.task_id:
        from routers import runtime_internal  # lazy: the runtime router pulls in Temporal

        await runtime_internal._attempt_of(owner_id, body.task_id, body.attempt, body.run_id, body.run_secret)
        tasks = [await tasks_repo.get_task(owner_id, body.task_id)]
    else:
        tasks = [await tasks_repo.get_task(owner_id, a["taskId"]) for a in await effect_doors.live_attempts(owner_id)]
    for task in tasks:
        if task is None or await tasks_repo.from_contact(owner_id, task):
            raise HTTPException(status_code=403, detail="A phone caller's task cannot write workspace memory.")
    return await _guard(memory_api.remember(owner_id, **body.model_dump(exclude=_RUN_FIELDS)))
