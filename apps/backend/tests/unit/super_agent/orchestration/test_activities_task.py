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
        "marks": [],  # lifecycle markers in the task room (UX slice 3), kept apart from the posts people read
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
        if kw.get("kind") == "marker":
            w["marks"].append((room_id, kw["text"], kw["event_id"]))
            return "mark"
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


async def _task(root=None, **over) -> str:
    tid = ts.new_task_id()
    row = {
        "taskId": tid,
        "identifier": "LB-1",
        "title": "Pay the invoice",
        "description": "Pay Acme $40",
        "status": "queued",
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

    async def session_kind_for(ws):  # V3's is async (switch + custody gate)
        return next(kinds)

    monkeypatch.setattr(acts.runtime_runs, "session_kind_for", session_kind_for, raising=False)
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
    assert request.effects == "required" and request.to_wire()["effects"] == "required"  # Lane Q: the checkpoint is on
    assert identity.run_id == ref.run_id and "Pay Acme $40" in request.input


async def test_worker_turn_casts_its_browser_to_the_origin_room(world, monkeypatch):
    # The requester's room gets the run's live browser frames (Lightbulb page, task pane), addressed by the runId the
    # task's attempts now carry.
    async def get_room(room_id):
        return {"roomId": room_id, "deliveryKey": "dk1", "workspaceId": "ws_1"} if room_id == ROOM else None

    monkeypatch.setattr(acts.messaging_repo, "get_room", get_room)
    monkeypatch.setattr(acts.settings, "APPSYNC_EVENTS_HTTP_ENDPOINT", "https://events.example")
    tid = await _task()
    ref = await _attempt(tid)
    with pytest.raises(activity._CompleteAsyncError):
        await ActivityEnvironment().run(acts.worker_turn, WorkerTurnIn(WS, tid, 1, ref.run_id))
    identity, _ = world["runs"][0]
    assert identity.to_wire()["frames"] == {
        "endpoint": "https://events.example",
        "channel": f"/ws/ws_1/rooms/{ROOM}/dk1",
        "roomId": ROOM,
    }


@pytest.mark.parametrize(("members", "cast"), [(["sam", "guest"], False), (["sam"], True)])
async def test_a_person_only_tasks_browser_never_reaches_a_room_with_anyone_else(world, monkeypatch, members, cast):
    # The room's channel reaches every member (a shared room's guests too): a person-only task casts only where its
    # people are everyone (its own DM), as `_announce` posts its line.
    async def get_room(room_id):
        return {"roomId": room_id, "deliveryKey": "dk1", "workspaceId": "ws_1"}

    async def list_members(room_id):
        return [{"kind": "human", "id": m} for m in members] + [{"kind": "agent", "id": CEO}]

    monkeypatch.setattr(acts.messaging_repo, "get_room", get_room)
    monkeypatch.setattr(acts.messaging_repo, "list_members", list_members)
    monkeypatch.setattr(acts.settings, "APPSYNC_EVENTS_HTTP_ENDPOINT", "https://events.example")
    tid = await _task(audience={"kind": "person", "userIds": ["sam"], "roomId": ROOM})
    ref = await _attempt(tid)
    with pytest.raises(activity._CompleteAsyncError):
        await ActivityEnvironment().run(acts.worker_turn, WorkerTurnIn(WS, tid, 1, ref.run_id))
    wire = world["runs"][0][0].to_wire()
    assert ("frames" in wire) is cast
    if cast:
        assert wire["frames"]["channel"] == f"/ws/ws_1/rooms/{ROOM}/dk1"


async def test_worker_turn_without_a_room_channel_casts_nowhere(world, monkeypatch):
    async def get_room(room_id):
        raise RuntimeError("table down")

    monkeypatch.setattr(acts.messaging_repo, "get_room", get_room)
    monkeypatch.setattr(acts.settings, "APPSYNC_EVENTS_HTTP_ENDPOINT", "https://events.example")
    tid = await _task()
    ref = await _attempt(tid)
    with pytest.raises(activity._CompleteAsyncError):
        await ActivityEnvironment().run(acts.worker_turn, WorkerTurnIn(WS, tid, 1, ref.run_id))
    assert "frames" not in world["runs"][0][0].to_wire()


async def test_worker_brief_carries_the_requesters_local_time(world, monkeypatch):
    async def tz(user_id):
        assert user_id == "sam"  # the task's requester, not the runtime's UTC clock
        return "America/New_York"

    monkeypatch.setattr("core.repositories.user_repo.get_timezone", tz)
    tid = await _task()
    ref = await _attempt(tid)
    with pytest.raises(activity._CompleteAsyncError):
        await ActivityEnvironment().run(acts.worker_turn, WorkerTurnIn(WS, tid, 1, ref.run_id))
    assert "New York time (America/New_York) (the requester's own time" in world["runs"][0][1].input


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
    room, origin = world["posts"]  # the task room first: it adopts any files
    # UX slice 3 / walk 10: the main chat gets one new plain line with the task as its chip (an unread).
    assert origin["room"] == ROOM and "thread_root" not in origin and origin["text"] == "Paid Acme."
    assert origin["kind"] == "task_line" and origin["task_refs"] == [{"taskId": tid, "role": "started"}]
    assert room["room"] == ts.task_room_id(tid) and room["text"] == "Paid Acme." and room["kind"] == "result"
    assert [m[1] for m in world["marks"]] == ["Started", "Done"]
    await acts.end_attempt(WS, tid, 1, "completed", None)  # a retried end_attempt posts nothing new
    assert len({p["event_id"] for p in world["posts"]}) == 2 and len({m[2] for m in world["marks"]}) == 2
    assert (await repo.get_task(WS, tid))["status"] == "idle"
    assert tid not in (await repo.get_slots(WS))["leases"]
    assert world["client"].signals == []  # no Router turn per result


async def test_end_attempt_with_a_room_name_for_origin_still_ends(world):
    """Gate run 3: an automation run delivering to `room:acc-a1-e3ffa8` (a name, not an id) failed end_attempt
    on the post, so the task workflow failed and the automation's run claim was never freed."""
    tid = ts.new_task_id()
    await repo.create_task(
        WS,
        {
            "taskId": tid,
            "identifier": "LB-2",
            "title": "Weekly signups",
            "description": "d",
            "status": "queued",
            "audience": {"kind": "room", "roomId": "acc-a1-e3ffa8"},
            "lane": "shared",
            "triggerId": "manual:s1:f1",
            "ownerUserId": "sam",
        },
    )
    await repo.subscribe(WS, tid, "room:acc-a1-e3ffa8", created_by="router")
    await _attempt(tid)
    await acts.acquire_slot(WS, tid)
    await _ended(tid, "completed")
    assert [p["room"] for p in world["posts"]] == [ts.task_room_id(tid)]  # the task room has the result
    assert world["invites"] == []
    assert (await repo.get_task(WS, tid))["status"] == "idle"


async def test_end_attempt_ends_when_the_origin_room_refuses(world, monkeypatch):
    """A deleted or left origin room: the task room has the result and the attempt still ends."""
    tid = await _task(root=ROOT)
    await _attempt(tid)
    await acts.acquire_slot(WS, tid)
    real = acts.task_conversation.post_as_lightbulb

    async def gone(ws, room, **kw):
        if room == ROOM:
            raise acts.messaging.ApiProblem("ROOM_NOT_FOUND", "Room not found.")
        return await real(ws, room, **kw)

    monkeypatch.setattr(acts.task_conversation, "post_as_lightbulb", gone)
    await _ended(tid, "completed")
    assert [p["room"] for p in world["posts"]] == [ts.task_room_id(tid)]
    assert (await repo.get_task(WS, tid))["status"] == "idle"


async def test_end_attempt_result_files_go_to_the_task_room_post(world):
    """Live dev bug (5 Nov): a Worker's file reached the room as a raw MEDIA: path. The task VM PUT it to the
    task room's key; the task room post adopts it, the origin post names it."""
    tid = await _task(root=ROOT)
    await _attempt(tid)
    files = [{"attachmentId": str(uuid.uuid4()), "fileName": "agenda.docx", "contentType": "application/pdf"}]
    await repo.update_attempt(WS, tid, 1, {"attachments": files}, allowed_status=frozenset({"dispatched"}))
    await _ended(tid, "completed", output="The agenda is ready.")
    room, origin = world["posts"]  # task room first: the files live in the task, the main chat gets one line
    assert room["room"] == ts.task_room_id(tid) and room["uploaded_attachments"] == files
    assert origin.get("uploaded_attachments") is None and origin["text"] == "The agenda is ready."
    # UX slice 5: the file stays on the task as what it made, openable from the task room
    (out,) = (await repo.get_task(WS, tid))["outputs"]
    assert (out["id"], out["kind"], out["ref"]["roomId"]) == (
        f"file:{files[0]['attachmentId']}",
        "document",
        ts.task_room_id(tid),
    )


async def test_a_refused_adoption_posts_the_result_without_files_or_note(world, monkeypatch):
    from core.services import messaging

    tid = await _task(root=ROOT)
    await _attempt(tid)
    files = [{"attachmentId": str(uuid.uuid4()), "fileName": "agenda.docx", "contentType": "application/pdf"}]
    await repo.update_attempt(WS, tid, 1, {"attachments": files}, allowed_status=frozenset({"dispatched"}))
    real = acts.task_conversation.post_as_lightbulb

    async def refuse_files(ws, room_id, **kw):
        if kw.get("uploaded_attachments"):
            raise messaging.ApiProblem("INVALID_REQUEST", "uploaded attachment not found")
        return await real(ws, room_id, **kw)

    monkeypatch.setattr(acts.task_conversation, "post_as_lightbulb", refuse_files)
    await _ended(tid, "completed", output="The agenda is ready.")
    room, origin = world["posts"]
    assert room["room"] == ts.task_room_id(tid) and not room.get("uploaded_attachments")
    assert origin["text"] == "The agenda is ready."
    assert not (await repo.get_task(WS, tid)).get("outputs")


async def test_end_attempt_failure_pushes_requester_only(world):
    tid = await _task()
    await _attempt(tid)
    await _ended(tid, "failed")
    (line,) = world["posts"]  # the main chat says it didn't finish, with the task (and its Retry) as the chip
    assert world["pushes"] == ["sam"] and line["kind"] == "task_line" and line["text"].startswith("Didn't finish:")
    assert world["marks"][-1][1] == "Didn't finish"
    assert (await repo.get_task(WS, tid))["status"] == "failed"


async def test_end_attempt_records_usage_for_requester(world):
    tid = await _task()
    await _attempt(tid)
    await _ended(tid, "completed", usage={"inputTokens": 120, "outputTokens": 30, "costUsd": 0.02})
    await acts.end_attempt(WS, tid, 1, "completed", None)  # a retried end_attempt bills nothing more
    assert [(u, n) for u, n, _ in world["usage"]] == [("sam", 120)]
    assert world["usage"][0][2] == {"room_turn": False, "event_kind": "task"}


async def test_end_attempt_with_no_requester_meters_the_workspace(world):
    tid = await _task(ownerUserId=None)
    await _attempt(tid)
    await _ended(tid, "failed", usage={"inputTokens": 70, "outputTokens": 3})
    assert [(u, n) for u, n, _ in world["usage"]] == [(WS, 70)]
    assert world["pushes"] == []  # nobody to tell: pushes stay requester-only


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


async def test_ending_a_task_lets_go_of_its_threads(world):
    """Audit #4: resolved/failed ⇒ THREAD# inactive, so later replies go to the Router; a retried move too."""
    root = str(uuid.uuid4())
    key = f"room:{ROOM}:{root}"
    tid = await _task(root=root)
    await _attempt(tid)
    await _ended(tid, "cancelled")
    assert await repo.task_for_thread(WS, key) is None
    assert [s["active"] for s in (await repo.get_task_tree(WS, tid))["subscriptions"]] == [False]
    await acts.transition(WS, tid, "resolved", "stopped")  # already there: still a no-op, no error
    other = await _task()
    await repo.subscribe(WS, other, key, created_by="router")  # the thread can move to a new task
    assert await repo.task_for_thread(WS, key) == other
    failed = await _task(root=str(uuid.uuid4()))
    await _attempt(failed)
    await _ended(failed, "failed")
    assert not any(s["active"] for s in (await repo.get_task_tree(WS, failed))["subscriptions"])


async def test_retry_and_reopen_take_their_threads_back(world):
    """Leaving resolved/failed resubscribes the task's Router threads, unless another task claimed one meanwhile."""
    root = str(uuid.uuid4())
    key = f"room:{ROOM}:{root}"
    tid = await _task(root=root)
    await _attempt(tid)
    await _ended(tid, "failed")
    assert await repo.task_for_thread(WS, key) is None
    await _attempt(tid)  # retry: begin_attempt moves failed → queued → working
    assert await repo.task_for_thread(WS, key) == tid
    await repo.update_attempt(WS, tid, 2, {"output": "x"}, allowed_status=frozenset({"dispatched"}))
    await acts.end_attempt(WS, tid, 2, "cancelled", None)  # resolved, stopped
    assert await repo.task_for_thread(WS, key) is None
    other = await _task()
    await repo.subscribe(WS, other, key, created_by="router")  # a new task took the thread
    await acts.transition(WS, tid, "queued", None)  # reopen
    assert await repo.task_for_thread(WS, key) == other  # never stolen back


async def test_reopen_to_idle_takes_its_thread_back(world):
    """Reopen moves a closed task straight to idle (Done, no attempt) and a reply in its thread steers it again."""
    root = str(uuid.uuid4())
    key = f"room:{ROOM}:{root}"
    tid = await _task(root=root)
    await _attempt(tid)
    await _ended(tid, "completed")
    await acts.transition(WS, tid, "resolved", "resolved")  # Close
    assert await repo.task_for_thread(WS, key) is None
    await acts.transition(WS, tid, "idle", "reopened")
    task = await repo.get_task(WS, tid)
    assert (task["status"], task["statusReason"]) == ("idle", "reopened")
    assert await repo.task_for_thread(WS, key) == tid


async def test_check_budget_between_attempts(world):
    tid = await _task(budgetUsd=0.05)
    assert await acts.check_budget(WS, tid) is True
    await _attempt(tid)
    await _ended(tid, "completed", usage={"costUsd": 0.06})
    assert await acts.check_budget(WS, tid) is False
    task = await repo.get_task(WS, tid)
    assert (task["status"], task["statusReason"]) == ("waiting", "budget")


async def test_check_budget_on_a_retried_failed_task(world):
    """Audit #14: failed → waiting was illegal, so check_budget raised and failed the workflow run."""
    tid = await _task(budgetUsd=0.05)
    await _attempt(tid)
    await _ended(tid, "failed", usage={"costUsd": 0.06})
    assert (await repo.get_task(WS, tid))["status"] == "failed"
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


async def test_begin_attempt_legacy_proposed_never_runs(world):
    """A row stored `proposed` before 5 Nov 2026 (proposals removed) never runs on a thread reply."""
    tid = await _task()
    repo._table().update_item(  # noqa: SLF001 -- no repo writer makes a proposed row any more
        Key={"pk": ts.ws_pk(WS), "sk": (await repo.get_task(WS, tid))["sk"]},
        UpdateExpression="SET #s = :p",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":p": "proposed"},
    )
    assert await acts.begin_attempt(WS, tid, ["chat:m2"]) == AttemptRef(0, "")
    assert (await repo.get_task(WS, tid))["status"] == "proposed"


async def test_bridge_usage_report_skips_task_runs(monkeypatch):
    """One usage writer for a Worker or Router turn: the activity (end_attempt, router_turn), never the bridge's usage
    callback too."""
    recorded = []

    async def record(*a, **k):
        recorded.append(a)

    async def claim(*a):
        return True

    monkeypatch.setattr(acts.messaging, "_record_run_usage", record)
    monkeypatch.setattr(acts.messaging.messaging_repo, "claim_usage_receipt", claim)
    monkeypatch.setattr(acts.messaging.run_analytics, "schedule_run_completed", lambda **k: None)
    usage = {"model": "m", "inputTokens": 5, "outputTokens": 1, "cacheReadTokens": 0, "cacheWriteTokens": 0}
    router = ts.router_session_key(CEO, "shared", ts.router_room_key(ROOM))
    for key, expected in ((ts.worker_session_key(CEO, ts.new_task_id()), 0), (router, 0), ("agent:x:main", 1)):
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


async def test_reopen_after_close_asks_again(world):
    """Review #4878: an approval given before Close is used up when the task closes, so a Reopen's repeated call
    asks again (`questions.gate` → needs_approval), never `proceed` on the old answer. A used approval stays used."""
    from core.repositories import effects_repo
    from core.services import questions

    tid = await _task()
    await _attempt(tid)

    async def approved(n):
        idem = f"{tid}:1:c{n}"
        await effects_repo.put_effect(
            WS,
            {
                "idemKey": idem,
                "taskId": tid,
                "attempt": 1,
                "kind": "purchase",
                "tool": "request_purchase",
                "operands": {"n": n},
                "operandsHash": f"h{n}",
                "state": "proposed",
            },
        )
        await effects_repo.set_effect_state(WS, idem, to="approved")
        return idem

    await approved(1)  # approved, never used
    used = await approved(2)
    assert await effects_repo.use_approval(
        WS,
        used,
        "h2",
        new_effect={
            "idemKey": f"{tid}:1:d2",
            "taskId": tid,
            "attempt": 1,
            "kind": "purchase",
            "tool": "request_purchase",
            "operands": {"n": 2},
            "operandsHash": "h2",
        },
    )
    retry = {"idemKey": f"{tid}:2:c1", "taskId": tid, "attempt": 2, "kind": "purchase", "operandsHash": "h1"}
    assert questions.gate(retry, await effects_repo.effects_for(WS, tid, "h1")).decision == "proceed"
    await acts.end_attempt(WS, tid, 1, "cancelled", None)  # Stop/Close: resolved
    await acts.transition(WS, tid, "resolved", "resolved")  # a retried transition is a no-op
    assert questions.gate(retry, await effects_repo.effects_for(WS, tid, "h1")).decision == "needs_approval"
    uses = [e for e in await effects_repo.effects_for(WS, tid, "h1") if e.get("type") == "effect_use"]
    assert [u["usedBy"] for u in uses] == ["closed"]
    assert [e["usedBy"] for e in await effects_repo.effects_for(WS, tid, "h2") if e.get("type") == "effect_use"] == [
        f"{tid}:1:d2"
    ]


async def test_an_automation_room_keeps_the_whole_result(world):
    """No Router thread (an automation delivers to its room): the result itself is the room's post, no one-liner."""
    tid = ts.new_task_id()
    await repo.create_task(
        WS,
        {
            "taskId": tid,
            "identifier": "LB-3",
            "title": "Weekly signups",
            "description": "d",
            "status": "queued",
            "audience": {"kind": "room", "roomId": ROOM},
            "lane": "shared",
            "triggerId": "manual:s2:f2",
            "ownerUserId": "sam",
        },
    )
    await _attempt(tid)
    await _ended(tid, "completed", output="41 signups this week.")
    room, origin = world["posts"]
    assert origin["room"] == ROOM and origin["kind"] == "result" and origin["text"] == "41 signups this week."


async def test_reopen_and_person_closes_post_no_workflow_marker(world):
    """A person's Close, Stop and Reopen are marked by the route with their name; the workflow's move adds none."""
    tid = await _task()
    await _attempt(tid)
    await _ended(tid, "completed")
    marks = len(world["marks"])
    await acts.transition(WS, tid, "resolved", "resolved")
    await acts.transition(WS, tid, "idle", "reopened")
    assert len(world["marks"]) == marks
    await acts.transition(WS, tid, "resolved", "quiet")  # nobody closed it: the workflow says Closed
    assert world["marks"][-1][1] == "Closed"


async def test_a_person_only_task_says_nothing_in_the_shared_room(world):
    """Review #4901: a person-only task from a shared room never puts its result (or itself) in that room's chat."""
    tid = await _task(audience={"kind": "person", "userIds": ["sam"]})
    await _attempt(tid)
    await _ended(tid, "completed", output="Sam's salary is 90k.")
    assert [p["room"] for p in world["posts"]] == [ts.task_room_id(tid)]
    assert not any("90k" in p["text"] for p in world["posts"] if p["room"] == ROOM)


async def test_a_workspace_task_carries_its_result_line(world):
    tid = await _task(audience={"kind": "workspace"})
    await _attempt(tid)
    long = '**"Fast," says Linear.** More below.\n\n' + "- detail\n" * 200  # past SHORT_RESULT_MAX: one line
    await _ended(tid, "completed", output=long)
    assert world["posts"][-1]["room"] == ROOM and world["posts"][-1]["text"] == '"Fast," says Linear.'
    assert not (await repo.get_task(WS, tid)).get("outputs")


async def test_a_short_text_result_shows_whole_and_is_kept(world):
    """Dev walk 15: an Instagram post (no file) lived only inside the task. Short text with no file is the finish
    message itself, and the task keeps it as what it made (Made here, Files, Creations)."""
    tid = await _task(root=ROOT)
    await _attempt(tid)
    post = "Drafted the Instagram post.\n\nTuesday is pizza night! 🍕\n\nSave your spot: [BOOKING LINK]"
    await _ended(tid, "completed", output=post)
    room, origin = world["posts"]
    assert origin["room"] == ROOM and origin["kind"] == "task_line" and origin["text"] == post
    (out,) = (await repo.get_task(WS, tid))["outputs"]
    assert (out["id"], out["kind"], out["title"]) == ("text:1", "document", (await repo.get_task(WS, tid))["title"])
    assert out["ref"]["roomId"] == ts.task_room_id(tid) and out["ref"]["messageId"]
    # Walk 16: it carries the deliverable's words, so the library opens and previews it without the task.
    assert out["ref"]["text"] == "Tuesday is pizza night! 🍕\n\nSave your spot: [BOOKING LINK]"
    await acts.end_attempt(WS, tid, 1, "completed", None)  # a retry keeps one output
    assert len((await repo.get_task(WS, tid))["outputs"]) == 1


async def test_a_question_tasks_answer_is_the_main_chat_reply(world):
    """One brain (founder ruling A, 8 Nov): "what have we shipped in the last week?" is a task the Router starts with
    the question verbatim; its Worker's answer (short text, no file) is the reply in the main chat, the card under it."""
    tid = await _task(
        root=ROOT,
        question=True,
        title="What have we shipped in the last week?",
        description="What have we shipped in the last week?",
    )
    await _attempt(tid)
    answer = "Three PRs merged since 1 Nov: #5061 voice worker fix, #5069 Paperclip exit, #5093 credential envelopes."
    await _ended(tid, "completed", output=answer)
    _room, origin = world["posts"]
    assert origin["room"] == ROOM and "thread_root" not in origin and origin["text"] == answer
    assert origin["kind"] == "task_line" and origin["task_refs"] == [{"taskId": tid, "role": "started"}]
    assert not (await repo.get_task(WS, tid)).get("outputs")  # Q&A never fills Files and Creations


async def test_a_long_question_answer_points_to_the_thread(world):
    tid = await _task(root=ROOT, question=True)
    await _attempt(tid)
    await _ended(tid, "completed", output="Twelve PRs merged since 1 Nov. " + "- #50xx a change\n" * 200)
    assert world["posts"][-1]["text"] == "Twelve PRs merged since 1 Nov. Full answer in the thread."


async def test_the_worker_reads_what_was_asked_word_for_word(monkeypatch):
    """Review #5099: the brief is the Router's words; the Worker also gets the trigger's own text under "## Asked"."""
    from unittest.mock import AsyncMock

    from core.services import room_memory

    monkeypatch.setattr(acts, "_local_now", AsyncMock(return_value="now"))
    monkeypatch.setattr(room_memory, "group_room_by_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        repo, "get_trigger", AsyncMock(return_value={"ref": {"text": "what have we shipped in the last week?"}})
    )
    task = {
        "taskId": ts.new_task_id(),
        "title": "Check what shipped",
        "description": "Check what shipped",
        "triggerId": "chat:m1",
        "audience": {"kind": "workspace"},
        "lane": "shared",
    }
    req = await acts._request(WS, task, {"n": 1, "inputTriggerIds": []}, {"attempts": []})
    assert "## Asked\nwhat have we shipped in the last week?" in req.input


async def test_a_task_of_another_room_says_only_done(world):
    tid = await _task(audience={"kind": "room", "roomId": str(uuid.uuid4())})
    await _attempt(tid)
    await _ended(tid, "completed")
    assert world["posts"][-1]["room"] == ROOM and world["posts"][-1]["text"] == "Done."
