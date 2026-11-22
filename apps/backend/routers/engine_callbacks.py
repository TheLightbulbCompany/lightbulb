"""Lightbulb Phone's and Meetings' side of `core.engine.hooks` over HTTP: the engine's processes call these
(`core.engine.http_hooks`) under `/api/v1/internal/engine` on the backend, with the service bearer. Each runs the
products' in-process hook (`core.voice.engine_hooks`); a refusal goes back marked, so the engine raises it again."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from core.engine import transport
from core.engine.hooks import ContextAsk, EngineEvent, ToolCall, ToolName
from core.voice import engine_hooks

router = APIRouter(dependencies=[Depends(transport.require_token)])


@router.post("/tools/{name}")
async def tool(name: ToolName, call: ToolCall):
    return await transport.reply(engine_hooks._tool(name, call))


@router.post("/context")
async def context(ask: ContextAsk):
    return await transport.reply(engine_hooks._context(ask))


@router.post("/events", status_code=204)
async def event(e: EngineEvent):
    return await transport.reply(engine_hooks._event(e), 204)
