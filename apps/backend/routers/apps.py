"""Apps — /api/v1/apps/*.

Builders (a workspace agent's hook token, or a signed-in computer's proof) build under ``/build``;
members read the list from Canvas, and an app's creator or an org admin changes, shares, publishes or
removes it (R73). Thin over ``core/services/apps_service.py``; this file only authenticates, gates and maps errors.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from typing import Annotated, Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field, StringConstraints

from core.config import settings
from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.repositories.apps_repo import NameTaken
from core.services import app_conversation
from core.services import apps_service as svc
from core.services import network_login, network_proof
from core.services.apps_db import AppsDbError
from core.services.apps_source import MAX_BYTES, SourceNotFound, SourceRefused, SourceTooLarge
from core.services.container_auth import _require_hook_token
from core.services.provisioning_workflow import WorkflowRefused
from core.services.stripe_provisioning import ProvisioningError
from core.services.vercel_client import VercelError
from routers.company_provisioning import PAID_MESSAGE, _refusal, _stripe_failure
from routers.teams._guards import require_always_on_entitlement

router = APIRouter()

_INVITE_PATH = "/api/v1/apps/invite/"


class InviteCors:
    """R78/R83: the invite swap answers CORS (incl. preflight) for https://<label>.{APPS_DOMAIN} only, with no
    credentials. Pure ASGI and outermost, so the global credentialed CORSMiddleware never sees these origins."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(_INVITE_PATH):
            return await self.app(scope, receive, send)
        origin = dict(scope["headers"]).get(b"origin", b"").decode("latin-1")
        if not re.fullmatch(rf"https://[a-z0-9]([a-z0-9-]*[a-z0-9])?\.{re.escape(settings.APPS_DOMAIN)}", origin):
            return await self.app(scope, receive, send)
        cors = [(b"access-control-allow-origin", origin.encode()), (b"vary", b"Origin")]
        if scope["method"] == "OPTIONS":
            cors += [(b"access-control-allow-methods", b"GET, OPTIONS"), (b"access-control-max-age", b"600")]
            await send({"type": "http.response.start", "status": 200, "headers": [*cors, (b"content-length", b"0")]})
            return await send({"type": "http.response.body", "body": b""})

        async def with_cors(message):
            if message["type"] == "http.response.start":
                # the inner credentialed CORSMiddleware stamps allow-credentials on every response: drop it
                kept = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"access-control-allow-credentials"
                ]
                message = {**message, "headers": [*kept, *cors]}
            await send(message)

        await self.app(scope, receive, with_cors)


MAX_UPLOAD = MAX_BYTES + 512 * 1024  # the archive plus the form's other fields, under API Gateway's 10 MB
_AGENT_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
_RETRY = "try again; if it keeps failing, report it"
_STATUS = {
    "app_not_found": 404,
    "app_destroyed": 404,
    "invite_invalid": 404,
    "not_a_member": 403,
    "not_allowed": 403,
    "room_not_found": 404,
    "source_not_found": 404,
    "version_not_found": 404,
    "approval_invalid": 403,
    "invalid_name": 422,
    "source_refused": 422,
}


async def _run(call):
    """Every service failure as a ``{code, message, next?}`` detail. Messages come from the
    exceptions' own safe fields; nothing here formats a credential."""
    try:
        return await call
    except svc.AppsRefused as e:
        detail = {"code": e.code, "message": e.message, **({"next": e.next} if e.next else {})}
        raise HTTPException(status_code=_STATUS.get(e.code, 409), detail=detail) from None
    except WorkflowRefused as e:
        raise _refusal(e) from None
    except ProvisioningError as e:
        raise _stripe_failure(e) from None
    except VercelError as e:
        raise HTTPException(502, {"code": f"vercel_{e.code}", "message": str(e), "next": _RETRY}) from None
    except AppsDbError as e:
        raise HTTPException(502, {"code": e.code, "message": str(e), "next": _RETRY}) from None
    except NameTaken:
        raise HTTPException(
            409, {"code": "name_taken", "message": "Another workspace uses that name.", "next": "pick another name"}
        ) from None
    except SourceTooLarge:
        raise _too_large() from None
    except SourceRefused as e:
        raise HTTPException(422, {"code": "source_refused", "message": str(e)}) from None
    except SourceNotFound:
        raise HTTPException(404, {"code": "source_not_found", "message": "That source was not uploaded."}) from None


def _too_large() -> HTTPException:
    return HTTPException(413, {"code": "source_too_large", "message": f"The archive is over {MAX_BYTES} bytes."})


# --- builders -----------------------------------------------------------------------------------


async def _caller(request: Request, *, upload: bool = False) -> svc.BuilderCaller:
    """A hook token (``Authorization`` + ``X-Lightbulb-Owner``) or a computer proof; nothing else.
    ``upload``: the body is read (bounded) only after the headers authenticate the caller."""
    owner = request.headers.get("X-Lightbulb-Owner")
    if owner and request.headers.get("Authorization", "").startswith("Bearer "):
        await _require_hook_token(owner, request)
        agent = request.headers.get("X-Lightbulb-Agent-Id") or "agent"  # R14: optional
        if not _AGENT_ID_RE.fullmatch(agent):
            raise HTTPException(422, {"code": "invalid_agent_id", "message": "X-Lightbulb-Agent-Id is malformed."})
        if upload:
            await _read_capped(request)
        caller = svc.BuilderCaller(owner_id=owner, actor_id=agent, actor_kind="agent")
    elif request.headers.get("X-Lightbulb-Proof"):
        if upload:
            # Signature + issued proof first (headers only); the full check below hashes the body.
            network_login.signed_proof(request)
            await _read_capped(request)
        auth, _ = await network_login.computer_identity(request)
        if not auth.org_id:
            raise HTTPException(
                409,
                {
                    "code": "workspace_required",
                    "message": "Apps belong to a workspace, and this computer is signed in to none.",
                    "next": "join or create a workspace, then lightbulb login again",
                },
            )
        caller = svc.BuilderCaller(owner_id=auth.org_id, actor_id=auth.user_id, actor_kind="person")
    else:
        raise HTTPException(status_code=401, detail="missing credentials")
    await require_always_on_entitlement(caller.owner_id, message=PAID_MESSAGE)  # R22
    run_id = request.headers.get("X-Lightbulb-Run")
    if run_id and _AGENT_ID_RE.fullmatch(run_id):  # absent or malformed: the app links no run (old CLIs)
        caller = dataclasses.replace(caller, run_id=run_id)
    return caller


async def builder_caller(request: Request) -> svc.BuilderCaller:
    return await _caller(request)


async def _upload_caller(request: Request) -> svc.BuilderCaller:
    return await _caller(request, upload=True)


Builder = Annotated[svc.BuilderCaller, Depends(builder_caller)]


async def _builder_app_id(app_id: str, caller: Builder) -> str:
    """``{app_id}`` on a builder route: the id or the app's name, so ``app ask booking`` works from any folder."""
    return await _run(svc.resolve_app_id(caller.owner_id, app_id))


AppId = Annotated[str, Depends(_builder_app_id)]


async def _read_capped(request: Request) -> None:
    """Refuse a declared oversize upload unread, and count a chunked one as it arrives. The body is
    kept on the request so the proof hash and ``form()`` replay it."""
    declared = request.headers.get("content-length")
    if declared is not None and not (declared.isascii() and declared.isdigit()):
        raise HTTPException(400, {"code": "invalid_request", "message": "Content-Length is malformed."})
    if int(declared or 0) > MAX_UPLOAD:
        raise _too_large()
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_UPLOAD:
            raise _too_large()
        chunks.append(chunk)
    request._body = b"".join(chunks)


class Origin(BaseModel):
    kind: str = Field(max_length=64)
    ref: str | None = Field(default=None, max_length=512)


class UpBody(BaseModel):
    name: str = Field(max_length=63)
    room_id: str | None = Field(default=None, max_length=128)  # optional: where the request came from
    services: list[Annotated[str, Field(max_length=64)]] = Field(max_length=20)
    open_to: Literal["me", "invited", "workspace", "public", "room"]
    budget_usd: int = Field(ge=0)
    source_id: str = Field(max_length=128)
    origin: Origin
    issue_id: str | None = Field(default=None, max_length=128)
    approved_destructive: str | None = Field(default=None, max_length=128)
    cost_cents: int | None = Field(default=None, ge=0)
    requested_by: str | None = Field(default=None, max_length=128)  # R73: first up only


class PublishBody(BaseModel):
    source_id: str = Field(max_length=128)
    reason: str = Field(default="", max_length=2000)
    approved_destructive: str | None = Field(default=None, max_length=128)
    cost_cents: int | None = Field(default=None, ge=0)


class RollbackBody(BaseModel):
    version: int | None = Field(default=None, ge=1)


class StartBody(BaseModel):
    name: str = Field(max_length=63)
    request: str = Field(min_length=1, max_length=4000)
    origin_room_id: str | None = Field(default=None, max_length=128)
    room_id: str | None = Field(default=None, max_length=128)  # what the CLI's `app start --room` sends
    requested_by: str | None = Field(default=None, max_length=128)


class AskOrigin(BaseModel):
    room_id: str | None = Field(default=None, max_length=128)
    requested_by: str | None = Field(default=None, max_length=128)


class AskBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    origin: AskOrigin | None = None
    client_message_id: str | None = Field(default=None, max_length=128)  # the source message: a resend is a no-op


@router.post("/build/start")
async def start(body: StartBody, caller: Builder):
    """A room agent's first move on an app request: the app gets its own conversation, the room is free."""
    return await _run(
        app_conversation.start(
            caller,
            body.name,
            body.request,
            origin_room_id=body.origin_room_id or body.room_id,
            requested_by=body.requested_by,
        )
    )


@router.post("/build/{app_id}/ask")
async def ask(app_id: AppId, body: AskBody, caller: Builder):
    origin = body.origin.model_dump(exclude_none=True) if body.origin else None
    return await _run(
        app_conversation.ask(caller, app_id, body.text, origin=origin, client_message_id=body.client_message_id)
    )


_COMMIT = re.compile(r"[A-Za-z0-9]{1,200}")


@router.post("/build/source")
async def put_source(request: Request, caller: Annotated[svc.BuilderCaller, Depends(_upload_caller)]):
    # Parsed by hand, after builder_caller: the proof check hashes the raw body first, and FastAPI's
    # own form parsing would consume the stream before it (Starlette replays the cached body here).
    async with request.form(max_part_size=4096) as form:
        archive = form.get("archive")
        fields = {k: form.get(k) for k in ("name", "commit")}
        if not hasattr(archive, "read") or not all(isinstance(v, str) and v for v in fields.values()):
            raise HTTPException(422, {"code": "invalid_request", "message": "name, commit and archive"})
        room_id = form.get("room_id")  # optional: an app belongs to its workspace, not a room
        if room_id is not None and not (isinstance(room_id, str) and len(room_id) <= 128):
            raise HTTPException(422, {"code": "invalid_request", "message": "room_id"})
        fields["room_id"] = room_id or None
        if not _COMMIT.fullmatch(fields["commit"]):  # it goes back out in a response header on pull
            raise HTTPException(422, {"code": "invalid_request", "message": "commit"})
        base_commit = form.get("base_commit")  # what the upload was built on; old CLIs omit it
        if base_commit is not None and not (isinstance(base_commit, str) and _COMMIT.fullmatch(base_commit)):
            raise HTTPException(422, {"code": "invalid_request", "message": "base_commit"})
        placeholder = form.get("placeholder") == "true"  # the starter's untouched page; old CLIs omit it
        data = await archive.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise _too_large()
    return await _run(
        svc.put_source(
            caller.owner_id,
            **fields,
            archive=data,
            actor=caller,
            placeholder=placeholder,
            base_commit=base_commit or None,
        )
    )


@router.get("/build/{app_id}/source")
async def get_source(app_id: AppId, caller: Builder):
    """The newest uploaded source, for ``app pull``: the archive, its commit and the real app id in headers."""
    data, commit = await _run(svc.latest_source(caller.owner_id, app_id, caller))
    headers = {"X-Lightbulb-Commit": commit, "X-Lightbulb-App": app_id, "Cache-Control": "no-store"}
    return Response(data, media_type="application/gzip", headers=headers)


@router.post("/build/up")
async def up(body: UpBody, caller: Builder):
    fields = {**body.model_dump(), "origin": body.origin.model_dump(exclude_none=True)}
    return await _run(svc.up(caller.owner_id, **fields, actor=caller))


@router.post("/build/{app_id}/publish")
async def publish(app_id: AppId, body: PublishBody, caller: Builder):
    return await _run(svc.publish(caller.owner_id, app_id, **body.model_dump(), actor=caller))


@router.post("/build/{app_id}/rollback")
async def rollback(app_id: AppId, body: RollbackBody, caller: Builder):
    return await _run(svc.rollback(caller.owner_id, app_id, version=body.version, run_id=caller.run_id))


@router.post("/build/{app_id}/destroy")
async def destroy(app_id: AppId, caller: Builder):
    return await _run(svc.destroy(caller.owner_id, app_id, actor=caller))


@router.get("/build/{app_id}/status")
async def status(app_id: AppId, caller: Builder):
    return await _run(svc.status(caller.owner_id, app_id))


@router.get("/build/{app_id}/errors")
async def errors(app_id: AppId, caller: Builder):
    return await _run(svc.errors(caller.owner_id, app_id))


@router.post("/build/{app_id}/test-link")
async def test_link(app_id: AppId, caller: Builder, response: Response):
    """The builder's own short-lived way past the preview's door. Never live."""
    response.headers["Cache-Control"] = "no-store"  # a pass: never cached
    return {"url": await _run(svc.test_link(caller.owner_id, app_id, actor=caller))}


@router.post("/build/{app_id}/reset-preview-data")
async def reset_preview_data(app_id: AppId, caller: Builder):
    """The preview database back to its schema and sample data, after ``app verify`` sent test values. Never live."""
    return await _run(svc.reset_preview_data(caller.owner_id, app_id))


# --- members ------------------------------------------------------------------------------------


@router.get("/issuer")
async def issuer():
    return {"keys": [network_proof.public_key()]}


@router.get("/invite/{token}")
async def swap_invite(token: str, response: Response):
    """Public: an invite link's one-time token for a 15-minute pass to its app (R41: ``token``)."""
    response.headers["Cache-Control"] = "no-store"  # a pass: never cached, per origin or otherwise
    try:
        return {"token": await _run(svc.swap_invite(token))}
    except (ClientError, BotoCoreError):  # storage down: a retryable 503, never an unhandled 500
        raise HTTPException(
            503,
            {"code": "unavailable", "message": "Invites are unavailable.", "next": _RETRY},
            headers={"Cache-Control": "no-store"},
        ) from None


@router.get("")
@router.get("/", include_in_schema=False)
async def list_apps(auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = resolve_owner_id(auth)
    return {"apps": await _run(svc.list_apps(owner_id, auth))}


class SettingsBody(BaseModel):
    open_to: Literal["me", "invited", "workspace", "public", "room"] | None = None
    budget_usd: int | None = Field(default=None, ge=0, strict=True)
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)] | None = None


class InvitesBody(BaseModel):
    emails: list[EmailStr] = Field(min_length=1, max_length=20)


class RemoveBody(BaseModel):
    confirm_name: str = Field(max_length=63)


class RevokeInviteBody(BaseModel):
    email: EmailStr


class AskAgentBody(BaseModel):
    error: str | None = Field(default=None, max_length=2000)  # a refusal Canvas saw that the record never kept


class MemberAskBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    client_message_id: uuid.UUID | None = None


class MemberPublishBody(BaseModel):
    pass  # R69: {} — Canvas publishes what the ready preview shows


async def _manager_gate(app_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]) -> str:
    """R73: the rail's owner gate minus its admin rule (paid workspace, owner = the org), then this
    app's creator or an org admin."""
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id, message=PAID_MESSAGE)
    await _run(svc.require_manager(owner_id, app_id, auth))
    return owner_id


Manager = Annotated[str, Depends(_manager_gate)]


@router.patch("/{app_id}")
async def update_settings(app_id: str, body: SettingsBody, owner_id: Manager):
    return await _run(
        svc.update_settings(
            owner_id, app_id, open_to=body.open_to, budget_usd=body.budget_usd, display_name=body.display_name
        )
    )


@router.post("/{app_id}/publish")
async def member_publish(
    app_id: str,
    body: MemberPublishBody,
    owner_id: Manager,
    auth: Annotated[AuthContext, Depends(get_current_user)],
):
    return await _run(svc.member_publish(owner_id, app_id, user_id=auth.user_id))


@router.post("/{app_id}/rollback")
async def member_rollback(app_id: str, body: RollbackBody, owner_id: Manager):
    return await _run(svc.rollback(owner_id, app_id, version=body.version))


@router.post("/{app_id}/ask-agent")
async def ask_agent(
    app_id: str, body: AskAgentBody, owner_id: Manager, auth: Annotated[AuthContext, Depends(get_current_user)]
):
    return await _run(svc.ask_agent_to_fix(owner_id, app_id, auth=auth, error=body.error))


class MemberStartBody(BaseModel):
    request: str = Field(min_length=1, max_length=4000)


@router.post("/start")
async def member_start(body: MemberStartBody, auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Canvas's prompt box: a person asks for an app with no room. It gets its own conversation and a builder."""
    await require_always_on_entitlement(resolve_owner_id(auth), message=PAID_MESSAGE)
    return await _run(app_conversation.start_as_member(auth, body.request))


@router.post("/{app_id}/ask")
async def member_ask(
    app_id: str, body: MemberAskBody, owner_id: Manager, auth: Annotated[AuthContext, Depends(get_current_user)]
):
    return await _run(app_conversation.ask_as_member(auth, app_id, body.text, body.client_message_id))


@router.post("/{app_id}/conversation")
async def open_conversation(app_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Canvas opens the app's conversation: created on the first open, the caller seated (anyone who can open the app)."""
    return await _run(app_conversation.open_conversation(auth, app_id))


@router.post("/{app_id}/open")
async def open_app(
    app_id: str,
    auth: Annotated[AuthContext, Depends(get_current_user)],
    target: Literal["live", "preview"] = "live",
):
    owner_id = resolve_owner_id(auth)
    return {"url": await _run(svc.open_app(owner_id, app_id, auth=auth, target=target))}


@router.post("/{app_id}/invites")
async def invite(
    app_id: str, body: InvitesBody, owner_id: Manager, auth: Annotated[AuthContext, Depends(get_current_user)]
):
    emails = list(dict.fromkeys(str(e).lower() for e in body.emails))
    return await _run(svc.invite(owner_id, app_id, emails=emails, user_id=auth.user_id))


@router.get("/{app_id}/invites")
async def list_invites(app_id: str, owner_id: Manager):
    return {"emails": await _run(svc.list_invites(owner_id, app_id))}


@router.delete("/{app_id}/invites")
async def revoke_invite(app_id: str, body: RevokeInviteBody, owner_id: Manager):
    return await _run(svc.revoke_invite(owner_id, app_id, email=str(body.email).lower()))


@router.delete("/{app_id}")
async def remove(
    app_id: str, body: RemoveBody, owner_id: Manager, auth: Annotated[AuthContext, Depends(get_current_user)]
):
    return await _run(svc.remove(owner_id, app_id, confirm_name=body.confirm_name, user_id=auth.user_id))
