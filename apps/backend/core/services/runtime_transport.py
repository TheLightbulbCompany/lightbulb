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
import concurrent.futures
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

GATEWAY_PORT = 18789

#: What ``resolve_running_container`` reports as the address of an AgentCore workspace. Truthy
#: on purpose: every "no ip → wake ECS / 503" door stays shut. Never dialled — the transport
#: addresses the runtime by ARN and ignores it.
AGENTCORE_ADDRESS = "agentcore"

#: AgentCore requires a session id of at least 33 characters.
_MIN_SESSION_ID_LEN = 33

#: A cold session is held by AgentCore for ~35s of microVM start before the contract server
#: answers at all. boto3's default 60s read timeout would cut a long RPC off under that.
_COLD_START_ALLOWANCE_S = 60.0

#: Invokes run on their own pool. A caller's ``wait_for`` can give up on a cold start while
#: the invoke keeps running to its read timeout; that thread must never sit in the default
#: executor that DynamoDB and every other ``to_thread`` user share.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="agentcore-invoke")


@dataclass(frozen=True)
class RuntimeResponse:
    """A gateway reply, shaped the same whichever transport carried it."""

    status_code: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)

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
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 30.0,
) -> RuntimeResponse:
    """Send one request to the owner's gateway.

    ``ip`` is required for the ECS transport and ignored by AgentCore, whose addressing comes
    from the container row's ``agentcore_runtime_arn``. ``content`` is a raw body for proxies
    that forward bytes verbatim (webhooks); it wins over ``json_body``.

    Failures are httpx-shaped on both transports — ``httpx.ReadTimeout`` when the gateway did
    not answer in time, ``httpx.ConnectError`` for anything else in the way — so a caller's
    existing ``except httpx.…`` covers a microVM exactly as it covers a task IP.
    """
    if host_for(container) == "agentcore":
        return await _request_agentcore(
            container,
            path,
            method=method,
            json_body=json_body,
            content=content,
            headers=headers,
            timeout_s=timeout_s,
        )
    return await _request_ecs(
        ip, path, method=method, json_body=json_body, content=content, headers=headers, timeout_s=timeout_s
    )


async def _request_ecs(
    ip: str | None,
    path: str,
    *,
    method: str,
    json_body: Any,
    content: bytes | None,
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
            content=content,
            json=json_body if content is None else None,
            headers=headers or {},
        )
    return RuntimeResponse(status_code=response.status_code, text=response.text, headers=dict(response.headers))


async def _request_agentcore(
    container: dict,
    path: str,
    *,
    method: str,
    json_body: Any,
    content: bytes | None,
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
    if content is not None:
        # The envelope is JSON, so a raw body rides as text. Webhook bodies are text; a binary
        # one would be lossy here — no caller sends one.
        envelope["body"] = content.decode("utf-8", errors="replace")
    elif json_body is not None:
        envelope["body"] = json_body

    def _invoke() -> RuntimeResponse:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError

        client = boto3.client(
            "bedrock-agentcore",
            config=Config(
                connect_timeout=10,
                read_timeout=timeout_s + _COLD_START_ALLOWANCE_S,
                # Never re-send. An ingress POST is a turn; retry is the caller's decision.
                retries={"total_max_attempts": 1},
            ),
        )
        try:
            result = client.invoke_agent_runtime(
                agentRuntimeArn=arn,
                runtimeSessionId=session_id_for(owner_id),
                contentType="application/json",
                accept="application/json",
                payload=json.dumps(envelope).encode(),
            )
            raw = result.get("response")
            text = raw.read().decode() if hasattr(raw, "read") else (raw if isinstance(raw, str) else "")
        except ReadTimeoutError as exc:
            raise httpx.ReadTimeout(str(exc)) from exc
        except (BotoCoreError, ClientError) as exc:
            raise httpx.ConnectError(str(exc)) from exc
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
            inner_headers = inner.get("headers")
            return RuntimeResponse(
                status_code=int(inner["status"]),
                text=body_text,
                headers={str(k): str(v) for k, v in inner_headers.items()} if isinstance(inner_headers, dict) else {},
            )
        return RuntimeResponse(status_code=502, text=text)

    # boto3 is synchronous; keep it off the event loop, and off the shared default pool.
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(_EXECUTOR, _invoke), timeout=timeout_s + 10.0)
