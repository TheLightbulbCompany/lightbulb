"""Dev fault hooks (wave-2 O Task 10): off in prod, and each hook does exactly what the scenarios need."""

import pytest
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import ActivityEnvironment

from core import tasks_schema as ts
from core.config import Settings, settings
from core.repositories import tasks_repo as repo
from core.services import super_agent_faults as faults
from core.temporal import activities as acts
from core.temporal import client
from core.temporal.types import WorkerTurnIn

from .test_router_turn import BATCH, FIN, ROOM, WS, _run, _start, _trig, world  # noqa: F401
from .test_tasks_repo import table  # noqa: F401


@pytest.fixture
def faults_on(monkeypatch):
    def set_faults(value: str) -> None:
        monkeypatch.setattr(settings, "SUPER_AGENT_FAULTS", value)

    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    return set_faults


def test_faults_refused_in_prod(monkeypatch):
    with pytest.raises(ValueError, match="dev-only"):
        Settings(**{**settings.model_dump(), "ENVIRONMENT": "prod", "SUPER_AGENT_FAULTS": "temporal_down"})
    Settings(**{**settings.model_dump(), "ENVIRONMENT": "dev", "SUPER_AGENT_FAULTS": "temporal_down"})
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "SUPER_AGENT_FAULTS", "temporal_down,fake_worker:5")
    assert faults.fault("temporal_down") is None and faults.fault("fake_worker") is None


def test_fault_params(faults_on):
    faults_on(" temporal_down , fake_worker:250,conditional_conflict:30 ")
    assert faults.fault("temporal_down") == "" and faults.fault("fake_worker") == "250"
    assert faults.fault("conditional_conflict") == "30" and faults.fault("scripted_router") is None
    faults_on("")
    assert faults.fault("temporal_down") is None


def test_temporal_down_raises_unavailable(faults_on, monkeypatch):
    monkeypatch.setattr(client, "_client", object())
    assert client.get_client() is not None
    faults_on("temporal_down")
    with pytest.raises(RPCError) as err:
        client.get_client()
    assert err.value.status == RPCStatusCode.UNAVAILABLE


@pytest.mark.asyncio
async def test_conditional_conflict_rate(faults_on, table):  # noqa: F811
    faults_on("conditional_conflict:0")
    row = {
        "taskId": ts.new_task_id(),
        "identifier": "LB-1",
        "title": "t",
        "status": "queued",
        "audience": {"kind": "workspace"},
        "lane": "shared",
        "triggerId": "chat:m1",
    }
    created, fresh = await repo.create_task(WS, row)
    assert fresh is True
    faults_on("conditional_conflict:100")
    with pytest.raises(repo.TasksRepoError):  # the conditional put "loses" and no row exists to replay
        await repo.create_task(WS, {**row, "taskId": ts.new_task_id()})
    with pytest.raises(repo.IllegalTransition):  # the conditional update "loses" too
        await repo.transition_task(WS, created["taskId"], to="working")
    faults_on("")
    assert (await repo.transition_task(WS, created["taskId"], to="working"))["status"] == "working"


@pytest.mark.asyncio
async def test_scripted_router_decisions_by_text(faults_on, world, monkeypatch):  # noqa: F811
    script = {"pay acme and book the flights": [_start([], ack="Scripted: paying Acme.")]}
    monkeypatch.setattr(faults, "_read_script", lambda key: script if key == "dev/router.json" else {})
    faults_on("scripted_router:dev/router.json")
    t1 = await _trig("chat:m1")
    t2 = await _trig("chat:m2", ref={"text": "unknown text", "deliverRoomId": ROOM}, source="manual")
    out = await _run([t1, t2])
    assert world["runs"] == []  # no Router run
    assert out.started_task_ids == [ts.router_task_id(BATCH, 0, 0)]
    task = await repo.get_task(WS, out.started_task_ids[0])
    assert task["specialist"] == FIN and task["title"] == "Pay Acme"
    texts = {p["event_id"]: p["text"] for p in world["posts"]}
    assert texts[f"router:{BATCH}:0"] == "Scripted: paying Acme."
    assert (
        texts[f"router:{BATCH}:1"] == acts.router_prompt.FALLBACK
    )  # no script entry: a respond trigger still hears back


@pytest.mark.asyncio
async def test_router_turn_timeout_fault(faults_on, world):  # noqa: F811
    faults_on("router_turn_timeout")
    t1 = await _trig("chat:m1")
    with pytest.raises(TimeoutError):
        await _run([t1])
    assert world["runs"] == [] and world["posts"] == []


@pytest.mark.asyncio
async def test_fake_worker_completes(faults_on, world, monkeypatch):  # noqa: F811
    faults_on("fake_worker:0")
    tid = ts.new_task_id()
    await repo.create_task(
        WS,
        {
            "taskId": tid,
            "identifier": "LB-1",
            "title": "Pay the invoice",
            "status": "queued",
            "audience": {"kind": "workspace"},
            "lane": "shared",
            "triggerId": "chat:m1",
        },
    )
    ref = await acts.begin_attempt(WS, tid, [])
    out = await ActivityEnvironment().run(acts.worker_turn, WorkerTurnIn(WS, tid, 1, ref.run_id))
    assert (out.outcome, out.error) == ("completed", None)
    assert world["runs"] == []  # no bridge run
    att = await repo.get_attempt(WS, tid, 1)
    assert att["output"] == "Done (fake worker): Pay the invoice"
