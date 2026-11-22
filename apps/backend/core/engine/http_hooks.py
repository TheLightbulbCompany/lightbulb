"""The engine's three calls into the products over HTTP (`core.engine.hooks`): each POSTs to the product's callback
routes at `settings.PRODUCT_CALLBACK_URL` (`routers/engine_callbacks.py`). The engine service always registers these;
the Temporal worker and the work Lambda do when `settings.ENGINE_TRANSPORT == "http"`.

A failure raises, as in process: inside an activity it fails the activity (retried; product handlers are idempotent
on the ids they get), never an empty answer. A `context` or `event` call is retried once on a gateway error (both are
safe to replay); a `tool` call only when nothing was sent, since a tool (a Meet chat line) may act twice."""

from __future__ import annotations

from core.config import settings
from core.engine import hooks, transport
from core.engine.hooks import ContextAnswer, ContextAsk, EngineEvent, ToolCall, ToolName

_PREFIX = "/api/v1/internal/engine"


async def _send(path: str, body, *, retry_status: bool = True):
    return await transport.send(
        settings.PRODUCT_CALLBACK_URL,
        "POST",
        _PREFIX + path,
        json=transport.dump(body),
        timeout=transport.HOOK_TIMEOUT_S,
        retry_status=retry_status,
    )


async def tool(name: ToolName, call: ToolCall) -> dict:
    return await _send(f"/tools/{name}", call, retry_status=False)


async def context(ask: ContextAsk) -> ContextAnswer:
    return ContextAnswer.model_validate(await _send("/context", ask))


async def event(e: EngineEvent) -> None:
    await _send("/events", e)


def register() -> None:
    hooks.register(tool=tool, context=context, event=event)
