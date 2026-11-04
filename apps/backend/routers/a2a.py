"""The A2A door (JSON-RPC binding, A2A v1.0): Agent Card, issuer keys, SendMessage and GetTask, records,
a holder's decision, the workspace rules, and the hook-token ask from a hosted agent."""

from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from google.protobuf.json_format import MessageToDict
from pydantic import Field

from core.auth import AuthContext, get_current_user, require_org_admin
from core.config import settings
from core.repositories import a2a_repo
from core.services import a2a_tasks, network_proof
from core.services.a2a_auth import READ_SCOPE_A2A, SEND_SCOPE, A2ACaller, a2a_caller, oauth_endpoints, require_scope
from core.services.container_auth import _require_hook_token
from core.services.a2a_rules import Rules, default_rules
from core.services.a2a_tasks import A2AError
from core.services.messaging import ContractModel
from routers._public_ingress import _check_rate_limit, _extract_caller_ip

router = APIRouter(tags=["a2a"])


@router.get("/a2a/keys.json")
async def keys():
    """A JWKS (RFC 7517, OKP per RFC 8037) of the issuer key."""
    k = network_proof.public_key()
    return {"keys": [{"kty": "OKP", "crv": "Ed25519", "x": k["key"], "alg": "EdDSA", "use": "sig", "kid": k["kid"]}]}


@router.get("/a2a/{org}/{agent_id}/agent-card.json")
async def agent_card(org: str, agent_id: str, request: Request):
    if not _check_rate_limit(f"a2a-card:{_extract_caller_ip(request)}"):
        raise HTTPException(429, detail={"code": "rate_limited"})
    name = await a2a_tasks.agent_name(org, agent_id)
    if name is None:
        raise HTTPException(404, detail={"code": "agent_not_found"})
    base = settings.PUBLIC_API_BASE.rstrip("/")
    return MessageToDict(a2a_tasks.signed_agent_card(org, agent_id, name, base, await oauth_endpoints()))


def _same_org(ctx: AuthContext, org: str) -> None:
    if ctx.org_id != org:
        raise HTTPException(403, detail={"code": "wrong_workspace"})


@router.get("/a2a/{org}/{agent_id}/tasks/{task_id}/record")
async def task_record(org: str, agent_id: str, task_id: str, caller: A2ACaller = Depends(a2a_caller)):
    _same_org(caller.auth, org)
    require_scope(caller, READ_SCOPE_A2A)
    try:
        return await a2a_tasks.records(org, task_id, reader=caller.auth)
    except A2AError as exc:
        raise HTTPException(404, detail={"code": "task_not_found", "message": exc.message}) from None


class Decision(ContractModel):
    verdict: Literal["allow", "deny"]
    reason: str = ""


@router.post("/a2a/{org}/{agent_id}/tasks/{task_id}/decide")
async def decide(org: str, agent_id: str, task_id: str, body: Decision, ctx: AuthContext = Depends(get_current_user)):
    _same_org(ctx, org)
    try:
        return await a2a_tasks.decide(ctx, org, agent_id, task_id, verdict=body.verdict, reason=body.reason)
    except A2AError as exc:
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.code)


@router.get("/a2a/{org}/rules")
async def get_rules(org: str, ctx: AuthContext = Depends(get_current_user)):
    _same_org(ctx, org)
    require_org_admin(ctx)
    return await a2a_repo.get_rules(org) or default_rules(org, [ctx.user_id]).model_dump()


@router.put("/a2a/{org}/rules")
async def put_rules(org: str, body: Rules, ctx: AuthContext = Depends(get_current_user)):
    _same_org(ctx, org)
    require_org_admin(ctx)
    return await a2a_tasks.put_rules(ctx, org, body)


@router.post("/a2a/{org}/{agent_id}")
async def jsonrpc(org: str, agent_id: str, body: dict = Body(...), caller: A2ACaller = Depends(a2a_caller)):
    _same_org(caller.auth, org)
    rpc_id, method, params = body.get("id"), body.get("method"), body.get("params")
    # A missing scope is an HTTP 403: the call never reaches the method.
    require_scope(caller, SEND_SCOPE if method == "SendMessage" else READ_SCOPE_A2A)
    try:
        if not isinstance(params, dict):
            raise A2AError(-32602, "params must be an object.")
        if method == "SendMessage":
            # A2A v1.0 SendMessageResponse: the Task under "task" (the a2a-sdk client parses it so).
            result = {"task": await a2a_tasks.send_message(caller, org, agent_id, params.get("message"))}
        elif method == "GetTask":
            if not isinstance(params.get("id"), str):
                raise A2AError(-32602, "params.id must be a string.")
            result = await a2a_tasks.get_task(org, params["id"], reader=caller.auth)
        elif method == "CancelTask":
            raise A2AError(-32004, "CancelTask is not supported.")
        else:
            raise A2AError(-32601, "Method not found.")
    except A2AError as exc:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": exc.code, "message": exc.message}}
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


class AskSessionRequest(ContractModel):
    """A hosted agent's ask. The backend verifies the origin message's author; the runtime, which holds
    the hook token, asserts which message triggered the turn."""

    session_id: str
    text: str
    intent: str
    origin_message_id: str | None = None
    room_id: str | None = None  # the turn's room, where the origin message lives
    agent_id: str
    run_id: str | None = None


@router.post("/api/v1/internal/a2a/{owner_id}/ask-session")
async def ask_session(owner_id: str, body: AskSessionRequest, _: dict = Depends(_require_hook_token)):
    try:
        return await a2a_tasks.ask_session(owner_id, body)
    except A2AError as exc:
        raise HTTPException(exc.code, detail={"code": exc.message}) from None


class SessionReplyRequest(ContractModel):
    """A laptop session's reply to a hosted send, forwarded by the cloud member's bridge."""

    request_id: str
    text: str
    session: str  # the cloud member's a2a session the reply arrived on
    sender: str = Field(alias="from")  # the replying laptop session's address


@router.post("/api/v1/internal/a2a/{owner_id}/session-reply")
async def session_reply(owner_id: str, body: SessionReplyRequest, _: dict = Depends(_require_hook_token)):
    try:
        await a2a_tasks.on_session_reply(owner_id, body.request_id, body.text, body.session, body.sender)
    except A2AError as exc:
        raise HTTPException(exc.code, detail={"code": exc.message}) from None
    return {"ok": True}
