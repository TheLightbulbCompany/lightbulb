"""The engine's operations over HTTP (`settings.ENGINE_TRANSPORT == "http"`): the same functions, signatures, values
and refusals as `core.engine.api`, each one call to the engine service at `settings.ENGINE_URL`
(`routers/engine_api.py`). The wire, its bearer and its one retry are `core.engine.transport`'s."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any, TypeVar

from fastapi import HTTPException

from core.config import settings
from core.engine import transport
from core.engine.contract import (
    ActIn,
    AnnotateIn,
    AnswerIn,
    ConversationOut,
    EngineError,
    EventIn,
    EventOut,
    LineIn,
    LineOut,
    OpenIn,
    StartIn,
    StartOut,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")
_V1 = "/engine/v1"


async def http(call: Awaitable[T]) -> T:
    """A route's adapter: an operation's refusal as the HTTPException the route raised before (same status, body)."""
    try:
        return await call
    except EngineError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


async def _get(path: str, **params: Any) -> Any:
    return await transport.send(settings.ENGINE_URL, "GET", _V1 + path, params=params)


async def _post(path: str, body: Any = None, **params: Any) -> Any:
    return await transport.send(settings.ENGINE_URL, "POST", _V1 + path, params=params, json=transport.dump(body))


def _conversation(data: dict | None) -> ConversationOut | None:
    return ConversationOut.model_validate(data) if data is not None else None


# --- conversations ------------------------------------------------------------------------------------------------


async def get_conversation(
    tenant_id: str,
    conversation_id: str,
    *,
    with_members: bool = False,
    member: str | None = None,
    include_deleted: bool = False,
) -> ConversationOut | None:
    return _conversation(
        await _get(
            f"/conversations/{conversation_id}",
            tenantId=tenant_id,
            withMembers=with_members,
            member=member,
            includeDeleted=include_deleted,
        )
    )


async def get_front_agent(tenant_id: str, conversation_id: str | None = None) -> str | None:
    return await _get(f"/tenants/{tenant_id}/front-agent", conversationId=conversation_id)


async def find_conversation(conversation_id: str, *, include_deleted: bool = False) -> ConversationOut | None:
    return _conversation(await _get(f"/lookup/conversations/{conversation_id}", includeDeleted=include_deleted))


async def list_conversations(tenant_id: str, kinds: list[str], limit: int) -> list[ConversationOut]:
    rows = await _get("/conversations", tenantId=tenant_id, kind=kinds, limit=limit)
    return [ConversationOut.model_validate(r) for r in rows]


async def open_conversation(req: OpenIn) -> ConversationOut:
    return ConversationOut.model_validate(await _post("/conversations", req))


async def seat_roster(tenant_id: str, conversation_id: str) -> None:
    await _post(f"/conversations/{conversation_id}/roster", tenantId=tenant_id)


async def get_roster(tenant_id: str) -> list[dict]:
    return await _get(f"/tenants/{tenant_id}/roster")


async def annotate(req: AnnotateIn) -> bool:
    return await _post(f"/conversations/{req.conversation_id}/annotations", req)


# --- lines, tasks, events -----------------------------------------------------------------------------------------


async def append_line(req: LineIn) -> LineOut:
    return LineOut.model_validate(await _post(f"/conversations/{req.conversation_id}/lines", req))


async def start_task(req: StartIn) -> StartOut:
    return StartOut.model_validate(await _post("/tasks", req))


async def submit_event(req: EventIn) -> EventOut:
    return EventOut.model_validate(await _post("/events", req))


async def read_lines(tenant_id: str, conversation_id: str, after: int | None) -> dict:
    return await _get(f"/conversations/{conversation_id}/lines", tenantId=tenant_id, after=after)


async def read_history(tenant_id: str, conversation_id: str, limit: int) -> list[dict]:
    return await _get(f"/conversations/{conversation_id}/history", tenantId=tenant_id, limit=limit)


async def get_task(tenant_id: str, task_id: str, *, tree: bool = False) -> dict | None:
    return await _get(f"/tasks/{task_id}", tenantId=tenant_id, tree=tree)


async def get_trigger(tenant_id: str, trigger_id: str) -> dict | None:
    return await _get(f"/triggers/{trigger_id}", tenantId=tenant_id)


async def list_conversation_tasks(tenant_id: str, conversation_id: str) -> list[dict]:
    return await _get(f"/conversations/{conversation_id}/tasks", tenantId=tenant_id)


async def get_lines(tenant_id: str, conversation_id: str, message_ids: list[str | None]) -> list[dict | None]:
    return await _post(f"/conversations/{conversation_id}/lines/lookup", message_ids, tenantId=tenant_id)


async def answer(req: AnswerIn) -> dict:
    return await _post(f"/tasks/{req.task_id}/questions/{req.qid}/answer", req)


async def act(req: ActIn) -> dict:
    return await _post(f"/tasks/{req.task_id}/actions/{req.action}", req)


# --- warm ----------------------------------------------------------------------------------------------------------

_background: set[asyncio.Task] = set()


def warm(tenant_id: str, agent_ids: list[str], conversation_id: str | None = None) -> None:
    """Fire-and-forget, as in process: the engine service answers at once and wakes the runtime itself."""

    async def send():
        try:
            await _post(f"/tenants/{tenant_id}/warm", {"agentIds": agent_ids, "conversationId": conversation_id})
        except Exception:  # noqa: BLE001 -- best effort; the first turn wakes it otherwise
            logger.info("engine warm not sent tenant=%s", tenant_id, exc_info=True)

    task = asyncio.create_task(send())
    _background.add(task)
    task.add_done_callback(_background.discard)
