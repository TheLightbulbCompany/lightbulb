"""The engine's operations over HTTP, under `/engine/v1` on the engine service (`engine_main.py`): one route per
operation of `core.engine.api`, called by `core.engine.client`. Bodies are the contract models' camelCase JSON;
every route checks the service bearer (`core.engine.transport.require_token`), and the tenant and principal are the
authenticated caller's word, as in process. A refusal goes back marked so the client raises it again."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Body, Depends, Query

from core.engine import api, transport
from core.engine.contract import (
    ActIn,
    AnnotateIn,
    AnswerIn,
    EngineError,
    EventIn,
    LineIn,
    Model,
    OpenIn,
    StartIn,
)

router = APIRouter(dependencies=[Depends(transport.require_token)])
Tenant = Query(alias="tenantId")


async def _checked(pairs: list[tuple[str, str]], call):
    """`call`, when every id in the path is the body's."""
    if any(path_id != body_id for path_id, body_id in pairs):
        call.close()
        raise EngineError("path_mismatch", 400, {"code": "path_mismatch"})
    return await call


# --- conversations ------------------------------------------------------------------------------------------------


@router.post("/conversations")
async def open_conversation(req: OpenIn):
    return await transport.reply(api.open_conversation(req))


@router.get("/conversations")
async def list_conversations(tenant_id: str = Tenant, kind: list[str] = Query(), limit: int = Query()):
    return await transport.reply(api.list_conversations(tenant_id, kind, limit))


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    tenant_id: str = Tenant,
    with_members: bool = Query(False, alias="withMembers"),
    member: str | None = Query(None),
    include_deleted: bool = Query(False, alias="includeDeleted"),
):
    return await transport.reply(
        api.get_conversation(
            tenant_id, conversation_id, with_members=with_members, member=member, include_deleted=include_deleted
        )
    )


@router.get("/lookup/conversations/{conversation_id}")
async def find_conversation(conversation_id: str, include_deleted: bool = Query(False, alias="includeDeleted")):
    return await transport.reply(api.find_conversation(conversation_id, include_deleted=include_deleted))


@router.post("/conversations/{conversation_id}/annotations")
async def annotate(conversation_id: str, req: AnnotateIn):
    return await transport.reply(_checked([(conversation_id, req.conversation_id)], api.annotate(req)))


@router.post("/conversations/{conversation_id}/roster")
async def seat_roster(conversation_id: str, tenant_id: str = Tenant):
    return await transport.reply(api.seat_roster(tenant_id, conversation_id))


@router.get("/tenants/{tenant_id}/roster")
async def get_roster(tenant_id: str):
    return await transport.reply(api.get_roster(tenant_id))


@router.get("/tenants/{tenant_id}/front-agent")
async def get_front_agent(tenant_id: str, conversation_id: str | None = Query(None, alias="conversationId")):
    return await transport.reply(api.get_front_agent(tenant_id, conversation_id))


# --- lines ---------------------------------------------------------------------------------------------------------


@router.post("/conversations/{conversation_id}/lines")
async def append_line(conversation_id: str, req: LineIn):
    return await transport.reply(_checked([(conversation_id, req.conversation_id)], api.append_line(req)))


@router.get("/conversations/{conversation_id}/lines")
async def read_lines(conversation_id: str, tenant_id: str = Tenant, after: int | None = Query(None)):
    return await transport.reply(api.read_lines(tenant_id, conversation_id, after))


@router.post("/conversations/{conversation_id}/lines/lookup")
async def get_lines(conversation_id: str, message_ids: list[str | None] = Body(), tenant_id: str = Tenant):
    return await transport.reply(api.get_lines(tenant_id, conversation_id, message_ids))


@router.get("/conversations/{conversation_id}/history")
async def read_history(conversation_id: str, tenant_id: str = Tenant, limit: int = Query()):
    return await transport.reply(api.read_history(tenant_id, conversation_id, limit))


@router.get("/conversations/{conversation_id}/tasks")
async def list_conversation_tasks(conversation_id: str, tenant_id: str = Tenant):
    return await transport.reply(api.list_conversation_tasks(tenant_id, conversation_id))


# --- tasks, triggers, events ---------------------------------------------------------------------------------------


@router.post("/tasks")
async def start_task(req: StartIn):
    return await transport.reply(api.start_task(req))


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, tenant_id: str = Tenant, tree: bool = Query(False)):
    return await transport.reply(api.get_task(tenant_id, task_id, tree=tree))


@router.post("/tasks/{task_id}/questions/{qid}/answer")
async def answer(task_id: str, qid: str, req: AnswerIn):
    return await transport.reply(_checked([(task_id, req.task_id), (qid, req.qid)], api.answer(req)))


@router.post("/tasks/{task_id}/actions/{action}")
async def act(task_id: str, action: Literal["stop", "resolve", "reopen", "retry"], req: ActIn):
    return await transport.reply(_checked([(task_id, req.task_id), (action, req.action)], api.act(req)))


@router.get("/triggers/{trigger_id}")
async def get_trigger(trigger_id: str, tenant_id: str = Tenant):
    return await transport.reply(api.get_trigger(tenant_id, trigger_id))


@router.post("/events")
async def submit_event(req: EventIn):
    return await transport.reply(api.submit_event(req))


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(tenant_id: str):
    return await transport.reply(api.delete_tenant(tenant_id))


# --- warm ----------------------------------------------------------------------------------------------------------


class WarmIn(Model):
    agent_ids: list[str]
    conversation_id: str | None = None


@router.post("/tenants/{tenant_id}/warm", status_code=202)
async def warm(tenant_id: str, req: WarmIn):
    async def fire() -> None:
        api.warm(tenant_id, req.agent_ids, req.conversation_id)

    return await transport.reply(fire(), 202)
