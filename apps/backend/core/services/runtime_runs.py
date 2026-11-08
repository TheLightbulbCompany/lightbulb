"""The one backend client of the bridge's generic run contract, /internal/v1/runs (spec 02 §5.1; C12).

Wave 2's Temporal activities are its only callers: no route, no flag here (contracts-v2 §1: the backend
decides the mode and writes it onto the request; wave 1 sends Worker mode on the workspace's own VM).
`create_run` mints the task run's local-session grant and forwards it (contracts-v2 §14.3), so paired
local sessions work in tasks.
"""

import logging
from dataclasses import dataclass, field
from typing import Literal

import httpx

from core.config import settings
from core.repositories import container_repo
from core.services import local_session_grants, runtime_transport
from core.services.messaging import ApiProblem
from core.tasks_schema import router_session_key, worker_session_key

logger = logging.getLogger(__name__)

CREATE_TIMEOUT_S = 90  # a parked workspace's cold start (54-57 s measured) plus the accept
POLL_TIMEOUT_S = 25
CURATE_TIMEOUT_S = 300  # the curator's synchronous review on the workspace VM, under its profile lock
#: Per-task AgentCore sessions a workspace may have at once (contracts-v2 §9: the shared VM keeps its
#: cap of 3; O's slot code reads this for task sessions). ponytail: one fixed cap for every plan --
#: make it per plan when a customer needs more.
MAX_TASK_VMS_PER_WORKSPACE = 10

SessionKind = Literal["workspace", "task"]


class RunLost(Exception):
    """404: the bridge does not know the run (its microVM died before any durable record)."""


class RunRefused(Exception):
    """400/409: a malformed or unsupported request, or the same run id with another request."""


class RuntimeBusy(Exception):
    """503 or an unreachable runtime: retryable."""


@dataclass(frozen=True)
class RunIdentity:
    """A Worker turn of one task, or (mode "router") one Router batch: `task_id` is then the batch id and
    `agent_id` the CEO's, and the run lives in the lane's Router session (`lane`: "shared" or a lane key)."""

    run_id: str
    owner_id: str
    task_id: str
    attempt: int
    agent_id: str
    run_secret: str | None = None  # the Attempt's secret; the bridge echoes it on run events and inputs
    mode: Literal["worker", "router"] = "worker"
    lane: str | None = None
    room: str | None = None  # Router runs: `router_room_key(trigger_scope)`, one session per lane and conversation
    model: str | None = None  # Router runs: the cheaper model (§9); the bridge overrides the profile's
    # "task": the run lives in its own AgentCore session (`runtime_transport.session_id_for(owner, task_id)`)
    # and the bridge latches task mode (read-only specialist profile, task-prefix writes). Decided per attempt
    # by `session_kind_for`, written on the Attempt row by O's `begin_attempt`, never cached on the task.
    session_kind: SessionKind = "workspace"

    @classmethod
    def for_attempt(cls, owner_id: str, task_id: str, att: dict) -> "RunIdentity":
        """The one way to name an Attempt's run: the Attempt row records where `begin_attempt` put it (V3), so
        every reader (worker_turn, the trace route, the watchdog) derives `session_kind` the same way."""
        kind: SessionKind = (
            "task" if att.get("sessionId") == runtime_transport.session_id_for(owner_id, task_id) else "workspace"
        )
        return cls(
            att["runId"],
            owner_id,
            task_id,
            att["n"],
            att["agentId"],
            run_secret=att.get("runSecret"),
            session_kind=kind,
        )

    @property
    def session_key(self) -> str:
        if self.mode == "router":
            if not self.room:
                raise ValueError("a Router run names its conversation (room)")
            return router_session_key(self.agent_id, self.lane or "shared", self.room)
        return worker_session_key(self.agent_id, self.task_id)

    def to_wire(self) -> dict:
        wire = {
            "runId": self.run_id,
            "ownerId": self.owner_id,
            "taskId": self.task_id,
            "attempt": self.attempt,
            "agentId": self.agent_id,
            "sessionKey": self.session_key,
            "mode": self.mode,
            "session": {"kind": self.session_kind},
            **({"runSecret": self.run_secret} if self.run_secret else {}),
        }
        if self.mode == "router":
            wire["lane"] = self.lane or "shared"
            if self.room:
                wire["room"] = self.room
            if self.model:
                wire["model"] = self.model
        return wire


@dataclass(frozen=True)
class RunRequest:
    input: str
    instructions: str
    max_iterations: int | None = None
    run_budget_seconds: int | None = None
    memory: dict | None = None  # spec 07 §5.2, built by memory_tags; absent = today's behaviour
    # Lane Q (contracts §8): "required" installs the bridge's effect checkpoint (F1); the bridge refuses it with
    # `unsupported_in_this_version` until the image carries F1. `faults` are dev-only kill hooks the bridge reads.
    effects: Literal["off", "required"] = "off"
    faults: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.effects not in ("off", "required"):
            raise ValueError(f"effects must be off or required, not {self.effects!r}")
        if self.faults and settings.ENVIRONMENT != "dev":
            raise ValueError("fault hooks are dev only")

    def to_wire(self) -> dict:
        limits = {"max_iterations": self.max_iterations, "run_budget_seconds": self.run_budget_seconds}
        wire = {
            "input": self.input,
            "instructions": self.instructions,
            "effects": self.effects,
            "limits": {k: v for k, v in limits.items() if v is not None},
        }
        if self.memory is not None:
            wire["memory"] = self.memory
        if self.faults:
            wire["faults"] = list(self.faults)
        return wire


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    status: str
    output: str | None
    error: str | None
    usage: dict | None
    recent_tools: list[dict]
    decisions: list[dict] = field(default_factory=list)  # a Router run's decisions, in order
    stage: str | None = None  # a running Router run: the tool it is writing ("reply", "start_task"), its name only


async def _container(owner_id: str) -> dict:
    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        raise RunRefused("no container for this workspace")
    return container


async def _call(
    owner_id: str,
    path: str,
    *,
    method: str,
    body: dict,
    timeout_s: float,
    grant: str | None = None,
    task_id: str | None = None,
):
    container = await _container(owner_id)
    headers = {"Authorization": f"Bearer {container.get('gateway_token') or ''}"}
    if grant is not None:
        headers["X-Lightbulb-Local-Session-Token"] = grant
    # The workspace path calls the transport exactly as before; only a task session names its task.
    session = {"task_id": task_id} if task_id is not None else {}
    try:
        response = await runtime_transport.request(
            container, path, method=method, json_body=body, headers=headers, timeout_s=timeout_s, **session
        )
    except runtime_transport.TaskSessionRefused as exc:
        raise RunRefused(f"task_session_refused: {exc}") from exc  # a programming error, never retried
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeBusy(type(exc).__name__) from exc
    data = response.json()
    if not isinstance(data, dict):
        data = {}
    if response.status_code == 404:
        if data.get("error") == "not_found":  # an older image without /internal/v1/runs: no route, not a lost run
            raise RunRefused("v1_runs_unsupported")
        raise RunLost(data.get("error") or "run_not_found")
    if response.status_code in (400, 403, 409):
        raise RunRefused(data.get("error") or str(response.status_code))
    if response.status_code >= 300:
        raise RuntimeBusy(data.get("error") or str(response.status_code))
    return data


def _status(data: dict) -> RunStatus:
    return RunStatus(
        run_id=data["run_id"],
        status=data["status"],
        output=data.get("output"),
        error=data.get("error"),
        usage=data.get("usage"),
        recent_tools=list(data.get("recentTools") or []),
        decisions=list(data.get("decisions") or []),
        stage=data.get("stage"),
    )


def _run_path(identity: RunIdentity, suffix: str = "") -> str:
    return f"/internal/v1/runs/{identity.run_id}{suffix}"


def _task_session(identity: RunIdentity) -> dict:
    return {"task_id": identity.task_id} if identity.session_kind == "task" else {}


async def session_kind_for(owner_id: str) -> SessionKind:
    """Where this attempt runs: its own task session iff no
    rotating credential is still the VM's to refresh (`token_custody.task_vm_allowed`, spec 13 §5.4).
    Called by O's `begin_attempt` at every attempt start; the answer is never cached on the task."""
    from core.services import token_custody

    return "task" if await token_custody.task_vm_allowed(owner_id) else "workspace"


async def stop_task_session(owner_id: str, task_id: str, run_id: str | None = None) -> bool:
    """Stop the task's own AgentCore session (and clear only its warm row). False when the workspace
    has no runtime; otherwise `hermes_provisioning.stop_runtime_session`'s answer. Called by V's
    `close_task_attempt` after the seal, before the learning merge is signalled. With `run_id`, the
    workspace browser lease that run still holds is freed after the stop (contracts §14.11: a VM
    stopped mid-tool never released it; the next task's run would wait out the lease)."""
    from core.services import browser_control, hermes_provisioning

    container = await container_repo.get_by_owner_id(owner_id) or {}
    arn = runtime_transport.runtime_arn(container)
    if not arn:
        return False
    stopped = await hermes_provisioning.stop_runtime_session(owner_id, arn, task_id=task_id)
    if run_id:
        try:
            await browser_control.release_for_run(owner_id, run_id)
        except Exception:  # noqa: BLE001 -- the lease still expires on its own; never cost the merge signal
            logger.warning("task browser release failed task=%s", task_id, exc_info=True)
    return stopped


async def curate(owner_id: str, agent_id: str) -> dict:
    """Run the curator for one specialist on the WORKSPACE session (`POST /internal/v1/curate`,
    spec 07 §3.5: never in a Worker VM). Returns the bridge's `{"ran", "changed"}`.

    Only on a warm workspace session (Phase 2: nothing but a live use wakes a workspace): a cold one
    answers `{"ran": False}` without an invoke, and the bridge's own idle pass (`_curate_when_idle`)
    curates on the next turn there. The curator comes due every 7 days anyway; waking a parked
    microVM after every task bought a ~55 s cold start and a full idle tail for a no-op."""
    from core.services import hermes_provisioning

    container = await _container(owner_id)
    if not await hermes_provisioning._session_warm(owner_id, runtime_transport.runtime_arn(container)):
        return {"ran": False, "changed": False}
    data = await _call(
        owner_id, "/internal/v1/curate", method="POST", body={"agentId": agent_id}, timeout_s=CURATE_TIMEOUT_S
    )
    return {"ran": bool(data.get("ran")), "changed": bool(data.get("changed"))}


async def create_run(owner_id: str, identity: RunIdentity, request: RunRequest) -> RunStatus:
    grant = None
    if identity.mode == "worker":  # a Router batch has no task, run or Worker session a task grant could bind
        try:
            grant = local_session_grants.mint_task(
                owner_id, identity.agent_id, identity.session_key, identity.run_id, identity.task_id
            )
        except (RuntimeError, ApiProblem) as exc:
            logger.warning("task run local session invocation unavailable: %s", type(exc).__name__)
    data = await _call(
        owner_id,
        "/internal/v1/runs",
        method="POST",
        body={"identity": identity.to_wire(), "request": request.to_wire()},
        timeout_s=CREATE_TIMEOUT_S,
        grant=grant,
        **_task_session(identity),
    )
    return _status(data)


async def get_run(owner_id: str, identity: RunIdentity) -> RunStatus:
    data = await _call(
        owner_id,
        _run_path(identity),
        method="GET",
        body={"identity": identity.to_wire()},
        timeout_s=POLL_TIMEOUT_S,
        **_task_session(identity),
    )
    return _status(data)


async def steer_run(owner_id: str, identity: RunIdentity, text: str, kind: Literal["user", "input"]) -> bool:
    data = await _call(
        owner_id,
        _run_path(identity, "/steer"),
        method="POST",
        body={"identity": identity.to_wire(), "text": text, "kind": kind},
        timeout_s=POLL_TIMEOUT_S,
        **_task_session(identity),
    )
    return bool(data.get("accepted"))


async def stop_run(owner_id: str, identity: RunIdentity) -> RunStatus:
    data = await _call(
        owner_id,
        _run_path(identity, "/stop"),
        method="POST",
        body={"identity": identity.to_wire()},
        timeout_s=POLL_TIMEOUT_S,
        **_task_session(identity),
    )
    return _status(data)  # "stopping" (202) while the bridge interrupts it, else the terminal status
