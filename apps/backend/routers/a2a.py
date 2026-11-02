"""The A2A door (JSON-RPC binding, A2A v1.0): Agent Card, issuer keys, SendMessage and GetTask, records."""

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from google.protobuf.json_format import MessageToDict

from core.auth import AuthContext, get_current_user
from core.config import settings
from core.services import a2a_tasks, network_proof
from core.services.a2a_tasks import A2AError
from routers._public_ingress import _check_rate_limit, _extract_caller_ip

router = APIRouter(tags=["a2a"])


@router.get("/a2a/keys.json")
async def keys():
    return {"keys": [network_proof.public_key()]}


@router.get("/a2a/{org}/{agent_id}/agent-card.json")
async def agent_card(org: str, agent_id: str, request: Request):
    if not _check_rate_limit(f"a2a-card:{_extract_caller_ip(request)}"):
        raise HTTPException(429, detail={"code": "rate_limited"})
    name = await a2a_tasks.agent_name(org, agent_id)
    if name is None:
        raise HTTPException(404, detail={"code": "agent_not_found"})
    return MessageToDict(a2a_tasks.agent_card(org, agent_id, name, settings.PUBLIC_API_BASE.rstrip("/")))


def _same_org(ctx: AuthContext, org: str) -> None:
    if ctx.org_id != org:
        raise HTTPException(403, detail={"code": "wrong_workspace"})


@router.get("/a2a/{org}/{agent_id}/tasks/{task_id}/record")
async def task_record(org: str, agent_id: str, task_id: str, ctx: AuthContext = Depends(get_current_user)):
    _same_org(ctx, org)
    try:
        return await a2a_tasks.records(org, task_id, reader=ctx)
    except A2AError as exc:
        raise HTTPException(404, detail={"code": "task_not_found", "message": exc.message}) from None


@router.post("/a2a/{org}/{agent_id}")
async def jsonrpc(org: str, agent_id: str, body: dict = Body(...), ctx: AuthContext = Depends(get_current_user)):
    _same_org(ctx, org)
    rpc_id, method, params = body.get("id"), body.get("method"), body.get("params")
    try:
        if not isinstance(params, dict):
            raise A2AError(-32602, "params must be an object.")
        if method == "SendMessage":
            # A2A v1.0 SendMessageResponse: the Task under "task" (the a2a-sdk client parses it so).
            result = {"task": await a2a_tasks.send_message(ctx, org, agent_id, params.get("message"))}
        elif method == "GetTask":
            if not isinstance(params.get("id"), str):
                raise A2AError(-32602, "params.id must be a string.")
            result = await a2a_tasks.get_task(org, params["id"], reader=ctx)
        elif method == "CancelTask":
            raise A2AError(-32004, "CancelTask is not supported.")
        else:
            raise A2AError(-32601, "Method not found.")
    except A2AError as exc:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": exc.code, "message": exc.message}}
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
