"""The engine's three calls into the products over HTTP (`core.engine.hooks`): each POSTs to the product's callback
routes at `settings.PRODUCT_CALLBACK_URL` (`routers/engine_callbacks.py`). The engine service always registers these;
the Temporal worker and the work Lambda do when `settings.ENGINE_TRANSPORT == "http"`.

A failure raises, as in process: inside an activity it fails the activity (retried; product handlers are idempotent
on the ids they get), never an empty answer. A call goes again only when it was never sent (`transport.send`): a tool
(a Meet chat line) may act twice, and the activity's own retry covers the rest.

Budgets: `context` and `event` 8 s an attempt, 12 s in all (`transport.HOOK_*`), under every caller's own limit: a
Temporal activity's 30 s start-to-close, the work Lambda's 180 s, and the runtime's 20 s wait on a callback route. A
`tool` gets 15 s (`transport.TOOL_*`): it runs only on the runtime's tool routes, whose plugin waits 20 s."""

from __future__ import annotations

import httpx
from fastapi import HTTPException

from core.config import settings
from core.engine import hooks, transport
from core.engine.hooks import ContextAnswer, ContextAsk, EngineEvent, ToolCall, ToolName

_PREFIX = "/api/v1/internal/engine"


#: A tool call that timed out after it was sent: the product may still have done it.
TOOL_TIMEOUT = "timed_out: the action may still have completed; check before trying again"
#: A tool call that never reached the product: nothing was done.
TOOL_NOT_SENT = "not_sent: the action was not sent; nothing was done"


async def _send(
    path: str, body, attempt_s: float = transport.HOOK_ATTEMPT_S, budget_s: float = transport.HOOK_BUDGET_S
):
    return await transport.send(
        settings.PRODUCT_CALLBACK_URL,
        "POST",
        _PREFIX + path,
        json=transport.dump(body),
        attempt_s=attempt_s,
        budget_s=budget_s,
    )


async def tool(name: ToolName, call: ToolCall) -> dict:
    """No timeout reaches the agent's tool as a bare 500: a 503 when the call never reached the product (a connect or
    pool timeout, a refused connection), a 504 that says the action may have run otherwise (a read or write timeout,
    or the deadline, which can fire after the request went out)."""
    try:
        return await _send(f"/tools/{name}", call, transport.TOOL_ATTEMPT_S, transport.TOOL_BUDGET_S)
    except (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError) as exc:
        raise HTTPException(status_code=503, detail=TOOL_NOT_SENT) from exc
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise HTTPException(status_code=504, detail=TOOL_TIMEOUT) from exc


async def context(ask: ContextAsk) -> ContextAnswer:
    return ContextAnswer.model_validate(await _send("/context", ask))


async def event(e: EngineEvent) -> None:
    await _send("/events", e)


def register() -> None:
    hooks.register(tool=tool, context=context, event=event)
