"""One way to reach an owner's gateway. There is exactly one: AgentCore.

``InvokeAgentRuntime`` carries a ``{path, method, headers, body}`` envelope, which the
runtime's contract server unwraps onto 127.0.0.1:18789.

Every workspace is on AgentCore, so ``containers.runtime_host`` is not consulted at all and
there is nothing to classify: a runtime is addressed by the row's ``agentcore_runtime_arn``.

**Why the session id is derived from the owner id.** AgentCore creates a NEW microVM for every
distinct ``runtimeSessionId`` and keeps them warm in parallel. Two live sessions for one
workspace means two gateways on one profile prefix and two writers on one S3 prefix — which was
reproduced on 2026-09-16 and corrupts the replica set silently (divergent transaction
histories; every later restore hangs with no error). Deriving the id from the owner makes
at-most-one-session structural rather than a rule someone has to remember.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


logger = logging.getLogger(__name__)

#: F4 (predeploy review, M6-R37): the RPC budget for a call to a possibly-parked AgentCore
#: workspace -- AgentCore holds an invoke through the microVM start (~35s) and the gateway boot
#: (measured 54-57s end to end, 2026-09-16), so any shorter caller budget fails on the first
#: call to a cold workspace and only succeeds on a retry. Hoisted here (not left on
#: ``openclaw_work``, its original home) so ``container_rpc`` can reuse it without importing
#: ``openclaw_work`` -- that module itself imports ``runtime_rpc``, which imports
#: ``container_rpc``, so the reverse import would cycle. ``openclaw_work``/``agent_mail_delivery``
#: keep their own (private) references pointed at this one value.
AGENTCORE_INGRESS_TIMEOUT_S = 90.0

#: AgentCore requires a session id of at least 33 characters.
_MIN_SESSION_ID_LEN = 33

#: Optimistic-create contract (Phase 1 of the AgentCore-era simplification):
#: nothing waits for a runtime to reach READY — not the provisioner, not this
#: transport. Invoking a still-CREATING runtime fails fast, and this loop
#: retries with backoff. Total sleep budget ~60s (2+4+8+16+30); then the last
#: error raises exactly as it would have on the first attempt, and the
#: consumer's own redrive (room ingress) or the caller's error path takes it
#: from there. Only explicit not-ready signals retry — anything else fails
#: immediately, so a real misconfiguration never burns a minute here.
_NOT_READY_SLEEP_S = (2.0, 4.0, 8.0, 16.0, 30.0)

#: Substrings (case-insensitive) of an AgentCore ``ValidationException``
#: identifying "exists but is not usable yet". Unknown shapes fail fast AND
#: log code+message at warning, so a new not-ready shape observed in prod can
#: be added to this tuple deliberately rather than retried blindly today.
_NOT_READY_SNIPPETS = ("not ready", "not in valid state", "is still creating", "is creating", "warming")

#: Invokes run on their own pool: one blocks a thread for the caller's whole budget (90s on a
#: cold-start ingress), and that thread must never sit in the default executor that DynamoDB
#: and every other ``to_thread`` user share.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="agentcore-invoke")


@dataclass(frozen=True)
class RuntimeResponse:
    """A gateway reply."""

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


def _is_not_ready(exc: Exception) -> bool:
    """Whether this AgentCore error means "exists but not usable yet".

    Deliberately narrow: a ``ValidationException`` whose message names a
    transitional state. Everything else — auth, missing runtime, throttling,
    unknown shapes — fails immediately (unknown shapes also log at the call
    site so prod can teach this tuple new members).
    """
    from botocore.exceptions import ClientError

    if not isinstance(exc, ClientError):
        return False
    if exc.response.get("Error", {}).get("Code") != "ValidationException":
        return False
    message = str(exc).lower()
    return any(snippet in message for snippet in _NOT_READY_SNIPPETS)


async def request(
    container: dict,
    path: str,
    *,
    method: str = "POST",
    json_body: Any = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 30.0,
) -> RuntimeResponse:
    """Send one request to the owner's gateway.

    Addressing comes from the container row's ``agentcore_runtime_arn`` — there is no address
    to pass in. ``content`` is a raw body for proxies that forward bytes verbatim (webhooks);
    it wins over ``json_body``.

    Failures are httpx-shaped — ``httpx.ReadTimeout`` when the gateway did not answer in time,
    ``httpx.ConnectError`` for anything else in the way — so a caller's existing
    ``except httpx.…`` covers a microVM exactly as it used to cover a task IP.
    """
    return await _request_agentcore(
        container,
        path,
        method=method,
        json_body=json_body,
        content=content,
        headers=headers,
        timeout_s=timeout_s,
    )


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
    owner_id = container.get("owner_id") or ""
    envelope: dict[str, Any] = {"path": path, "method": method}
    if headers:
        envelope["headers"] = headers
    if content is not None:
        # The envelope is JSON, so a raw body rides as text. Webhook bodies are text; a binary
        # one would be lossy here — no caller sends one.
        envelope["body"] = content.decode("utf-8", errors="replace")
    elif json_body is not None:
        envelope["body"] = json_body

    # The caller's budget is the whole budget. A cold session is held by AgentCore through the
    # microVM start and gateway boot (54-57s measured), so the one caller that wakes a parked
    # workspace — room ingress — passes a budget that outlasts it; probes pass a short one and
    # simply report "not ready". boto's read timeout is the caller's budget plus a
    # small margin, and the outer wait sits just above it so the THREAD gives up first:
    # an invoke never outlives its caller, so a retried delivery cannot race an abandoned one.
    margin = min(5.0, timeout_s)
    read_timeout = timeout_s + margin

    def _invoke() -> RuntimeResponse:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError

        client = boto3.client(
            "bedrock-agentcore",
            config=Config(
                connect_timeout=10,
                read_timeout=read_timeout,
                # Never re-send. An ingress POST is a turn; retry is the caller's decision.
                retries={"total_max_attempts": 1},
            ),
        )
        not_ready: ClientError | None = None
        for attempt, delay in enumerate((0.0, *_NOT_READY_SLEEP_S)):
            if attempt:
                time.sleep(delay)
            try:
                result = client.invoke_agent_runtime(
                    agentRuntimeArn=arn,
                    runtimeSessionId=session_id_for(owner_id),
                    contentType="application/json",
                    accept="application/json",
                    # Same compact encoding as the ingress size cap measures, so a body that
                    # clears 1 MiB there is not inflated past it here.
                    payload=json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(),
                )
                raw = result.get("response")
                text = raw.read().decode() if hasattr(raw, "read") else (raw if isinstance(raw, str) else "")
                break
            except ReadTimeoutError as exc:
                raise httpx.ReadTimeout(str(exc)) from exc
            except ClientError as exc:
                if _is_not_ready(exc):
                    not_ready = exc
                    continue
                logger.warning(
                    "agentcore invoke for owner %s failed: %s %s",
                    owner_id,
                    exc.response.get("Error", {}).get("Code", "?"),
                    str(exc)[:160],
                )
                raise httpx.ConnectError(str(exc)) from exc
            except BotoCoreError as exc:
                raise httpx.ConnectError(str(exc)) from exc
        else:
            # Every attempt hit not-ready; raise the last one exactly as a
            # first-attempt failure would surface.
            assert not_ready is not None
            raise httpx.ConnectError(str(not_ready)) from not_ready
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
    try:
        return await asyncio.wait_for(loop.run_in_executor(_EXECUTOR, _invoke), timeout=read_timeout + 2.0)
    except asyncio.TimeoutError as exc:
        # httpx-shaped, so callers need one except clause.
        raise httpx.ReadTimeout(f"agentcore invoke exceeded {read_timeout:.0f}s") from exc
