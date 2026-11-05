"""Task Activities against a moto `tasks` table, with fake runtime, Temporal client and posts (O Task 3)."""

import time
import uuid

import pytest
from temporalio import activity
from temporalio.testing import ActivityEnvironment

from core import tasks_schema as ts
from core.repositories import tasks_repo as repo
from core.services import runtime_runs
from core.temporal import activities as acts
from core.temporal.types import AttemptRef, WorkerTurnIn

from .test_tasks_repo import table  # noqa: F401 -- moto tasks table fixture

WS = "org_1"
CEO = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ROOM = str(uuid.uuid4())
ROOT = str(uuid.uuid4())


class FakeHandle:
    def __init__(self, log, wid):
        self.log, self.wid = log, wid

    async def signal(self, name, *args):
        self.log.append((self.wid, name))


class FakeClient:
    def __init__(self):
        self.signals = []

    def get_workflow_handle(self, wid):
        return FakeHandle(self.signals, wid)


@pytest.fixture
def world(table, monkeypatch):  # noqa: F811
    w = {
        "posts": [],
        "pushes": [],
        "usage": [],
        "invites": [],
        "runs": [],
        "steers": [],
        "reactions": [],
        "client": FakeClient(),
        "create": None,
        "gate": {"blocked": False},
        "accepted": True,
    }

    async def post(ws, room_id, **kw):
        w["posts"].append({"room": room_id, **kw})
        return f"m{len(w['posts'])}"

    async def ensure_room(ws, task, **kw):
        return ts.task_room_id(task["taskId"])

    async def front(ws):
        return CEO

    async def roster(ws):
        return [{"id": CEO, "name": "CEO"}]

    async def push(user_id, title, body, data=None, **kw):
        w["pushes"].append(user_id)

    async def usage(ws, user, u, **kw):
        w["usage"].append((user, u.input_tokens, kw))

    async def invite(ws, row, outcome):
        w["invites"].append(row["roomId"])

    async def create_run(ws, identity, request):
        w["runs"].append((identity, request))
        if w["create"]:
            return w["create"]()
        return runtime_runs.RunStatus(identity.run_id, "queued", None, None, None, [])

    async def steer(ws, identity, text, kind):
        w["steers"].append((text, kind))
        return w["accepted"]

    async def gate(**kw):
        return w["gate"]

    async def get_msg(room_id, message_id):
        return {"messageId": message_id, "messageSeq": 7, "text": "use the Amex"}

    async def react(ws, room_id, seq, who, emoji, active, member_kind="human"):
        w["reactions"].append((who, emoji, member_kind))

    monkeypatch.setattr(acts.task_conversation, "post_as_lightbulb", post)
    monkeypatch.setattr(acts.task_conversation, "ensure_task_room", ensure_room)
    monkeypatch.setattr(acts.task_conversation, "front_agent_id", front)
    monkeypatch.setattr(acts.messaging, "load_agent_roster", roster)
    monkeypatch.setattr(acts.messaging, "_record_run_usage", usage)
    monkeypatch.setattr(acts.messaging, "ask_who_should_see", invite)
    monkeypatch.setattr(acts.push_service, "send_push_to_user", push)
    monkeypatch.setattr(acts.runtime_runs, "create_run", create_run)
    monkeypatch.setattr(acts.runtime_runs, "steer_run", steer)
    monkeypatch.setattr(acts.chat_gate, "gate_chat", gate)
    monkeypatch.setattr(acts.messaging_repo, "get_message_by_id", get_msg)
    monkeypatch.setattr(acts.messaging_repo, "set_reaction", react)
    monkeypatch.setattr(acts, "get_client", lambda: w["client"])
    monkeypatch.setattr(acts, "BUSY_PAUSE_S", 0)
    return w


async def _task(status="queued", root=None, **over) -> str:
    tid = ts.new_task_id()
    row = {
        "taskId": tid,
        "identifier": "LB-1",
        "title": "Pay the invoice",
        "description": "Pay Acme $40",
        "status": status if status in ("proposed", "queued") else "queued",
        "audience": {"kind": "room", "roomId": ROOM},
        "lane": "shared",
        "triggerId": "chat:m1",
        "ownerUserId": "sam",
        "specialist": "finance",
    } | over
    await repo.create_task(WS, row)
    await repo.subscribe(WS, tid, f"room:{ROOM}:{root or uuid.uuid4()}", created_by="router")
    return tid


async def _attempt(tid, **fields) -> AttemptRef:
    return await acts.begin_attempt(WS, tid, fields.pop("inputs", []))


async def test_begin_attempt_idempotent(world):
    tid = await _task()
    a = await acts.begin_attempt(WS, tid, ["chat:m2"])
    b = await acts.begin_attempt(WS, tid, ["chat:m2"])
    assert a == b == AttemptRef(1, ts.run_id(WS, tid, 1))
    task = await repo.get_task(WS, tid)
    assert task["status"] == "working" and task["attemptCount"] == 1
    att = await repo.get_attempt(WS, tid, 1)
    assert att["agentId"] == CEO and att["inputTriggerIds"] == ["chat:m2"]  # "finance" is not an agent id


async def test_begin_attempt_recomputes_session_kind(world, monkeypatch):
    kinds = iter(["workspace", "task"])
    monkeypatch.setattr(acts.runtime_runs, "session_kind_for", lambda ws: next(kinds), raising=False)
    tid = await _task()
    await acts.begin_attempt(WS, tid, [])
    await acts.end_attempt(WS, tid, 1, "runtime_lost", "HeartbeatTimeout")
    await acts.begin_attempt(WS, tid, [])
    a1, a2 = await repo.get_attempt(WS, tid, 1), await repo.get_attempt(WS, tid, 2)
    assert "-t-" not in a1["sessionId"] and "-t-" in a2["sessionId"]


async def test_slots_cap_three_and_release_signals_waiter(world):
    ids = [await _task() for _ in range(4)]
    assert [await acts.acquire_slot(WS, t) for t in ids] == [True, True, True, False]
    assert (await repo.get_slots(WS))["waiters"] == [ids[3]]
    await acts.release_slot(WS, ids[0])
    assert world["client"].signals == [(ts.task_workflow_id(WS, ids[3]), "slot_free")]
    assert await acts.acquire_slot(WS, ids[3]) is True


async def test_slot_lease_survives_terminated_workflow(world):
    ids = [await _task() for _ in range(4)]
    for t in ids[:3]:
        await acts.acquire_slot(WS, t)
    row = await repo.get_slots(WS)
    leases = dict(row["leases"]) | {ids[0]: int(time.time()) - 1}  # its workflow died: the lease ran out
    await repo.put_slots(WS, leases, [], expected_version=row["version"])
    assert await acts.acquire_slot(WS, ids[3]) is True


async def test_worker_turn_stores_token_and_goes_async(world):
    tid = await _task()
    ref = await _attempt(tid)
    with pytest.raises(activity._CompleteAsyncError):
        await ActivityEnvironment().run(acts.worker_turn, WorkerTurnIn(WS, tid, 1, ref.run_id))
    att = await repo.get_attempt(WS, tid, 1)
    assert att["taskToken"] and att["status"] == "running"
    identity, request = world["runs"][0]
    assert identity.run_id == ref.run_id and "Pay Acme $40" in request.input


async def test_worker_turn_refused_maps_outcome(world):
    tid = await _task()
    ref = await _attempt(tid)

    def refuse():
        raise runtime_runs.RunRefused("bad")

    world["create"] = refuse
    out = await ActivityEnvironment().run(acts.worker_turn, WorkerTurnIn(WS, tid, 1, ref.run_id))
    assert (out.outcome, out.error) == ("refused", "RunRefused")


async def test_worker_turn_busy_then_lost(world, monkeypatch):
    tid = await _task()
    ref = await _attempt(tid)
    monkeypatch.setattr(acts, "BUSY_RETRY_S", -1)

    def busy():
        raise runtime_runs.RuntimeBusy("503")

    world["create"] = busy
    out = await ActivityEnvironment().run(acts.worker_turn, WorkerTurnIn(WS, tid, 1, ref.run_id))
    assert (out.outcome, out.error) == ("runtime_lost", "RuntimeBusy")


async def _ended(tid, outcome, output="Paid Acme.", usage=None):
    await repo.update_attempt(
        WS, tid, 1, {"output": output, "usage": usage or {}}, allowed_status=frozenset({"dispatched"})
    )
    await acts.end_attempt(WS, tid, 1, outcome, None if outcome == "completed" else "RunFailed")


async def test_end_attempt_posts_result_as_lightbulb(world):
    tid = await _task(root=ROOT)
    await _attempt(tid)
    await acts.acquire_slot(WS, tid)
    await _ended(tid, "completed")
    origin, room = world["posts"]
    assert origin["room"] == ROOM and origin["thread_root"] == ROOT and origin["specialist"] == "finance"
    assert origin["kind"] == "result" and origin["task_refs"] == [{"taskId": tid, "role": "started"}]
    assert room["room"] == ts.task_room_id(tid) and room["text"] == "Paid Acme."
    assert (await repo.get_task(WS, tid))["status"] == "idle"
    assert tid not in (await repo.get_slots(WS))["leases"]
    assert world["client"].signals == []  # no Router turn per result


async def test_end_attempt_failure_pushes_requester_only(world):
    tid = await _task()
    await _attempt(tid)
    await _ended(tid, "failed")
    assert world["pushes"] == ["sam"] and world["posts"] == []
    assert (await repo.get_task(WS, tid))["status"] == "failed"


async def test_end_attempt_records_usage_for_requester(world):
    tid = await _task()
    await _attempt(tid)
    await _ended(tid, "completed", usage={"inputTokens": 120, "outputTokens": 30, "costUsd": 0.02})
    await acts.end_attempt(WS, tid, 1, "completed", None)  # a retried end_attempt bills nothing more
    assert [(u, n) for u, n, _ in world["usage"]] == [("sam", 120)]
    assert world["usage"][0][2] == {"room_turn": False, "event_kind": "task"}


async def test_end_attempt_invite_ask_once_per_workspace(world):
    tid = await _task()
    await _attempt(tid)
    await _ended(tid, "completed")
    assert world["invites"] == [ROOM]  # ask_who_should_see keeps its own once-per-workspace marker


async def test_end_attempt_stopped_resolves(world):
    tid = await _task()
    await _attempt(tid)
    await _ended(tid, "cancelled")
    task = await repo.get_task(WS, tid)
    assert (task["status"], task["statusReason"]) == ("resolved", "stopped")


async def test_check_budget_between_attempts(world):
    tid = await _task(budgetUsd=0.05)
    assert await acts.check_budget(WS, tid) is True
    await _attempt(tid)
    await _ended(tid, "completed", usage={"costUsd": 0.06})
    assert await acts.check_budget(WS, tid) is False
    task = await repo.get_task(WS, tid)
    assert (task["status"], task["statusReason"]) == ("waiting", "budget")


async def _input(tid, actor="priya") -> str:
    trig = ts.trigger_id("chat", "m9")
    await repo.put_trigger(
        WS,
        {
            "triggerId": trig,
            "source": "chat",
            "ref": {"roomId": ROOM, "messageId": "m9"},
            "admission": "respond",
            "lane": "shared",
            "actorUserId": actor,
            "receivedAt": "2026-11-04T10:00:00Z",
        },
    )
    return trig


async def test_deliver_input_reacts_eyes(world):
    tid = await _task()
    await _attempt(tid)
    assert await acts.deliver_input(WS, tid, 1, await _input(tid)) is True
    assert world["steers"] == [("use the Amex", "input")]
    assert world["reactions"] == [(CEO, "👀", "agent")]


async def test_deliver_input_not_accepted_stays_pending(world):
    world["accepted"] = False
    tid = await _task()
    await _attempt(tid)
    assert await acts.deliver_input(WS, tid, 1, await _input(tid)) is False
    assert world["reactions"] == []


async def test_deliver_input_gate_chat_refused(world):
    world["gate"] = {"blocked": True, "code": "subscription_inactive", "message": "Your plan is inactive."}
    tid = await _task()
    await _attempt(tid)
    assert await acts.deliver_input(WS, tid, 1, await _input(tid)) is True  # consumed, not delivered
    assert world["steers"] == [] and world["posts"][0]["text"] == "Your plan is inactive."


async def test_transition_same_status_is_noop(world):
    tid = await _task()
    await acts.transition(WS, tid, "resolved", "dismissed")
    await acts.transition(WS, tid, "resolved", "quiet")
    assert (await repo.get_task(WS, tid))["statusReason"] == "dismissed"


async def test_begin_attempt_proposed_waits_for_start(world):
    tid = await _task(status="proposed")
    assert await acts.begin_attempt(WS, tid, ["chat:m2"]) == AttemptRef(0, "")
    assert (await repo.get_task(WS, tid))["status"] == "proposed"
    assert await acts.begin_attempt(WS, tid, ["chat:m2"], True) == AttemptRef(1, ts.run_id(WS, tid, 1))
    assert (await repo.get_task(WS, tid))["status"] == "working"


async def test_bridge_usage_report_skips_task_runs(monkeypatch):
    """One usage writer for a Worker turn: end_attempt (requester), never the bridge's usage callback too."""
    recorded = []

    async def record(*a, **k):
        recorded.append(a)

    async def claim(*a):
        return True

    monkeypatch.setattr(acts.messaging, "_record_run_usage", record)
    monkeypatch.setattr(acts.messaging.messaging_repo, "claim_usage_receipt", claim)
    monkeypatch.setattr(acts.messaging.run_analytics, "schedule_run_completed", lambda **k: None)
    usage = {"model": "m", "inputTokens": 5, "outputTokens": 1, "cacheReadTokens": 0, "cacheWriteTokens": 0}
    for key, expected in ((ts.worker_session_key(CEO, ts.new_task_id()), 0), ("agent:x:main", 1)):
        body = acts.messaging.UsageReportRequest.model_validate(
            {"sessionKey": key, "agentId": CEO, "runId": str(uuid.uuid4()), "success": True, "usage": usage}
        )
        await acts.messaging.handle_usage_report(WS, body)
        assert len(recorded) == expected


async def test_begin_attempt_retry_completes_the_move(world, monkeypatch):
    """put_attempt landed, the move to working failed: the retry finishes it, so end_attempt can run."""
    tid = await _task()
    real = acts._move
    fails = iter([True])

    async def flaky(ws, task_id, to, reason=None, fields=None):
        if to == "working" and next(fails, False):
            raise repo.VersionConflict("lost race")
        return await real(ws, task_id, to, reason, fields)

    monkeypatch.setattr(acts, "_move", flaky)
    with pytest.raises(repo.VersionConflict):
        await acts.begin_attempt(WS, tid, [])
    assert await acts.begin_attempt(WS, tid, []) == AttemptRef(1, ts.run_id(WS, tid, 1))
    assert (await repo.get_task(WS, tid))["status"] == "working"
    await _ended(tid, "completed")
    assert (await repo.get_task(WS, tid))["status"] == "idle"


async def test_attempt_gets_a_run_secret_on_the_wire(world):
    tid = await _task()
    await _attempt(tid)
    att = await repo.get_attempt(WS, tid, 1)
    assert len(att["runSecret"]) >= 32
    assert acts._identity(WS, tid, att).to_wire()["runSecret"] == att["runSecret"]
