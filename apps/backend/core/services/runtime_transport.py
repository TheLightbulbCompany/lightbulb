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
import re
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


class LegacyRuntimeSessionRefused(httpx.ConnectError):
    """A live ledger names a previous runtime that may not receive new invocations.

    This is unknown/busy to lifecycle guards, never evidence that the VM is absent or idle.
    HTTPError inheritance preserves the existing RPC retryable-error contract.
    """


class TaskSessionRefused(ValueError):
    """A call with ``task_id`` that is not a runs-v1 call for that task's own task run."""


#: The bridge's own runs-v1 routes (``bridge/runs.py`` create + ``_RUN_RE``), matched whole: no
#: prefix test a ``/runs/../`` path could slip through.
# ``/internal/v1/caller-runs`` is a caller's create (``runtime_runs.CALLER_RUNS_PATH``); poll/steer/stop stay on runs.
_RUNS_V1_RE = re.compile(r"^/internal/v1/(caller-runs|runs(/[0-9a-f-]{36}(/stop|/steer)?)?)$")


def _check_task_call(path: str, json_body: Any, task_id: str) -> None:
    """Only a runs-v1 call whose identity is THIS task's run with ``session.kind == "task"`` may
    reach a task session. That kind is what a bridge without task mode refuses, so a task session
    is never asked to run anything that would restore and write ``<owner>/hermes/`` -- the 16 Sep
    two-writers corruption (module docstring). Everything else stays on the workspace session."""
    identity = json_body.get("identity") if isinstance(json_body, dict) else None
    session = identity.get("session") if isinstance(identity, dict) else None
    if (
        _RUNS_V1_RE.fullmatch(path)
        and isinstance(session, dict)
        and session.get("kind") == "task"
        and identity.get("taskId") == task_id  # the wire identity is camelCase (RunIdentity.to_wire)
    ):
        return
    raise TaskSessionRefused(f"task session refused for {path}")


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


def runtime_arn(container: dict) -> str | None:
    """The configured canonical pool; obsolete owned pointers are never invocation routes.

    Configuration loss must fail closed rather than revive a previous writer. Saved owner
    routes and identity fields remain intact for cleanup and teardown.
    """
    from core.config import settings

    if not settings.HERMES_SHARED_RUNTIME_ARN:
        raise RuntimeError("HERMES_SHARED_RUNTIME_ARN is not configured for this environment")
    if (
        container.get("agentcore_runtime_arn")
        or container.get("agentcore_shared_runtime_arn")
        or container.get("status") == "running"
    ):
        return settings.HERMES_SHARED_RUNTIME_ARN
    return None


#: How long past ``warm_until`` :func:`live_arn` still counts a session live on its own runtime. It
#: must outlast AgentCore's idle timeout, or the pool starts a second microVM for the owner while the
#: old one is still up: ``warm_until`` can trail the last invoke's by WARM_EXTEND_STEP_S, so the old
#: VM may live WARM_MARGIN_S + WARM_EXTEND_STEP_S (160 s) past it. Plus 30 s, which also covers a
#: late room-turn heartbeat (every 30 s); a Worker's (every ~2 min) slides the estimate itself.
LIVE_GRACE_S = 190


def live_arn(container: dict, task_id: str | None = None) -> str | None:
    """The observed live writer for lifecycle/stop operations, otherwise the canonical pool.

    Session heartbeats keep the estimate current through LIVE_GRACE_S. Invocation separately
    refuses a live writer outside the pool: it must neither revive the legacy runtime nor
    start a second writer before that session drains. Failed ledger reads propagate.
    """
    arn = runtime_arn(container)
    owner_id = container.get("owner_id")
    if not arn or not owner_id:
        return arn
    from core.repositories import container_repo as repo

    # The session ledger records the actual writer, including a legacy workspace already
    # migrated to the pool when configuration is rolled back. Container fields describe the
    # desired destination, not which VM is still alive. A failed read must propagate: neither
    # destination is safe to start or stop until the current writer can be established.
    row = repo.get_session_sync(owner_id, task_id=task_id)
    if row.get("arn") and int(row.get("warm_until") or 0) > time.time() - LIVE_GRACE_S:
        return row["arn"]
    return arn


def note_alive(owner_id: str, task_id: str | None = None) -> None:
    """A run's heartbeat: its session is alive, so slide its warm estimate as an invoke would.
    Without this a Worker run that sends no invoke reads as idle after 200 s, and at the cut-over
    its polls would leave its own runtime for the pool. Sync; never raises."""
    from core.repositories import container_repo as repo

    try:
        row = repo.get_session_sync(owner_id, task_id=task_id)
        if row.get("arn"):
            _note_invoked(owner_id, row, task_id)
    except Exception:  # noqa: BLE001 -- the next heartbeat tries again
        logger.warning("session alive mark failed for owner %s", owner_id, exc_info=True)


def session_id_for(owner_id: str, task_id: str | None = None) -> str:
    """The ONE AgentCore session id for this workspace, or for one task of it.

    Stable per owner, so every backend call lands on the same microVM: that is both the
    latency win (session affinity — AWS routes a reused id back to the same microVM) and the
    single-writer guarantee. A random id per call would quietly fan a workspace across
    concurrent microVMs.

    With ``task_id`` (super-agent per-task VMs, dev only): one stable id per task. Its microVM
    is a second writer for the owner only in name -- the bridge's task mode restores the
    specialist profile read-only and writes nothing outside ``<owner>/hermes-tasks/<task>/``,
    so each S3 prefix still has exactly one writer. ``-t-`` keeps it apart from the
    workspace id (``t`` is not a hex digit).
    """
    if task_id is not None:
        candidate = f"lb-{owner_id}-t-{hashlib.sha256(task_id.encode()).hexdigest()[:20]}"
    else:
        candidate = f"lb-{owner_id}-{hashlib.sha256(owner_id.encode()).hexdigest()[:16]}"
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
    task_id: str | None = None,
) -> RuntimeResponse:
    """Send one request to the owner's gateway (``task_id``: that task's own session).

    Addressing comes from the container row's ``agentcore_runtime_arn`` — there is no address
    to pass in. ``content`` is a raw body for proxies that forward bytes verbatim (webhooks);
    it wins over ``json_body``.

    With ``task_id``, only a runs-v1 call for that task's task run is sent (:func:`_check_task_call`);
    anything else raises :class:`TaskSessionRefused` before any wake or invoke.

    Failures are httpx-shaped — ``httpx.ReadTimeout`` when the gateway did not answer in time,
    ``httpx.ConnectError`` for anything else in the way — so a caller's existing
    ``except httpx.…`` covers a microVM exactly as it used to cover a task IP.
    """
    if task_id is not None:
        _check_task_call(path, None if content is not None else json_body, task_id)
    return await _request_agentcore(
        container,
        path,
        method=method,
        json_body=json_body,
        content=content,
        headers=headers,
        timeout_s=timeout_s,
        task_id=task_id,
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
    task_id: str | None = None,
) -> RuntimeResponse:
    arn = runtime_arn(container)
    if not arn:
        raise ValueError("agentcore transport requires a runtime (containers.agentcore_runtime_arn)")
    owner_id = container.get("owner_id") or ""
    # Every invoke names its tenant: a shared runtime binds its session to the first one and
    # refuses any other (bridge.bind_tenant); a per-owner runtime checks it against its own.
    vault = {**_vault_header(container), **({TENANT_HEADER: owner_id} if owner_id else {})}
    envelope: dict[str, Any] = {"path": path, "method": method}
    if headers or vault:
        envelope["headers"] = {**(headers or {}), **vault}
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
    deadline = time.monotonic() + timeout_s

    def _wake() -> None:
        # The winner's cold start: one cheap ``health`` RPC (restores no profile) on the full
        # cold budget whatever the caller's own, so a short-budget winner cannot release the
        # lease while AgentCore is still binding a microVM. If the caller gives up first this
        # thread still finishes the wake -- a health call outliving its caller is harmless.
        _invoke_envelope(
            arn,
            owner_id,
            {
                "path": "/plugins/lightbulb-channel/rpc",
                "method": "POST",
                "headers": {
                    "Authorization": f"Bearer {container.get('gateway_token') or ''}",
                    **vault,
                    **_session_credentials_header(owner_id),
                    **_boot_header(container),
                },
                "body": {"method": "health", "params": {}, "timeoutMs": int(AGENTCORE_INGRESS_TIMEOUT_S * 1000)},
            },
            AGENTCORE_INGRESS_TIMEOUT_S + 5.0,
            task_id,
        )

    def _invoke() -> RuntimeResponse:
        nonlocal arn  # the wake below uses the same one
        arn = live_arn(container, task_id)  # a DynamoDB read at the cut-over: here on the pool
        if arn != runtime_arn(container):
            # Lifecycle/teardown can still identify and stop the previous writer, but no new
            # invoke may reach it. Do not start another writer while that session may be live.
            raise LegacyRuntimeSessionRefused("legacy runtime session is still draining")
        # Keyword only for a task session: the workspace call keeps its exact pre-task shape.
        session = _ensure_session(
            owner_id, arn, deadline, _wake, **({"task_id": task_id} if task_id is not None else {})
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Waited out the whole budget on someone else's wake: the caller's retry path.
            raise httpx.ReadTimeout("agentcore session is still waking")
        # STS and SecretsManager block: here on the pool, not the loop.
        scoped = {**_session_credentials_header(owner_id), **_boot_header(container)}
        sent = {**envelope, "headers": {**envelope.get("headers", {}), **scoped}} if scoped else envelope
        result, text = _invoke_envelope(arn, owner_id, sent, remaining + margin, task_id)
        _note_invoked(owner_id, session, task_id)
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


VAULT_HEADER = "X-Lightbulb-Vault"
TENANT_HEADER = "X-Lightbulb-Tenant"


def _vault_header(container: dict) -> dict[str, str]:
    """The website-login vault token, delivered on every invoke the backend sends -- the ONLY way
    the bridge learns it (it keeps it in memory). It used to ride the boot secret, but the
    workspace execution role can read that secret, and the agent's shell can read the role's
    credentials over the microVM metadata service. An invoke comes only from a caller holding
    ``bedrock-agentcore:InvokeAgentRuntime``, which the owner role does not have. Empty (no
    header) when the row has no ``id`` or the HMAC key is unset: fills then 401, nothing else."""
    from core.services.agentcore_runtime import derive_vault_token

    try:
        return {VAULT_HEADER: derive_vault_token(container.get("owner_id") or "", container.get("id") or "")}
    except (RuntimeError, ValueError):
        return {}


BOOT_HEADER = "X-Lightbulb-Boot"
#: (owner, row id) -> network seed: fixed for a row's life, so SecretsManager is read once per task.
_NETWORK_SEEDS: dict[tuple[str, str], str | None] = {}


def _network_seed(owner_id: str, row_id: str) -> str | None:
    from core.services.agentcore_runtime import network_seed_sync

    return network_seed_sync(owner_id, row_id)


def _boot_header(container: dict) -> dict[str, str]:
    """What a per-owner runtime reads from its boot secret and env, for a runtime that has neither
    (the shared one): the bridge takes the values of the invoke that binds its session to this
    owner and ignores them on every other (``bridge.bind_tenant``); a per-owner runtime ignores
    them outright. Secrets, handled like the vault token and the session credentials: on the
    invoke only, never logged, never in a runtime's env. Empty without a gateway token (the
    bridge would bind nothing). Sync: runs on the invoke pool."""
    from core.billing.bedrock_gate_tokens import derive_gate_bearer

    owner_id, row_id = container.get("owner_id") or "", container.get("id") or ""
    if not owner_id or not container.get("gateway_token"):
        return {}
    boot: dict[str, Any] = {"gatewayToken": container["gateway_token"], "hookToken": container.get("hook_token") or ""}
    try:
        boot["bedrockBearer"] = derive_gate_bearer(owner_id, row_id, int(container.get("gate_bearer_generation") or 0))
    except (RuntimeError, ValueError):
        pass  # no HMAC key or row id: the gate refuses, as on a boot secret without a bearer
    if (owner_id, row_id) not in _NETWORK_SEEDS:
        try:
            _NETWORK_SEEDS[(owner_id, row_id)] = _network_seed(owner_id, row_id)
        except Exception as exc:  # noqa: BLE001 -- no member this session; retried on the next task
            logger.warning("network seed for owner %s unavailable: %s", owner_id, type(exc).__name__)
    if seed := _NETWORK_SEEDS.get((owner_id, row_id)):
        boot["networkKey"] = seed
    from core.config import settings

    if settings.NETWORK_PROOF_KEY:  # PUBLIC: the sidecar verifies members' sign-in proofs (build_hermes_runtime_env)
        from core.services import network_proof

        boot["issuerKey"] = network_proof.public_key()["env"]
    if container.get("browser_profile_id"):
        boot["browserProfileId"] = container["browser_profile_id"]
    if container.get("network_workers"):
        boot["networkWorkers"] = True
    if container.get("max_concurrent_turns"):
        boot["maxConcurrentTurns"] = int(container["max_concurrent_turns"])
    return {BOOT_HEADER: json.dumps(boot, separators=(",", ":"))}


SESSION_CREDENTIALS_HEADER = "X-Lightbulb-Aws-Session"
#: STS caps a chained AssumeRole at 1 h; a fresh set rides any invoke made in the last 15 min.
SESSION_CREDENTIALS_S = 3600
_SESSION_REFRESH_S = 900
_SESSION_CREDENTIALS: dict[str, tuple[float, dict[str, str]]] = {}
#: After a failed mint, invokes for that owner go without credentials this long before STS is tried again.
_SESSION_FAILED_S = 60
_SESSION_FAILED: dict[str, float] = {}
_STS_CONFIG_KW = {"connect_timeout": 2, "read_timeout": 5, "retries": {"total_max_attempts": 2, "mode": "standard"}}


def _session_credentials_header(owner_id: str) -> dict[str, str]:
    """Credentials for this owner's session only (research 11 §1.3, the token-vending role): an
    AssumeRole of ``HERMES_TENANT_ROLE_ARN`` with ``build_owner_session_policy`` as the session
    policy, so they reach ``<owner>/*`` and this owner's memory and nothing else. The bridge uses
    them for S3 and memory instead of its execution role. Sent like the vault token, on the invoke,
    never in the runtime's env or boot secret. Empty (no header, the bridge keeps its role) when
    the role is unset or STS fails. Sync: runs on the invoke pool."""
    from core.config import settings

    role = settings.HERMES_TENANT_ROLE_ARN
    if not role or not owner_id:
        return {}
    cached = _SESSION_CREDENTIALS.get(owner_id)
    if cached and cached[0] - time.time() > _SESSION_REFRESH_S:
        return cached[1]
    if _SESSION_FAILED.get(owner_id, 0.0) > time.time():
        return {}  # STS failed moments ago: not again on every invoke
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    from core.billing.bedrock_aip import build_owner_session_policy

    try:
        account = role.split(":")[4]  # arn:aws:iam::<account>:role/<name>
        memory_arn = ""
        if settings.AGENTCORE_MEMORY_ID:
            memory_arn = (
                f"arn:aws:bedrock-agentcore:{settings.AWS_REGION}:{account}:memory/{settings.AGENTCORE_MEMORY_ID}"
            )
        policy = build_owner_session_policy(owner_id, settings.OWNER_STATE_BUCKET, memory_arn)
        # Short budgets: an STS outage must cost an invoke seconds, not boto's 60 s connect default.
        sts = boto3.client("sts", region_name=settings.AWS_REGION, config=Config(**_STS_CONFIG_KW))
        creds = sts.assume_role(
            RoleArn=role,
            RoleSessionName=f"lb-{owner_id}"[:64],
            Policy=json.dumps(policy, separators=(",", ":")),
            DurationSeconds=SESSION_CREDENTIALS_S,
        )["Credentials"]
    except (ClientError, BotoCoreError, IndexError) as exc:
        logger.warning("session credentials for owner %s failed: %s", owner_id, str(exc)[:160])
        _SESSION_FAILED[owner_id] = time.time() + _SESSION_FAILED_S
        return {}
    expires = creds["Expiration"]
    header = {
        SESSION_CREDENTIALS_HEADER: json.dumps(
            {
                "accessKeyId": creds["AccessKeyId"],
                "secretAccessKey": creds["SecretAccessKey"],
                "sessionToken": creds["SessionToken"],
                "expiration": expires.isoformat(),
            }
        )
    }
    _SESSION_CREDENTIALS[owner_id] = (expires.timestamp(), header)
    return header


def _invoke_envelope(
    arn: str, owner_id: str, envelope: dict, read_timeout: float, task_id: str | None = None
) -> tuple[dict, str]:
    """One ``InvokeAgentRuntime`` (plus the not-ready retry). Sync; httpx-shaped failures."""
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
                runtimeSessionId=session_id_for(owner_id, task_id),
                contentType="application/json",
                accept="application/json",
                # Same compact encoding as the ingress size cap measures, so a body that
                # clears 1 MiB there is not inflated past it here.
                payload=json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(),
            )
            raw = result.get("response")
            text = raw.read().decode() if hasattr(raw, "read") else (raw if isinstance(raw, str) else "")
            return result, text
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
    # Every attempt hit not-ready; raise the last one exactly as a first-attempt failure would.
    assert not_ready is not None
    raise httpx.ConnectError(str(not_ready)) from not_ready


# --- Single-flight wake ------------------------------------------------------------------------
#
# AgentCore does NOT serialize a session's first claim: N concurrent invokes on a session with no
# compute get N microVMs from the warm pool (reproduced on dev 2026-09-25; no 409 is ever
# returned), and every later invoke settles on one of them while the rest live on, orphaned, to
# maxLifetime. Two timer wakes landing on two backend tasks within ~1 s after a roll did exactly
# that in prod twice on 09-25. So the backend decides "cold" itself and lets ONE caller wake it.
#
# The estimate lives on ``__session__#<owner>`` (container_repo) and errs toward cold: a wrong
# "cold" costs one serialized health call; a wrong "warm" (a stop the backend never saw) re-opens
# the race, which is why every stop goes through ``hermes_provisioning.stop_runtime_session``.

#: The winner's lease: the 90 s cold budget plus margin. A holder whose task dies lets it lapse.
WAKE_LEASE_S = 120
#: How far inside AgentCore's own idle / lifetime cut-offs "warm" stops being trusted. A third of
#: the 5 min idle window, so 200 s of it is trusted.
WARM_MARGIN_S = 100
#: Only move ``warm_until`` when it moves this far, or every invoke is a DynamoDB write. Must stay
#: well under IDLE_TIMEOUT_S - WARM_MARGIN_S, or a warm session is never extended.
WARM_EXTEND_STEP_S = 60
_WAKE_POLL_S = 1.0


def _warm_until(now: int, started_at: int) -> int:
    from core.services.agentcore_runtime import IDLE_TIMEOUT_S, MAX_LIFETIME_S

    return min(now + IDLE_TIMEOUT_S - WARM_MARGIN_S, started_at + MAX_LIFETIME_S - WARM_MARGIN_S)


def _ensure_session(owner_id: str, arn: str, deadline: float, wake, task_id: str | None = None) -> dict | None:
    """Return once the session is warm (or we warmed it), within the caller's ``deadline``.

    Returns the session row to extend after the invoke, or ``None`` when DynamoDB itself failed
    and we invoke without the gate (the pre-gate behaviour). Sync: runs on the invoke pool.
    """
    import uuid

    from botocore.exceptions import BotoCoreError, ClientError

    from core.repositories import container_repo as repo

    token = uuid.uuid4().hex
    # Only a task session names its row; the workspace path calls the repo exactly as before.
    tk = {"task_id": task_id} if task_id is not None else {}
    try:
        while True:
            now = int(time.time())
            row = repo.get_session_sync(owner_id, **tk)
            # Warm only for THIS runtime: a re-created runtime starts with no session at all.
            if int(row.get("warm_until") or 0) > now and row.get("arn") == arn:
                return row
            if int(row.get("waking_until") or 0) < now and repo.try_take_wake_lease_sync(
                owner_id, arn=arn, token=token, now=now, lease_s=WAKE_LEASE_S, **tk
            ):
                break
            if time.monotonic() + _WAKE_POLL_S >= deadline:
                # Never invoke blindly while someone else is waking: the caller's retry path.
                raise httpx.ReadTimeout("agentcore session is waking")
            time.sleep(_WAKE_POLL_S)
    except (ClientError, BotoCoreError) as exc:
        logger.warning("session gate unavailable for owner %s; invoking without it: %s", owner_id, exc)
        return None

    # AgentCore counts maxLifetime from the claim, which this invoke makes as it STARTS; stamping
    # before the wake (not after its up-to-a-minute cold start) errs toward cold.
    started = int(time.time())
    try:
        wake()
    except BaseException:
        try:
            repo.finish_wake_sync(owner_id, token=token, **tk)
        except Exception:  # noqa: BLE001 -- the lease lapses on its own
            logger.warning("wake lease release failed for owner %s", owner_id, exc_info=True)
        raise
    row = {"warm_until": _warm_until(int(time.time()), started), "started_at": started, "arn": arn}
    try:
        repo.finish_wake_sync(owner_id, token=token, **tk, **row)
    except (ClientError, BotoCoreError):
        logger.warning("session warm mark failed for owner %s", owner_id, exc_info=True)
    return row


def _note_invoked(owner_id: str, session: dict | None, task_id: str | None = None) -> None:
    """Slide ``warm_until`` after a successful invoke, at most once per ``WARM_EXTEND_STEP_S``."""
    if not session:
        return
    now = int(time.time())
    new = _warm_until(now, int(session.get("started_at") or now))
    if new - int(session.get("warm_until") or 0) <= WARM_EXTEND_STEP_S:
        return
    from core.repositories import container_repo as repo

    try:
        repo.extend_session_warm_sync(owner_id, warm_until=new, **({"task_id": task_id} if task_id is not None else {}))
    except Exception:  # noqa: BLE001 -- a missed extend costs one serialized health, later
        logger.warning("session warm extend failed for owner %s", owner_id, exc_info=True)
