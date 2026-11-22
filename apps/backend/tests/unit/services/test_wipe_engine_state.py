"""The terminal wipe's engine step (`account_wipe.wipe_engine_state`): meeting bots, Temporal and the tasks table.

moto `tasks` table; a fake Temporal client records terminations and Schedule deletes; a recorder stands in for
Recall. Two owners share every store, so each test also proves the other owner's state is untouched."""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import boto3
import httpx
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
        gone = AsyncMock(return_value=True)  # Recall: the ended meeting's bot is done
        connections = AsyncMock(return_value=2)
        with (
            patch.object(account_wipe.messaging_repo, "list_owner_room_ids", AsyncMock(side_effect=owned.get)),
            patch.object(account_wipe.messaging_repo, "get_room", AsyncMock(side_effect=rooms.get)),
            patch("core.voice.meetings.records.meeting_bots.leave", leave),
            patch("core.voice.meetings.records.meeting_bots.gone", gone),
            patch("core.voice.meetings.records.livekit_huddles.close_room", AsyncMock()),
            patch.object(account_wipe.connection_service, "delete_all_for_user", connections),
        ):
            yield SimpleNamespace(table=table, temporal=fake, leave=leave, gone=gone, connections=connections)
        temporal_client.set_client(None)


@pytest.mark.asyncio
async def test_wipe_engine_state_clears_tasks_schedules_bots(world):
    detail = await account_wipe.wipe_engine_state(A)

    assert not {r for r in _rows(world.table) if A in r[0] or r[0] == ts.agent_owner_pk(f"{A}-ag1")}
    assert set(world.temporal.terminated) == _workflows(A)
    assert ts.schedule_workflow_id(A, "s1") not in world.temporal.schedules
    # every bot still on a meeting row is pulled, a cancelled or failed one too; an ended meeting's only if Recall
    # says it is not done (asked once)
    assert sorted(c.args[0] for c in world.leave.await_args_list) == ["bot-a1", "bot-a2", "bot-a4", "bot-a5"]
    world.gone.assert_awaited_once_with("bot-a3")
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


POOL = "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/pool"


@pytest.fixture
def runtime(monkeypatch):
    """The stop door (`hermes_provisioning.stop_runtime_session`) as a recorder, the shared pool configured, no
    session-ledger rows, and no containers row read at all: the stop must not need one."""
    from core.config import settings
    from core.engine import api as engine_api

    monkeypatch.setattr(settings, "HERMES_SHARED_RUNTIME_ARN", POOL)
    stop = AsyncMock(return_value=True)
    ledger: dict[str, dict] = {}
    monkeypatch.setattr(engine_api.hermes_provisioning, "stop_runtime_session", stop)
    monkeypatch.setattr(
        engine_api.container_repo, "get_session_sync", lambda o, *, task_id=None: ledger.get(task_id, {})
    )
    monkeypatch.setattr(engine_api.container_repo, "get_by_owner_id", AsyncMock(side_effect=AssertionError("no row")))
    return SimpleNamespace(stop=stop, ledger=ledger)


def _a_rows(table) -> set:
    return {r for r in _rows(table) if r[0] == ts.ws_pk(A)}


@pytest.mark.asyncio
async def test_open_task_sessions_are_stopped_while_their_rows_name_them(world, runtime):
    runs = _attempts(world.table, A)
    runtime.ledger[runs["own"][0]] = {"arn": "arn:legacy-writer"}  # the ledger's writer wins over the pool
    seen_rows = []
    runtime.stop.side_effect = lambda *a, **k: seen_rows.append(len(_a_rows(world.table))) or True

    detail = await account_wipe.wipe_engine_state(A)

    # only the live attempt in its own task session; not the workspace-session one, not the ended one
    runtime.stop.assert_awaited_once_with(A, "arn:legacy-writer", task_id=runs["own"][0])
    assert seen_rows[0] > 0  # stopped before the partition went
    assert "stopped 1 task session(s)" in detail and not _a_rows(world.table)


@pytest.mark.asyncio
async def test_a_failed_stop_survives_the_runtime_teardown_and_a_rerun_stops_it(world, runtime):
    """The step fails, the wipe's runtime step still runs and deletes the containers row (never read here), and the
    re-run stops the session from its row and the pool before deleting anything."""
    runs = _attempts(world.table, A)
    runtime.stop.return_value = False

    with pytest.raises(RuntimeError, match=runs["own"][0]):
        await account_wipe.wipe_engine_state(A)
    assert _a_rows(world.table)  # kept: they name the session

    runtime.stop.reset_mock()
    runtime.stop.return_value = True
    await account_wipe.wipe_engine_state(A)
    runtime.stop.assert_awaited_once_with(A, POOL, task_id=runs["own"][0])
    assert not _a_rows(world.table)


@pytest.mark.asyncio
async def test_an_attempt_written_after_the_first_read_is_stopped(world, runtime):
    from core.services import runtime_transport

    _attempts(world.table, A)
    late = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{A}/late"))

    def begin_attempt_lands(*a, **k):  # a terminated workflow's in-flight begin_attempt writes after the read
        if k["task_id"] != late:
            world.table.put_item(
                Item={"pk": ts.ws_pk(A), "sk": ts.attempt_sk(late, 1), "type": "attempt", "taskId": late, "n": 1,
                      "runId": "r", "sessionId": runtime_transport.session_id_for(A, late), "status": "dispatched"},
            )  # fmt: skip
        return True

    runtime.stop.side_effect = begin_attempt_lands
    await account_wipe.wipe_engine_state(A)

    assert late in {c.kwargs["task_id"] for c in runtime.stop.await_args_list}
    assert not _a_rows(world.table)


@pytest.mark.asyncio
async def test_no_runtime_arn_with_open_task_sessions_fails_and_keeps_the_rows(world, runtime, monkeypatch):
    from core.config import settings

    runs = _attempts(world.table, A)
    monkeypatch.setattr(settings, "HERMES_SHARED_RUNTIME_ARN", "")

    with pytest.raises(RuntimeError, match=runs["own"][0]):
        await account_wipe.wipe_engine_state(A)
    runtime.stop.assert_not_awaited()
    assert _a_rows(world.table)


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


@pytest.mark.asyncio
async def test_an_ended_meeting_whose_bot_is_still_in_the_call_is_pulled(world):
    """The idle leave marks the meeting ended before pulling the bot; when that pull failed the bot is still there."""
    world.gone.return_value = False

    await account_wipe.wipe_engine_state(A)

    assert "bot-a3" in {c.args[0] for c in world.leave.await_args_list}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer, done",
    [
        (httpx.Response(404), True),
        (httpx.Response(200, json={"status_changes": [{"code": "in_call_recording"}, {"code": "done"}]}), True),
        (httpx.Response(200, json={"status_changes": [{"code": "in_call_recording"}]}), False),
    ],
)
async def test_gone_asks_recall(monkeypatch, answer, done):
    from core.voice.meetings import meeting_bots

    monkeypatch.setattr(meeting_bots, "_transport", httpx.MockTransport(lambda r: answer))
    assert await meeting_bots.gone("bot-x") is done


@pytest.mark.asyncio
async def test_a_later_attempt_of_a_task_already_stopped_is_stopped_too(world, runtime):
    """Pass 1 stops attempt 1; attempt 2 of the same task lands meanwhile, under the same session id."""
    runs = _attempts(world.table, A)
    task_id = runs["own"][0]

    def attempt_two_lands(*a, **k):
        item = dict(world.table.get_item(Key={"pk": ts.ws_pk(A), "sk": ts.attempt_sk(task_id, 1)})["Item"])
        world.table.put_item(Item={**item, "sk": ts.attempt_sk(task_id, 2), "n": 2, "runId": "r2"})
        return True

    runtime.stop.side_effect = attempt_two_lands
    await account_wipe.wipe_engine_state(A)

    assert [c.kwargs["task_id"] for c in runtime.stop.await_args_list] == [task_id, task_id]
    assert not _a_rows(world.table)


@pytest.mark.asyncio
async def test_the_meet_room_closes_even_when_recall_does_not_answer():
    from core.voice.meetings import records

    close = AsyncMock()
    with (
        patch.object(records.meeting_bots, "gone", AsyncMock(side_effect=RuntimeError("timeout"))),
        patch.object(records.livekit_huddles, "close_room", close),
        pytest.raises(RuntimeError, match="timeout"),  # still the caller's failure (the wipe names the bot)
    ):
        await records.end_meeting("room-1", {"recallBotId": "bot-1", "status": "ended"})
    close.assert_awaited_once_with("room-1", meet=True)


@pytest.mark.asyncio
async def test_wipe_runs_the_engine_delete_in_process_over_http_transport(world, monkeypatch):
    """With ENGINE_TRANSPORT=http the wipe still deletes the tenant in this process (`core.engine.api`), never over
    the engine service's DELETE: that call can outlast the internal ALB's 300 s idle timeout and come back a 504."""
    from core.config import settings
    from core.engine import client

    monkeypatch.setattr(settings, "ENGINE_TRANSPORT", "http")
    over_http = AsyncMock(side_effect=AssertionError("the wipe sent the tenant delete over HTTP"))
    monkeypatch.setattr(client, "delete_tenant", over_http)
    detail = await account_wipe.wipe_engine_state(A)
    over_http.assert_not_awaited()
    assert set(world.temporal.terminated) == _workflows(A) and "terminated 6 workflow(s)" in detail
