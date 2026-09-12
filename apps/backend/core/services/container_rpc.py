"""One HTTP request per gateway RPC, to the container's baked lightbulb-channel plugin.

Replaces the per-owner gateway WebSocket pool. ``call`` raises exactly what the pool's
``send_rpc`` raised so every caller's ``except`` chain is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from core.containers import get_ecs_manager
from core.observability.metrics import put_metric
from core.repositories import container_repo
from core.services.messaging import ApiProblem

GATEWAY_PORT = 18789
RPC_PATH = "/plugins/lightbulb-channel/rpc"


async def running_container(owner_id: str) -> tuple[dict, str]:
    """The owner's running container row and IP; wakes a stopped one and reports retryable."""
    manager = get_ecs_manager()
    container, ip = await manager.resolve_running_container(owner_id)
    if container and ip:
        token = container.get("gateway_token")
        if isinstance(token, str) and token:
            return container, ip
    row = container or await container_repo.get_by_owner_id(owner_id, consistent=True)
    if row and row.get("status") == "stopped":
        from core.services.container_wake import request_wake_silent

        await request_wake_silent(owner_id)
        raise ApiProblem(
            "RUNTIME_UNAVAILABLE", "The runtime is waking. Try again shortly.", retryable=True, retry_after=3
        )
    raise ApiProblem("RUNTIME_UNAVAILABLE", "The runtime is temporarily unavailable.", retryable=True, retry_after=3)


def _unavailable() -> ApiProblem:
    return ApiProblem("RUNTIME_UNAVAILABLE", "The runtime is temporarily unavailable.", retryable=True, retry_after=3)


async def call(owner_id: str, method: str, params: dict | None = None, *, timeout_s: float = 30.0) -> Any:
    container, ip = await running_container(owner_id)
    started = time.perf_counter()
    outcome = "unavailable"
    try:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s + 5.0, connect=5.0)) as client:
                response = await client.post(
                    f"http://{ip}:{GATEWAY_PORT}{RPC_PATH}",
                    json={"method": method, "params": params or {}, "timeoutMs": int(timeout_s * 1000)},
                    headers={"Authorization": f"Bearer {container['gateway_token']}"},
                )
        except httpx.HTTPError as exc:
            raise _unavailable() from exc
        if response.status_code == 504:
            outcome = "timeout"
            raise asyncio.TimeoutError(f"{method} timed out")
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200 or not isinstance(body, dict):
            raise _unavailable()
        if body.get("ok") is True:
            outcome = "ok"
            return body.get("payload")
        outcome = "error"
        raise RuntimeError(json.dumps(body.get("error") or {"code": "UNAVAILABLE", "message": "gateway method failed"}))
    finally:
        put_metric("messaging.rpc", dimensions={"outcome": outcome, "method": method})
        put_metric(
            "messaging.rpc.latency",
            value=(time.perf_counter() - started) * 1000,
            unit="Milliseconds",
            dimensions={"method": method},
        )
