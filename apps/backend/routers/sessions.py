"""/api/v1/sessions -- coding-agent sessions a person uploads from the CLI, by choice.

The caller is a signed-in computer (``X-Lightbulb-Proof``: the person its login names, in the
workspace chosen at login) or the web's Clerk person. A caller in a workspace uploads to, lists and
deletes within the workspace; one without a workspace has only their own. See core.services.session_store.
Session rows and summaries are never logged.
"""

import base64
import hashlib
import hmac
import logging
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, StringConstraints

from core.config import settings
from core.auth import AuthContext, get_current_user, security_optional
from core.services import evaluator, network_login, session_store

logger = logging.getLogger(__name__)
router = APIRouter()
shared_router = APIRouter()

SHARE_TTL = 7 * 86400

# The CLI's ids are "<harness>:<id>" (claude:3f2a...), kept as given: ":" is safe in an S3 key and a
# DynamoDB sort key, and "/" (the only character that could reshape the key) is still refused.
Id = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]  # it becomes an S3 key
Text = Annotated[str, StringConstraints(max_length=2000)]
Summary = Annotated[str, StringConstraints(max_length=2000)]


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
    mode: Literal["metadata", "full"]
    sessions: list[Session] = Field(default_factory=list, max_length=200)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=5000)


async def caller(request: Request, credentials=Depends(security_optional)) -> AuthContext:
    if request.headers.get("X-Lightbulb-Proof"):
        return (await network_login.computer_identity(request))[0]
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await get_current_user(credentials)


@router.post("")
async def upload(body: Upload, auth: AuthContext = Depends(caller)) -> dict:
    full = body.mode == "full"
    rows = [
        {**s.model_dump(), "tool_calls": [c.model_dump() for c in body.tool_calls if c.session_id == s.id]}
        for s in body.sessions
    ]
    stored = await session_store.put_sessions(auth.user_id, rows, full, auth.org_id)
    # The evaluator reads only what a person chose to upload in full, and only in a workspace: a free
    # account's sessions are stored, never evaluated (founder ruling, 4 Nov 2026). Its failure never
    # fails the upload.
    if full and auth.org_id:
        for r in rows:
            try:
                await evaluator.ingest_session(
                    auth.user_id,
                    {k: v for k, v in r.items() if k != "tool_calls"},
                    r["tool_calls"],
                    workspace_id=auth.org_id,
                )
            except Exception as e:
                logger.warning("session ingest failed: %s", type(e).__name__)
    return {"stored": stored}


@router.get("")
async def list_mine(since: str | None = None, auth: AuthContext = Depends(caller)) -> dict:
    rows = await session_store.list_sessions(user_id=auth.user_id, workspace_id=auth.org_id, since=since)
    return {"sessions": rows}


@router.delete("/{session_id}", status_code=204)
async def delete(session_id: str, auth: AuthContext = Depends(caller)) -> Response:
    """The session's rows and object, then what the evaluator holds of it: its ingest marker, its
    events and its place in drafts' evidence (a draft citing only it goes). Reflections already drawn
    from it are not attributable to one session and stay until the person's wipe. The evaluator's
    part never fails the delete."""
    if not await session_store.delete_session(session_id, user_id=auth.user_id, workspace_id=auth.org_id):
        raise HTTPException(status_code=404, detail="session not found")
    try:
        await evaluator.forget_session(auth.user_id, session_id, workspace_id=auth.org_id)
    except Exception as e:
        logger.warning("session forget failed: %s", type(e).__name__)
    return Response(status_code=204)


def _sign(body: str) -> str:
    # Domain-separated from the Paperclip JWTs this key also signs.
    return hmac.new(
        settings.PAPERCLIP_SERVICE_TOKEN_KEY.encode(), f"session-share|{body}".encode(), hashlib.sha256
    ).hexdigest()


@router.post("/{session_id}/share")
async def share(session_id: str, auth: AuthContext = Depends(caller)) -> dict:
    """A link anyone can open for 7 days. The token names the owner, session and expiry; only the
    server can sign it, so none can be changed or extended."""
    item = await session_store.get_session(session_id, user_id=auth.user_id, workspace_id=auth.org_id)
    # Only the uploader may publish their session: a teammate reads it, nothing more.
    if item is None or item.get("user_id") != auth.user_id or not settings.PAPERCLIP_SERVICE_TOKEN_KEY:
        raise HTTPException(status_code=404, detail="session not found")
    payload = f"{item['user_id']}|{session_id}|{int(time.time()) + SHARE_TTL}"
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return {"url": f"{settings.FRONTEND_URL.rstrip('/')}/shared/{body}.{_sign(body)}"}


@shared_router.get("/{token}")
async def shared(token: str) -> dict:
    body, _, sig = token.partition(".")
    key = settings.PAPERCLIP_SERVICE_TOKEN_KEY
    if not (key and body and sig and hmac.compare_digest(sig.encode(), _sign(body).encode())):
        raise HTTPException(status_code=404, detail="not found")
    try:
        owner, sid, exp = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode().split("|")
        live = int(exp) > time.time()
    except ValueError:
        live = False
    found = await session_store.read_shared(owner, sid) if live else None
    if found is None:
        raise HTTPException(status_code=404, detail="not found")
    return found
