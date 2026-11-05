"""The one backend client of the bridge's generic run contract, /internal/v1/runs (spec 02 §5.1; C12).

Wave 2's Temporal activities are its only callers: no route, no flag here (contracts-v2 §1: the backend
decides the mode and writes it onto the request; wave 1 sends Worker mode on the workspace's own VM).
`create_run` mints the task run's local-session grant and forwards it (contracts-v2 §14.3), so paired
local sessions work in tasks the way they do in Paperclip runs.
"""

import logging
from dataclasses import dataclass
from typing import Literal

import httpx

from core.repositories import container_repo
from core.services import local_session_grants, runtime_transport
from core.services.messaging import ApiProblem
from core.tasks_schema import worker_session_key

logger = logging.getLogger(__name__)

CREATE_TIMEOUT_S = 90  # a parked workspace's cold start (54-57 s measured) plus the accept
POLL_TIMEOUT_S = 25


class RunLost(Exception):
    """404: the bridge does not know the run (its microVM died before any durable record)."""


class RunRefused(Exception):
    """400/409: a malformed or unsupported request, or the same run id with another request."""


class RuntimeBusy(Exception):
    """503 or an unreachable runtime: retryable."""


@dataclass(frozen=True)
class RunIdentity:
    run_id: str
    owner_id: str
    task_id: str
    attempt: int
    agent_id: str

    @property
    def session_key(self) -> str:
        return worker_session_key(self.agent_id, self.task_id)

    def to_wire(self) -> dict:
        return {
            "runId": self.run_id,
            "ownerId": self.owner_id,
            "taskId": self.task_id,
            "attempt": self.attempt,
            "agentId": self.agent_id,
            "sessionKey": self.session_key,
            "mode": "worker",
            "session": {"kind": "workspace"},
        }


@dataclass(frozen=True)
class RunRequest:
    input: str
    instructions: str
    max_iterations: int | None = None
    run_budget_seconds: int | None = None

    def to_wire(self) -> dict:
        limits = {"max_iterations": self.max_iterations, "run_budget_seconds": self.run_budget_seconds}
        return {
            "input": self.input,
            "instructions": self.instructions,
            "effects": "off",
            "limits": {k: v for k, v in limits.items() if v is not None},
        }


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    status: str
    output: str | None
    error: str | None
    usage: dict | None
    recent_tools: list[dict]


async def _container(owner_id: str) -> dict:
    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        raise RunRefused("no container for this workspace")
    return container


async def _call(owner_id: str, path: str, *, method: str, body: dict, timeout_s: float, grant: str | None = None):
    container = await _container(owner_id)
    headers = {"Authorization": f"Bearer {container.get('gateway_token') or ''}"}
    if grant is not None:
        headers["X-Lightbulb-Local-Session-Token"] = grant
    try:
        response = await runtime_transport.request(
            container, path, method=method, json_body=body, headers=headers, timeout_s=timeout_s
        )
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeBusy(type(exc).__name__) from exc
    data = response.json() or {}
    if response.status_code == 404:
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
    )


def _run_path(identity: RunIdentity, suffix: str = "") -> str:
    return f"/internal/v1/runs/{identity.run_id}{suffix}"


async def create_run(owner_id: str, identity: RunIdentity, request: RunRequest) -> RunStatus:
    try:
        grant = local_session_grants.mint_task(
            owner_id, identity.agent_id, identity.session_key, identity.run_id, identity.task_id
        )
    except (RuntimeError, ApiProblem):
        grant = None
        logger.warning("task run local session invocation unavailable")
    data = await _call(
        owner_id,
        "/internal/v1/runs",
        method="POST",
        body={"identity": identity.to_wire(), "request": request.to_wire()},
        timeout_s=CREATE_TIMEOUT_S,
        grant=grant,
    )
    return _status(data)


async def get_run(owner_id: str, identity: RunIdentity) -> RunStatus:
    data = await _call(
        owner_id, _run_path(identity), method="GET", body={"identity": identity.to_wire()}, timeout_s=POLL_TIMEOUT_S
    )
    return _status(data)


async def steer_run(owner_id: str, identity: RunIdentity, text: str, kind: Literal["user", "input"]) -> bool:
    data = await _call(
        owner_id,
        _run_path(identity, "/steer"),
        method="POST",
        body={"identity": identity.to_wire(), "text": text, "kind": kind},
        timeout_s=POLL_TIMEOUT_S,
    )
    return bool(data.get("accepted"))


async def stop_run(owner_id: str, identity: RunIdentity) -> RunStatus:
    data = await _call(
        owner_id,
        _run_path(identity, "/stop"),
        method="POST",
        body={"identity": identity.to_wire()},
        timeout_s=POLL_TIMEOUT_S,
    )
    return _status(data)  # "stopping" (202) while the bridge interrupts it, else the terminal status
