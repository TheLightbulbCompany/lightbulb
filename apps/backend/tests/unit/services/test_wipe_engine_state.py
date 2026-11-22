"""The terminal wipe's engine step (`account_wipe.wipe_engine_state`): meeting bots, Temporal and the tasks table.

moto `tasks` table; a fake Temporal client records terminations and Schedule deletes; a recorder stands in for
Recall. Two owners share every store, so each test also proves the other owner's state is untouched."""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from moto import mock_aws
from temporalio.service import RPCError, RPCStatusCode

from core import dynamodb, tasks_schema as ts
from core.services import account_wipe
from core.temporal import client as temporal_client
from tests.unit.services.test_account_wipe import OWNER_ID, wipe_mocks  # noqa: F401 -- the fixture
from tests.unit.super_agent.registry.conftest import _create_tasks_table

A, B = "user_a", "user_b"


class _Handle:
    def __init__(self, fake: "_FakeTemporal", wid: str):
        self.fake, self.wid = fake, wid

    async def terminate(self, reason: str) -> None:
        if self.wid not in self.fake.running:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        self.fake.running.discard(self.wid)
        self.fake.terminated.append(self.wid)

    async def delete(self) -> None:
        if self.wid not in self.fake.schedules:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        self.fake.schedules.discard(self.wid)


class _FakeTemporal:
    """Answers the visibility query the way Temporal does for the `STARTS_WITH` clauses it sends."""

    def __init__(self, running: set[str], schedules: set[str]):
        self.running, self.schedules, self.terminated, self.queries = running, schedules, [], []

    def get_workflow_handle(self, wid: str) -> _Handle:
        return _Handle(self, wid)

    def get_schedule_handle(self, sid: str) -> _Handle:
        return _Handle(self, sid)

    async def list_workflows(self, query: str):
        self.queries.append(query)
        assert query.startswith("ExecutionStatus = 'Running' AND (")
        prefixes = re.findall(r"WorkflowId STARTS_WITH '([^']*)'", query)
        for wid in sorted(self.running):
            if any(wid.startswith(p) for p in prefixes):
                yield SimpleNamespace(id=wid)


def _workflows(ws: str) -> set[str]:
    return {
        ts.router_workflow_id(ws),
        ts.task_workflow_id(ws, "t1"),
        ts.learning_workflow_id(ws, "ag1"),
        f"gwatch:{ws}:h1",
        f"fire:{ws}:s1",
        f"hook:{ws}:h1:k1",
    }


def _seed(table, ws: str) -> None:
    pk = ts.ws_pk(ws)
    for sk, extra in (
        (ts.META_SK, {}),
        (ts.task_sk("t1"), {}),
        (ts.schedule_sk("s1"), {"scheduleId": "s1"}),
        (ts.agent_sk(f"{ws}-ag1"), {}),
    ):
        table.put_item(Item={"pk": pk, "sk": sk, **extra})
    table.put_item(Item={"pk": ts.agent_owner_pk(f"{ws}-ag1"), "sk": ts.AGENT_OWNER_SK, "ws": ws})


def _rows(table) -> set[tuple[str, str]]:
    return {(i["pk"], i["sk"]) for i in table.scan()["Items"]}


@pytest.fixture
def world(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        table = _create_tasks_table(resource)
        _seed(table, A)
        _seed(table, B)
        fake = _FakeTemporal(
            running=_workflows(A) | _workflows(B) | {"task:user_ab:t1"},
            schedules={ts.schedule_workflow_id(A, "s1"), ts.schedule_workflow_id(B, "s1")},
        )
        temporal_client.set_client(fake)
        rooms = {
            "room-a-future": {"ownerId": A, "meeting": {"recallBotId": "bot-a1", "status": "scheduled"}},
            "room-a-live": {"ownerId": A, "meeting": {"recallBotId": "bot-a2", "status": "in_call"}},
            "room-a-over": {"ownerId": A, "meeting": {"recallBotId": "bot-a3", "status": "ended"}},
            # a cancel or a failed move whose leave failed keeps the bot id: the bot is still booked
            "room-a-cancelled": {"ownerId": A, "meeting": {"recallBotId": "bot-a4", "status": "cancelled"}},
            "room-a-failed": {"ownerId": A, "meeting": {"recallBotId": "bot-a5", "status": "bot_failed"}},
            "room-a-chat": {"ownerId": A},
            "room-shared": {"ownerId": B, "meeting": {"recallBotId": "bot-b-shared", "status": "scheduled"}},
            "room-b": {"ownerId": B, "meeting": {"recallBotId": "bot-b1", "status": "scheduled"}},
        }
        owned = {A: [*(r for r in rooms if r.startswith("room-a-")), "room-shared"], B: ["room-b"]}
        leave = AsyncMock()
        connections = AsyncMock(return_value=2)
        with (
            patch.object(account_wipe.messaging_repo, "list_owner_room_ids", AsyncMock(side_effect=owned.get)),
            patch.object(account_wipe.messaging_repo, "get_room", AsyncMock(side_effect=rooms.get)),
            patch("core.voice.meetings.records.meeting_bots.leave", leave),
            patch("core.voice.meetings.records.livekit_huddles.close_room", AsyncMock()),
            patch.object(account_wipe.connection_service, "delete_all_for_user", connections),
        ):
            yield SimpleNamespace(table=table, temporal=fake, leave=leave, connections=connections)
        temporal_client.set_client(None)


@pytest.mark.asyncio
async def test_wipe_engine_state_clears_tasks_schedules_bots(world):
    detail = await account_wipe.wipe_engine_state(A)

    assert not {r for r in _rows(world.table) if A in r[0] or r[0] == ts.agent_owner_pk(f"{A}-ag1")}
    assert set(world.temporal.terminated) == _workflows(A)
    assert ts.schedule_workflow_id(A, "s1") not in world.temporal.schedules
    # every bot still on a meeting row is pulled, a cancelled or failed one too; only an ended meeting's is left
    assert sorted(c.args[0] for c in world.leave.await_args_list) == ["bot-a1", "bot-a2", "bot-a4", "bot-a5"]
    world.connections.assert_awaited_once_with(A)
    assert "terminated 6 workflow(s)" in detail and "deleted 1 schedule(s), 5 tasks row(s)" in detail

    # Idempotent: a second run finds nothing left and does not fail.
    world.leave.reset_mock()
    await account_wipe.wipe_engine_state(A)
    assert world.temporal.terminated.count(ts.router_workflow_id(A)) == 1


@pytest.mark.asyncio
async def test_wipe_engine_state_leaves_other_owner(world):
    await account_wipe.wipe_engine_state(A)

    assert {r for r in _rows(world.table) if B in r[0]} == {
        (ts.ws_pk(B), ts.META_SK),
        (ts.ws_pk(B), ts.task_sk("t1")),
        (ts.ws_pk(B), ts.schedule_sk("s1")),
        (ts.ws_pk(B), ts.agent_sk(f"{B}-ag1")),
        (ts.agent_owner_pk(f"{B}-ag1"), ts.AGENT_OWNER_SK),
    }
    assert _workflows(B) | {"task:user_ab:t1"} <= world.temporal.running  # a longer id sharing A's prefix stays
    assert ts.schedule_workflow_id(B, "s1") in world.temporal.schedules
    bots = {c.args[0] for c in world.leave.await_args_list}
    assert not bots & {"bot-b1", "bot-b-shared"}  # B's room, even one listed in A's catalog, keeps its bot


@pytest.mark.asyncio
async def test_a_bot_recall_refuses_fails_the_step_after_the_rest_ran(world):
    world.leave.side_effect = lambda bot: (_ for _ in ()).throw(RuntimeError("402")) if bot == "bot-a1" else None

    with pytest.raises(RuntimeError, match="bot-a1"):
        await account_wipe.wipe_engine_state(A)

    assert set(world.temporal.terminated) == _workflows(A)
    assert not {r for r in _rows(world.table) if r[0] == ts.ws_pk(A)}


@pytest.mark.asyncio
async def test_both_wipes_run_engine_step(wipe_mocks):  # noqa: F811 -- the imported fixture
    """Both terminal wipes run the step before the runtime goes, and both previews list it."""
    from core.services import workspace_wipe

    preview = await account_wipe.preview_wipe(OWNER_ID)
    report = await account_wipe.wipe_account(OWNER_ID, actor_admin_id="admin_1")
    for steps in (preview.steps, report.steps):
        systems = [s.system for s in steps]
        assert systems.index("engine") < systems.index("runtime")
    assert next(s for s in preview.steps if s.system == "engine").detail == account_wipe.ENGINE_PREVIEW
    wipe_mocks.engine_wipe.assert_awaited_once_with(OWNER_ID)

    order = workspace_wipe.WORKSPACE_STEP_ORDER
    assert order.index(("engine", "wipe_engine_state")) < order.index(("runtime", "delete_workspace_runtime"))
    engine = next(d for d in workspace_wipe.WORKSPACE_STEP_REGISTRY if d.system == "engine")
    assert engine.preview(None) == (True, account_wipe.ENGINE_PREVIEW)

    snap = SimpleNamespace(org_id="org_x", members=[{"user_id": "user_m", "in_clerk": True, "other_orgs": []}])
    with (
        patch.object(workspace_wipe, "wipe_engine_state", AsyncMock(return_value="done")) as step,
        patch.object(workspace_wipe, "deletable_member_ids", lambda s: ["user_m"]),
        patch.object(workspace_wipe.connection_service, "delete_all_for_user", AsyncMock()) as conns,
    ):
        assert await engine.run(snap) == "done"
    step.assert_awaited_once_with("org_x")
    conns.assert_awaited_once_with("user_m")


@pytest.mark.asyncio
async def test_a_temporal_failure_still_reports_the_stuck_bot_ids(world):
    world.leave.side_effect = lambda bot: (_ for _ in ()).throw(RuntimeError("402")) if bot == "bot-a4" else None
    temporal_client.set_client(None)  # Temporal unreachable: delete_tenant raises

    with pytest.raises(RuntimeError, match="bot-a4") as err:
        await account_wipe.wipe_engine_state(A)

    assert "Temporal client is not connected" in str(err.value) and "bot-a4" in str(err.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [
        {"DELETE": 404},  # never dispatched, already deleted
        {"DELETE": 405, "POST": 404},  # dispatched, already gone from the call
    ],
)
async def test_a_bot_already_gone_is_done(monkeypatch, caplog, answers):
    """A re-run of a wipe whose rooms survived meets bots Recall no longer has; that is success, not a stuck bot."""
    import httpx

    from core.voice.meetings import meeting_bots

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(answers.get(request.method, 500))

    monkeypatch.setattr(meeting_bots, "_transport", httpx.MockTransport(handler))
    await meeting_bots.leave("bot-gone")
    assert seen == list(answers)
    assert "bot-gone: Recall has no such bot (404)" in caplog.text  # a wrong Recall base URL would look like this

    monkeypatch.setattr(meeting_bots, "_transport", httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(meeting_bots.RecallError):  # anything else still fails
        await meeting_bots.leave("bot-x")


def _attempts(table, ws: str) -> dict[str, tuple[str, str]]:
    """Three Attempt rows (the fields `begin_attempt` writes that the wipe reads): a live one in its own task
    session, a live one in the workspace session, and an ended one in a task session. Label -> (task id, run id)."""
    from core.services import runtime_transport

    runs = {}
    for label, own, status in (("own", True, "running"), ("ws", False, "running"), ("done", True, "ended")):
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ws}/{label}"))
        run_id = ts.run_id(ws, task_id, 1)
        session = runtime_transport.session_id_for(ws, task_id if own else None)
        table.put_item(
            Item={"pk": ts.ws_pk(ws), "sk": ts.attempt_sk(task_id, 1), "type": "attempt", "taskId": task_id, "n": 1,
                  "runId": run_id, "sessionId": session, "status": status},
        )  # fmt: skip
        runs[label] = (task_id, run_id)
    return runs


@pytest.fixture
def runtime(monkeypatch):
    from core.engine import api as engine_api

    from core.config import settings

    monkeypatch.setattr(settings, "HERMES_SHARED_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:0:runtime/pool")
    container = AsyncMock(return_value={"owner_id": A, "status": "running"})
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(engine_api.container_repo, "get_by_owner_id", container)
    monkeypatch.setattr(engine_api.runtime_runs, "stop_task_session", stop)
    return SimpleNamespace(container=container, stop=stop)


@pytest.mark.asyncio
async def test_open_task_sessions_are_stopped_while_their_rows_name_them(world, runtime):
    runs = _attempts(world.table, A)
    seen_rows = []
    runtime.stop.side_effect = lambda *a: seen_rows.append(len(_rows(world.table))) or True

    detail = await account_wipe.wipe_engine_state(A)

    runtime.stop.assert_awaited_once_with(A, *runs["own"])  # not the workspace-session or ended one
    assert seen_rows and seen_rows[0] > 0  # stopped before the partition went
    assert "stopped 1 task session(s)" in detail
    assert not {r for r in _rows(world.table) if r[0] == ts.ws_pk(A)}


@pytest.mark.asyncio
async def test_an_unconfirmed_task_stop_keeps_the_rows_for_a_rerun(world, runtime):
    runs = _attempts(world.table, A)
    runtime.stop.return_value = False

    with pytest.raises(RuntimeError, match=runs["own"][0]):
        await account_wipe.wipe_engine_state(A)
    assert {r for r in _rows(world.table) if r[0] == ts.ws_pk(A)}  # kept: they name the session

    runtime.stop.return_value = True
    await account_wipe.wipe_engine_state(A)
    assert not {r for r in _rows(world.table) if r[0] == ts.ws_pk(A)}


@pytest.mark.asyncio
async def test_no_runtime_left_means_no_task_session_to_stop(world, runtime):
    _attempts(world.table, A)
    runtime.container.return_value = None  # the runtime step already ran

    await account_wipe.wipe_engine_state(A)

    runtime.stop.assert_not_awaited()
    assert not {r for r in _rows(world.table) if r[0] == ts.ws_pk(A)}


@pytest.mark.asyncio
async def test_the_partition_is_read_strongly_consistent(world, monkeypatch):
    from core.repositories import tasks_repo

    table, reads = world.table, []

    class _Spy:
        def __getattr__(self, name):
            return getattr(table, name)

        def query(self, **kwargs):
            reads.append(kwargs.get("ConsistentRead"))
            return table.query(**kwargs)

    monkeypatch.setattr(tasks_repo, "_table", lambda: _Spy())
    await tasks_repo.list_partition(A)
    assert reads == [True]
