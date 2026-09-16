"""One way to reach an owner's OpenClaw gateway, whichever platform hosts it.

Two transports, chosen per owner by ``containers.runtime_host``:

``ecs`` (default, today's fleet)
    Dial the task's Cloud Map IP on :18789 directly.

``agentcore``
    ``InvokeAgentRuntime`` with a ``{path, method, headers, body}`` envelope, which the
    container's contract server unwraps onto 127.0.0.1:18789. Proven end to end on
    2026-09-16: an authenticated ``/plugins/lightbulb-channel/rpc`` call returned 200 with a
    real payload from inside a microVM.

The choice is per OWNER, not global, because moving the fleet is a per-workspace rollout —
each workspace moves once, one way. It is NOT a fallback: nothing routes back to ECS after a
workspace has cut over (owner ruling, see the spec's §3).

**Why the session id is derived from the owner id.** AgentCore creates a NEW microVM for every
distinct ``runtimeSessionId`` and keeps them warm in parallel. Two live sessions for one
workspace means two gateways on one EFS access point and two litestream replicators on one S3
prefix — which was reproduced on 2026-09-16 and corrupts the replica set silently (divergent
transaction histories; every later restore hangs with no error). Deriving the id from the owner
makes at-most-one-session structural rather than a rule someone has to remember.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx

GATEWAY_PORT = 18789

#: AgentCore requires a session id of at least 33 characters.
_MIN_SESSION_ID_LEN = 33


@dataclass(frozen=True)
class RuntimeResponse:
    """A gateway reply, shaped the same whichever transport carried it."""

    status_code: int
    text: str

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError:
            return None


def session_id_for(owner_id: str) -> str:
    """The ONE AgentCore session id for this workspace.

    Stable per owner, so every backend call lands on the same microVM: that is both the
    latency win (session affinity — AWS routes a reused id back to the same microVM) and the
    single-writer guarantee. A random id per call would quietly fan a workspace across
    concurrent microVMs.
    """
    digest = hashlib.sha256(owner_id.encode()).hexdigest()
    candidate = f"lb-{owner_id}-{digest[:16]}"
    # Pad rather than truncate: the id must stay unique per owner AND clear the 33-char floor.
    return candidate if len(candidate) >= _MIN_SESSION_ID_LEN else candidate.ljust(_MIN_SESSION_ID_LEN, "0")


def host_for(container: dict) -> str:
    """Which transport this owner is on. Absent/unknown means ECS — the pre-migration default."""
    host = container.get("runtime_host")
    return host if host in ("ecs", "agentcore") else "ecs"


async def request(
    container: dict,
    path: str,
    *,
    ip: str | None = None,
    method: str = "POST",
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 30.0,
) -> RuntimeResponse:
    """Send one request to the owner's gateway.

    ``ip`` is required for the ECS transport and ignored by AgentCore, whose addressing comes
    from the container row's ``agentcore_runtime_arn``.
    """
    if host_for(container) == "agentcore":
        return await _request_agentcore(
            container, path, method=method, json_body=json_body, headers=headers, timeout_s=timeout_s
        )
    return await _request_ecs(ip, path, method=method, json_body=json_body, headers=headers, timeout_s=timeout_s)


async def _request_ecs(
    ip: str | None,
    path: str,
    *,
    method: str,
    json_body: Any,
    headers: dict[str, str] | None,
    timeout_s: float,
) -> RuntimeResponse:
    if not ip:
        raise ValueError("ecs transport requires a container ip")
    # margin bounds the HTTP ceiling just above the plugin's own timeout, so the plugin's 504
    # fires first on a slow call rather than httpx aborting the connection under it.
    margin = min(5.0, timeout_s)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s + margin, connect=margin)) as client:
        response = await client.request(
            method,
            f"http://{ip}:{GATEWAY_PORT}{path}",
            json=json_body,
            headers=headers or {},
        )
    return RuntimeResponse(status_code=response.status_code, text=response.text)


async def _request_agentcore(
    container: dict,
    path: str,
    *,
    method: str,
    json_body: Any,
    headers: dict[str, str] | None,
    timeout_s: float,
) -> RuntimeResponse:
    arn = container.get("agentcore_runtime_arn")
    if not arn:
        raise ValueError("agentcore transport requires containers.agentcore_runtime_arn")
    owner_id = container.get("owner_id") or container.get("user_id") or ""
    envelope: dict[str, Any] = {"path": path, "method": method}
    if headers:
        envelope["headers"] = headers
    if json_body is not None:
        envelope["body"] = json_body

    def _invoke() -> RuntimeResponse:
        import boto3

        client = boto3.client("bedrock-agentcore")
        result = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            runtimeSessionId=session_id_for(owner_id),
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(envelope).encode(),
        )
        raw = result.get("response")
        text = raw.read().decode() if hasattr(raw, "read") else (raw if isinstance(raw, str) else "")
        # The contract server answers 200 with the gateway's own status inside; a 503 here is
        # the contract server saying the gateway has not finished booting.
        outer_status = int(result.get("statusCode") or 200)
        if outer_status != 200:
            return RuntimeResponse(status_code=outer_status, text=text)
        try:
            inner = json.loads(text)
        except ValueError:
            return RuntimeResponse(status_code=502, text=text)
        if isinstance(inner, dict) and "status" in inner:
            body = inner.get("body")
            body_text = body if isinstance(body, str) else json.dumps(body) if body is not None else ""
            return RuntimeResponse(status_code=int(inner["status"]), text=body_text)
        return RuntimeResponse(status_code=502, text=text)

    # boto3 is synchronous; keep it off the event loop.
    return await asyncio.wait_for(asyncio.to_thread(_invoke), timeout=timeout_s + 10.0)
