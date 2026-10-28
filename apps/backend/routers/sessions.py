"""/api/v1/sessions -- coding-agent sessions a person uploads from the CLI, by choice.

The caller is a signed-in computer (``X-Lightbulb-Proof``: the person its login names, in the
workspace chosen at login) or the web's Clerk person. A caller in a workspace uploads to, lists and
deletes within the workspace; one without a workspace has only their own. See core.services.session_store.
Session rows and summaries are never logged.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, StringConstraints

from core.auth import AuthContext, get_current_user, security_optional
from core.services import network_login, session_store

router = APIRouter()

Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,128}$")]  # it becomes an S3 key
Text = Annotated[str, StringConstraints(max_length=2000)]
Summary = Annotated[str, StringConstraints(max_length=20_000)]


class Session(BaseModel):
    id: Id
    harness: Text
    computer: Text = ""
    repo: Text = ""
    branch: Text = ""
    started: Text
    ended: Text | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ToolCall(BaseModel):
    session_id: Id
    seq: int
    at: Text
    tool: Text
    summary: Summary | None = None
    duration_ms: int = 0
    ok: bool = True


class Upload(BaseModel):
    sessions: list[Session] = Field(default_factory=list, max_length=200)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=20_000)


async def caller(request: Request, credentials=Depends(security_optional)) -> AuthContext:
    if request.headers.get("X-Lightbulb-Proof"):
        return (await network_login.computer_person(request))[0]
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await get_current_user(credentials)


@router.post("")
async def upload(body: Upload, auth: AuthContext = Depends(caller)) -> dict:
    # Summaries only come with a full upload; a metadata upload carries none to keep.
    full = any(c.summary is not None for c in body.tool_calls)
    rows = [
        {**s.model_dump(), "tool_calls": [c.model_dump() for c in body.tool_calls if c.session_id == s.id]}
        for s in body.sessions
    ]
    return {"stored": await session_store.put_sessions(auth.user_id, rows, full, auth.org_id)}


@router.get("")
async def list_mine(since: str | None = None, auth: AuthContext = Depends(caller)) -> dict:
    rows = await session_store.list_sessions(user_id=auth.user_id, workspace_id=auth.org_id, since=since)
    return {"sessions": rows}


@router.delete("/{session_id}", status_code=204)
async def delete(session_id: str, auth: AuthContext = Depends(caller)) -> Response:
    if not await session_store.delete_session(session_id, user_id=auth.user_id, workspace_id=auth.org_id):
        raise HTTPException(status_code=404, detail="session not found")
    return Response(status_code=204)
