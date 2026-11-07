"""V3 backend half: `session.kind="task"` on the run identity, the per-attempt kind decision, the
task-session stop and the curate client. The bridge side (accepting kind=task, sealing) is V3b."""

from unittest.mock import AsyncMock

import pytest

from core.services import hermes_provisioning, runtime_runs as rr, token_custody
from core.services.runtime_transport import RuntimeResponse, TaskSessionRefused

OWNER = "org_v3"
TASK = "7d1e0c6a-4b1f-4d55-9c1e-1f2a3b4c5d6e"
RUN = "0b9a3c1e-6f2d-4e5a-9b7c-1d2e3f4a5b6c"
ARN = "arn:aws:bedrock-agentcore:::runtime/r"


def _identity(kind: str = "task") -> rr.RunIdentity:
    return rr.RunIdentity(run_id=RUN, owner_id=OWNER, task_id=TASK, attempt=2, agent_id="a1", session_kind=kind)


@pytest.fixture
def transport(monkeypatch):
    """A fake runtime_transport.request recording every call's path and kwargs."""
    calls: list[dict] = []

    async def request(container, path, **kw):
        calls.append({"path": path, **kw})
        body = {"run_id": RUN, "status": "running", "accepted": True, "ran": True, "changed": False}
        return RuntimeResponse(status_code=200, text=__import__("json").dumps(body))

    monkeypatch.setattr(rr, "_container", AsyncMock(return_value={"owner_id": OWNER, "gateway_token": "t"}))
    monkeypatch.setattr(rr.runtime_transport, "request", request)
    monkeypatch.setattr(rr.local_session_grants, "mint_task", lambda *a, **k: "grant")
    return calls


def test_task_identity_wire_and_session_id():
    wire = _identity("task").to_wire()
    assert wire["session"] == {"kind": "task"} and wire["taskId"] == TASK
    assert rr.RunIdentity(run_id=RUN, owner_id=OWNER, task_id=TASK, attempt=1, agent_id="a1").to_wire()["session"] == {
        "kind": "workspace"
    }


@pytest.mark.asyncio
async def test_task_runs_route_every_call_to_the_task_session(transport):
    identity, request = _identity("task"), rr.RunRequest(input="x", instructions="y")
    await rr.create_run(OWNER, identity, request)
    await rr.get_run(OWNER, identity)
    await rr.steer_run(OWNER, identity, "hi", "input")
    await rr.stop_run(OWNER, identity)
    assert [c["path"] for c in transport] == [
        "/internal/v1/runs",
        f"/internal/v1/runs/{RUN}",
        f"/internal/v1/runs/{RUN}/steer",
        f"/internal/v1/runs/{RUN}/stop",
    ]
    assert all(c["task_id"] == TASK for c in transport)
    assert all(c["json_body"]["identity"]["session"] == {"kind": "task"} for c in transport)


@pytest.mark.asyncio
async def test_workspace_runs_keep_the_pre_task_call_shape(transport):
    await rr.get_run(OWNER, _identity("workspace"))
    assert "task_id" not in transport[0]  # the transport's workspace path is byte-for-byte today's


@pytest.mark.asyncio
async def test_task_session_refused_is_run_refused(monkeypatch):
    async def refuse(container, path, **kw):
        raise TaskSessionRefused("task session refused for /x")

    monkeypatch.setattr(rr, "_container", AsyncMock(return_value={"owner_id": OWNER}))
    monkeypatch.setattr(rr.runtime_transport, "request", refuse)
    with pytest.raises(rr.RunRefused, match="task_session_refused"):
        await rr.get_run(OWNER, _identity("task"))


@pytest.mark.asyncio
async def test_session_kind_recomputed_each_attempt(monkeypatch):
    gate = AsyncMock(return_value=True)
    monkeypatch.setattr(token_custody, "task_vm_allowed", gate)
    assert await rr.session_kind_for(OWNER) == "task"
    gate.return_value = False  # the workspace switched to ChatGPT login mid-task
    assert await rr.session_kind_for(OWNER) == "workspace"
    assert gate.await_count == 2  # asked every time, never cached


@pytest.mark.asyncio
async def test_stop_task_session_targets_task_session(monkeypatch):
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(hermes_provisioning, "stop_runtime_session", stop)
    monkeypatch.setattr(rr.container_repo, "get_by_owner_id", AsyncMock(return_value={"agentcore_runtime_arn": ARN}))
    assert await rr.stop_task_session(OWNER, TASK) is True
    stop.assert_awaited_once_with(OWNER, ARN, task_id=TASK)
    monkeypatch.setattr(rr.container_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": OWNER}))
    assert await rr.stop_task_session(OWNER, TASK) is False  # no runtime: nothing to stop
    assert stop.await_count == 1


@pytest.mark.asyncio
async def test_curate_goes_to_workspace_session(transport, monkeypatch):
    monkeypatch.setattr(hermes_provisioning, "_session_warm", AsyncMock(return_value=True))
    assert await rr.curate(OWNER, "a1") == {"ran": True, "changed": False}
    [call] = transport
    assert call["path"] == "/internal/v1/curate" and call["json_body"] == {"agentId": "a1"}
    assert "task_id" not in call


@pytest.mark.asyncio
async def test_curate_never_wakes_a_parked_workspace(transport, monkeypatch):
    """Phase 2: the learning workflow curated after every task, waking the workspace VM each time."""
    monkeypatch.setattr(hermes_provisioning, "_session_warm", AsyncMock(return_value=False))
    assert await rr.curate(OWNER, "a1") == {"ran": False, "changed": False}
    assert transport == []


def test_task_vm_cap():
    assert rr.MAX_TASK_VMS_PER_WORKSPACE == 10


@pytest.mark.asyncio
async def test_activities_identity_carries_the_attempts_session_kind(monkeypatch):
    """O's `_identity` reads the kind off the Attempt row `begin_attempt` wrote, so a task-session
    attempt's every run call says `session.kind == "task"`; the shim awaits the async gate."""
    from core.temporal import activities as acts

    att = {"runId": RUN, "n": 2, "agentId": "a1", "sessionId": rr.runtime_transport.session_id_for(OWNER, TASK)}
    assert acts._identity(OWNER, TASK, att).to_wire()["session"] == {"kind": "task"}
    att["sessionId"] = rr.runtime_transport.session_id_for(OWNER)
    assert acts._identity(OWNER, TASK, att).to_wire()["session"] == {"kind": "workspace"}
    monkeypatch.setattr(rr, "session_kind_for", AsyncMock(return_value="task"))
    assert await acts._session_kind(OWNER) == "task"


def test_for_attempt_derives_session_kind_from_the_attempt_row():
    from core.services import runtime_transport

    att = {
        "runId": RUN,
        "n": 3,
        "agentId": "a1",
        "runSecret": "s",
        "sessionId": runtime_transport.session_id_for(OWNER, TASK),
    }
    task_run = rr.RunIdentity.for_attempt(OWNER, TASK, att)
    assert (task_run.session_kind, task_run.attempt, task_run.run_secret, task_run.run_id) == ("task", 3, "s", RUN)
    shared = rr.RunIdentity.for_attempt(OWNER, TASK, {**att, "sessionId": runtime_transport.session_id_for(OWNER)})
    assert shared.session_kind == "workspace" and shared.to_wire()["session"] == {"kind": "workspace"}


@pytest.mark.asyncio
async def test_router_runs_mint_no_task_grant(transport, monkeypatch):
    """Dev logs (5 Nov): every Router batch logged "task run local session invocation unavailable" -- a task
    grant cannot bind a Router session. A Router run asks for none; a Worker run still gets one."""
    minted = []
    monkeypatch.setattr(rr.local_session_grants, "mint_task", lambda *a, **k: minted.append(a) or "grant")
    router = rr.RunIdentity(
        run_id=RUN, owner_id=OWNER, task_id=TASK, attempt=1, agent_id="ceo", mode="router", lane="shared", room="r"
    )
    await rr.create_run(OWNER, router, rr.RunRequest(input="x", instructions="y"))
    assert minted == [] and "X-Lightbulb-Local-Session-Token" not in transport[0]["headers"]
    await rr.create_run(OWNER, _identity("task"), rr.RunRequest(input="x", instructions="y"))
    assert len(minted) == 1 and transport[1]["headers"]["X-Lightbulb-Local-Session-Token"] == "grant"
