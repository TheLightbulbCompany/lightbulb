"""One request per gateway RPC, carried to the owner's AgentCore runtime.

``call`` raises ``asyncio.TimeoutError`` on a 504, ``RuntimeError(json.dumps(error))`` when the
gateway reports ``{"ok": false}``, and ``ApiProblem("RUNTIME_UNAVAILABLE", ...)`` when the owner
has no addressable runtime row or the transport itself fails.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from core.observability.metrics import put_metric
from core.services import runtime_transport
from core.repositories import container_repo
from core.services.messaging import ApiProblem

RPC_PATH = "/plugins/lightbulb-channel/rpc"


def _unavailable() -> ApiProblem:
    return ApiProblem("RUNTIME_UNAVAILABLE", "The runtime is temporarily unavailable.", retryable=True, retry_after=3)


async def running_container(owner_id: str) -> dict:
    """The owner's addressable runtime row; raises retryable when there is none.

    There is no wake step and no address to resolve: AgentCore starts the microVM on the
    invoke itself, so a read-only caller cannot accidentally start anything and a parked
    runtime answers the invoke that wakes it.

    Status is deliberately NOT gated: a row is addressable as soon as it carries its gateway
    token. ``runtime_transport.request`` is the one place that insists on
    ``agentcore_runtime_arn`` -- a row without one fails there as the same ``ApiProblem``.
    """
    container = await container_repo.get_by_owner_id(owner_id)
    token = (container or {}).get("gateway_token")
    if container and isinstance(token, str) and token:
        return container
    raise _unavailable()


async def call(
    owner_id: str,
    method: str,
    params: dict | None = None,
    *,
    timeout_s: float = 30.0,
    target: dict | None = None,
) -> Any:
    """``target``, when given, is an already-resolved runtime row (e.g. from a prior
    ``running_container`` call) -- skips resolution so a caller making many calls for one owner
    pays one DynamoDB read, not one per call."""
    started = time.perf_counter()
    outcome = "unavailable"
    try:
        container = target if target is not None else await running_container(owner_id)
        # F4 (predeploy review, M6-R37): a parked workspace holds an invoke through the microVM
        # start + gateway boot (54-57s measured) -- a caller's shorter default (30s in
        # runtime_rpc, 10s/5s/2s in narrower probes) fails on the first call to a cold workspace
        # and only succeeds on a retry. Floor, never lower: a caller that already asked for a
        # longer budget keeps it.
        effective_timeout_s = max(timeout_s, runtime_transport.AGENTCORE_INGRESS_TIMEOUT_S)
        try:
            # runtime_transport owns HOW to reach the gateway (an InvokeAgentRuntime envelope).
            # Timeout margin lives there for the same reason it used to live here: the plugin's
            # own 504 must fire before the transport gives up.
            response = await runtime_transport.request(
                container,
                RPC_PATH,
                method="POST",
                json_body={"method": method, "params": params or {}, "timeoutMs": int(effective_timeout_s * 1000)},
                headers={"Authorization": f"Bearer {container['gateway_token']}"},
                timeout_s=effective_timeout_s,
            )
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as exc:
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
