"""`router_turn`: render, run, apply (wave-2 O Task 11; contracts-v2 §9, §10; G2, G18, G27).
Review focus 2: a retried `router_turn` applies once."""

import sys
import types
import uuid

import pytest

from core import tasks_schema as ts
from core.repositories import tasks_repo as repo
from core.services import runtime_runs
from core.temporal import activities as acts
from core.temporal import router_prompt
from core.temporal.types import RouterTurnIn

from .test_tasks_repo import table  # noqa: F401 -- moto tasks table fixture

WS = "org_1"
CEO = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FIN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ROOM = str(uuid.uuid4())
BATCH = str(uuid.uuid4())
SAM, PRIYA = "user_sam", "user_priya"
ROSTER = [
    {"id": CEO, "name": "Aden", "role": "ceo", "status": "active"},
    {"id": FIN, "name": "Finance", "role": "specialist", "title": "Books and invoices", "status": "active"},
]


@pytest.fixture
def world(table, monkeypatch):  # noqa: F811
    w = {
        "posts": [],
        "runs": [],
        "polls": [],
        "decisions": [],
        "output": "",
        "first_status": "completed",
        "starts": [],
        "inputs": [],
        "gate": {"blocked": False},
        "indexed": [],
        "noted": [],
        "rooms": [],
    }

    async def post(ws, room_id, **kw):
        prior = next((p for p in w["posts"] if p["event_id"] == kw["event_id"]), None)
        if prior:
            return prior["id"]
        w["posts"].append({"room": room_id, "id": str(uuid.uuid4()), **kw})
        return w["posts"][-1]["id"]

    async def ensure_room(ws, task, **kw):
        w["rooms"].append(task["taskId"])
        return ts.task_room_id(task["taskId"])

    async def front(ws):
        return CEO

    async def roster(ws):
        return ROSTER

    def status(identity, state):
        return runtime_runs.RunStatus(identity.run_id, state, w["output"], None, None, [], list(w["decisions"]))

    async def create_run(ws, identity, request):
        # The bridge: a second POST of the same run id with another request is an idempotency conflict (409).
        prior = next((r for i, r in w["runs"] if i.run_id == identity.run_id), None)
        w["runs"].append((identity, request))
        if prior is not None and prior != request:
            raise runtime_runs.RunRefused("idempotency_conflict")
        return status(identity, w["first_status"])

    async def get_run(ws, identity):
        w["polls"].append(identity.run_id)
        return status(identity, "completed")

    async def start_wf(ws, task_id, reason):
        w["starts"].append((task_id, reason))

    async def signal_input(ws, task_id, ref):
        w["inputs"].append((task_id, ref.trigger_id, ref.kind))

    async def gate(**kw):
        return w["gate"]

    async def get_msg(room_id, message_id):
        return {"messageId": message_id, "text": "pay acme and book the flights", "author": {"displayName": "Sam"}}

    async def get_room(room_id):
        return {"roomId": room_id, "kind": "channel", "name": "general"}

    async def index(ws, row):
        w["indexed"].append((row["taskId"], row["status"]))

    async def noted(ws, key):
        w["noted"].append(key)

    async def provider(ws):
        return ("byo_key", "anthropic")

    monkeypatch.setattr(acts.task_conversation, "post_as_lightbulb", post)
    monkeypatch.setattr(acts.task_conversation, "ensure_task_room", ensure_room)
    monkeypatch.setattr(acts.task_conversation, "front_agent_id", front)
    monkeypatch.setattr(acts.messaging, "load_agent_roster", roster)
    monkeypatch.setattr(acts.runtime_runs, "create_run", create_run)
    monkeypatch.setattr(acts.runtime_runs, "get_run", get_run)
    monkeypatch.setattr(acts.temporal_client, "start_task_workflow", start_wf)
    monkeypatch.setattr(acts.temporal_client, "signal_task_input", signal_input)
    monkeypatch.setattr(acts.chat_gate, "gate_chat", gate)
    monkeypatch.setattr(acts.messaging_repo, "get_message_by_id", get_msg)
    monkeypatch.setattr(acts.messaging_repo, "get_room", get_room)
    monkeypatch.setattr(acts.memory_tasks_index, "index_task", index)
    monkeypatch.setattr(acts.rules_activities, "note_task_started", noted)
    monkeypatch.setattr(acts.container_provision, "resolve_provider_choice", provider)
    monkeypatch.setattr(acts, "ROUTER_POLL_S", 0)
    monkeypatch.setattr(acts.activity, "heartbeat", lambda *a, **k: None)
    return w


async def _trig(tid, *, admission="respond", lane="shared", actor=SAM, source="chat", ref=None, routed=None):
    row = {
        "triggerId": tid,
        "source": source,
        "ref": ref if ref is not None else {"roomId": ROOM, "messageId": str(uuid.uuid4())},
        "admission": admission,
        "lane": lane,
        "actorUserId": actor,
        "receivedAt": "2026-11-05T01:00:00Z",
    }
    if routed:
        row["routedTo"] = routed
    await repo.put_trigger(WS, row)
    return tid


async def _open_task(audience, status="working", **over):
    tid = ts.new_task_id()
    row = {
        "taskId": tid,
        "identifier": "LB-0",
        "title": "Open one",
        "status": "queued",
        "audience": audience,
        "lane": "shared",
        "triggerId": "chat:x",
        **over,
    }
    await repo.create_task(WS, row)
    if status != "queued":
        await repo.transition_task(WS, tid, to=status)
    return tid


def _start(tids, title="Pay Acme", specialist=FIN, ack="On it: paying Acme.", **extra):
    return {
        "kind": "start_task",
        "triggerIds": tids,
        "title": title,
        "brief": f"{title} now",
        "specialist": specialist,
        "ack": ack,
        **extra,
    }


def _run(trigger_ids, lane="shared", batch=BATCH):
    return acts.router_turn(RouterTurnIn(WS, lane, batch, trigger_ids))


@pytest.mark.asyncio
async def test_three_asks_one_reply_three_cards(world):
    t1 = await _trig("chat:m1")
    world["decisions"] = [
        _start([t1]),
        _start([t1], title="Book the flights", specialist=None, ack="And booking the flights."),
        {
            "kind": "propose_tasks",
            "triggerIds": [t1],
            "reason": "a paid tool",
            "ack": "One more you can start:",
            "tasks": [{"title": "Estimate the Stripe fees", "brief": "Estimate", "specialist": FIN}],
        },
    ]
    out = await _run([t1])
    assert len(world["posts"]) == 1  # one line, three cards (G2)
    post = world["posts"][0]
    assert post["room"] == ROOM and post["event_id"] == f"router:{BATCH}:0" and post["thread_root"] is None
    assert post["text"] == "On it: paying Acme.\nAnd booking the flights.\nOne more you can start:"
    assert post["remember"] is False  # acks only: kept out of room memory (audit item 12)
    ids = [r["taskId"] for r in post["task_refs"]]
    assert [r["role"] for r in post["task_refs"]] == ["started", "started", "suggested"]
    assert ids == [ts.router_task_id(BATCH, 0, 0), ts.router_task_id(BATCH, 1, 0), ts.router_task_id(BATCH, 2, 0)]
    rows = [await repo.get_task(WS, i) for i in ids]
    assert [r["status"] for r in rows] == ["queued", "queued", "proposed"]
    assert [r["specialist"] for r in rows] == [FIN, None, FIN]
    assert rows[2]["proposalReason"] == "a paid tool" and rows[0]["audience"] == {
        "kind": "room",
        "roomId": ROOM,
        "userIds": [],
    }
    assert sorted(r["identifier"] for r in rows) == ["LB-1", "LB-2", "LB-3"]
    assert rows[0]["ownerUserId"] == SAM and rows[0]["triggerId"] == t1 and rows[0]["lane"] == "shared"
    assert out.started_task_ids == ids[:2] and out.posted_message_ids == [post["id"]]
    assert world["starts"] == [(ids[0], "router"), (ids[1], "router")]
    assert sorted(world["rooms"]) == sorted(ids) and {t for t, _ in world["indexed"]} == set(ids)
    assert (await repo.get_trigger(WS, t1))["routedTo"] == {"taskId": ids[0]}
    # The first started task holds the ack thread (one thread, one task); the others fall back to the room.
    ack_thread = f"room:{ROOM}:{post['id']}"
    assert await repo.task_for_thread(WS, ack_thread) == ids[0]
    assert world["noted"] == [f"room:{ROOM}:{(await repo.get_trigger(WS, t1))['ref']['messageId']}"]


@pytest.mark.asyncio
async def test_router_turn_retry_applies_once(world):
    t1 = await _trig("chat:m1")
    world["decisions"] = [
        _start([t1]),
        {"kind": "propose_tasks", "triggerIds": [t1], "ack": "Or:", "tasks": [{"title": "B", "brief": "b"}]},
    ]
    first = await _run([t1])
    again = await _run([t1])
    assert first == again
    assert len(world["posts"]) == 1 and len(world["runs"]) == 2  # the same run id, re-POSTed
    assert world["runs"][0][0].run_id == world["runs"][1][0].run_id == ts.router_run_id(WS, BATCH)
    # The retry rendered a different prompt (the first apply created tasks the batch now lists), the bridge
    # refused the re-POST, and the stored run's decisions were read back instead of a fallback.
    assert world["runs"][0][1] != world["runs"][1][1] and world["polls"] == [ts.router_run_id(WS, BATCH)]
    working, _ = await repo.list_tasks_by_status(WS, "queued")
    proposed, _ = await repo.list_tasks_by_status(WS, "proposed")
    assert len(working) == 1 and len(proposed) == 1
    assert sorted(r["identifier"] for r in working + proposed) == ["LB-1", "LB-2"]  # the replay mints nothing


@pytest.mark.asyncio
async def test_invalid_specialist_dropped(world):
    t1 = await _trig("chat:m1")
    t2 = await _trig("chat:m2")
    nobody = str(uuid.UUID(int=5))
    world["decisions"] = [
        _start([t1], specialist=nobody),  # dropped: not on the roster
        {"kind": "pass_through", "triggerIds": [t2], "taskId": str(uuid.UUID(int=6))},  # dropped: not open
        {"kind": "reply", "triggerIds": ["chat:other"], "text": "nope"},  # dropped: not in the batch
        {"kind": "start_task", "triggerIds": [t2], "title": "x"},  # dropped: malformed
        {"kind": "reply", "triggerIds": [t2], "text": "Done already."},
    ]
    await _run([t1, t2])
    assert world["starts"] == [] and world["inputs"] == []
    texts = {p["event_id"]: p["text"] for p in world["posts"]}
    assert texts == {f"router:{BATCH}:0": router_prompt.FALLBACK, f"router:{BATCH}:1": "Done already."}


@pytest.mark.asyncio
async def test_uncovered_respond_gets_reply(world):
    t1 = await _trig("chat:m1")
    t2 = await _trig("chat:m2", admission="candidate")
    world["output"] = "Sure, the invoice went out on Monday."
    await _run([t1, t2])
    assert [(p["event_id"], p["text"]) for p in world["posts"]] == [(f"router:{BATCH}:0", world["output"])]


@pytest.mark.asyncio
async def test_pass_through_signals_input(world):
    open_id = await _open_task({"kind": "room", "roomId": ROOM})
    t1 = await _trig("chat:m1")
    world["decisions"] = [
        {"kind": "pass_through", "triggerIds": [t1], "taskId": open_id, "ack": "Passed to the Acme task."}
    ]
    out = await _run([t1])
    assert world["inputs"] == [(open_id, t1, "message")] and out.started_task_ids == []
    assert world["posts"][0]["text"] == "Passed to the Acme task." and world["posts"][0]["task_refs"] is None
    assert (await repo.get_trigger(WS, t1))["routedTo"] == {"taskId": open_id}
    assert len(world["noted"]) == 1
    assert open_id in world["runs"][0][1].input  # the open task was offered to the Router


@pytest.mark.asyncio
async def test_pinned_specialist_in_prompt(world):
    mid = str(uuid.uuid4())
    t1 = await _trig("chat:m1", ref={"roomId": ROOM, "messageId": mid, "threadRoot": mid, "pinnedSpecialist": FIN})
    world["decisions"] = [_start([t1])]
    await _run([t1])
    identity, request = world["runs"][0]
    assert f"pinned specialist: Finance ({FIN})" in request.input
    assert "Sam: pay acme and book the flights" in request.input and "#general" in request.input
    assert (
        f"- {FIN} · Finance · Books and invoices" in request.input
        and CEO not in request.input.split("## Specialists")[1]
    )
    assert request.instructions == router_prompt.ROUTER_INSTRUCTIONS
    assert request.memory == {
        "lane": "shared",
        "member": None,
        "audience": {"kind": "room", "roomId": ROOM},
        "tags": {},
        "sync": "none",
    }
    assert (identity.mode, identity.lane, identity.model, identity.agent_id) == (
        "router",
        "shared",
        "claude-haiku-4-5",
        CEO,
    )
    assert identity.run_id == ts.router_run_id(WS, BATCH) and identity.task_id == BATCH
    assert world["posts"][0]["thread_root"] == mid  # answered in the thread the ask came from
    assert await repo.task_for_thread(WS, f"room:{ROOM}:{mid}") == ts.router_task_id(BATCH, 0, 0)


@pytest.mark.asyncio
async def test_router_ask_is_plain_reply(world):
    t1 = await _trig("chat:m1")
    world["decisions"] = [{"kind": "ask", "triggerIds": [t1], "text": "Which card?", "options": ["Amex", "Visa"]}]
    await _run([t1])
    assert world["posts"][0]["text"] == "Which card? (Amex / Visa)" and world["posts"][0]["task_refs"] is None
    for status in ("queued", "proposed", "waiting"):
        assert (await repo.list_tasks_by_status(WS, status))[0] == []  # no task, no Question row (G27)


@pytest.mark.asyncio
async def test_private_lane_batch_reads_only_that_users_triggers(world):
    lane = ts.router_lane_key(SAM)
    mine = await _open_task({"kind": "person", "userIds": [SAM]}, lane="private", ownerUserId=SAM)
    theirs = await _open_task({"kind": "person", "userIds": [PRIYA]}, lane="private", ownerUserId=PRIYA)
    t1 = await _trig("chat:m1", lane=lane, ref={"roomId": ROOM, "messageId": str(uuid.uuid4())})
    world["decisions"] = [_start([t1], specialist=None)]
    await _run([t1], lane=lane)
    identity, request = world["runs"][0]
    assert mine in request.input and theirs not in request.input
    assert request.memory["lane"] == "private" and request.memory["member"] == SAM
    assert request.memory["audience"] == {"kind": "person", "userId": SAM}
    assert identity.lane == lane and identity.session_key == f"agent:{CEO}:lightbulb:router:{lane}"
    task = await repo.get_task(WS, ts.router_task_id(BATCH, 0, 0))
    assert task["lane"] == "private" and task["audience"] == {"kind": "person", "userIds": [SAM], "roomId": None}


@pytest.mark.asyncio
async def test_gate_chat_refusal_blocks_start(world):
    t1 = await _trig("chat:m1")
    world["gate"] = {"blocked": True, "code": "TRIAL_LIMIT", "message": "Your trial's daily limit is reached."}
    world["decisions"] = [_start([t1])]
    out = await _run([t1])
    assert out.started_task_ids == [] and world["starts"] == []
    assert await repo.get_task(WS, ts.router_task_id(BATCH, 0, 0)) is None
    assert world["posts"][0]["text"] == "Your trial's daily limit is reached."


@pytest.mark.asyncio
async def test_deterministic_start_skips_router_run(world):
    t1 = await _trig(
        "sched:s1:100",
        source="schedule",
        actor=None,
        ref={
            "deliverRoomId": ROOM,
            "a2aTaskId": "a2a-task-1",
            "start": {
                "kind": "start_task",
                "triggerIds": ["sched:s1:100"],
                "title": "Weekly digest",
                "brief": "Write it",
                "specialist": FIN,
                "ack": "Starting the weekly digest.",
            },
        },
    )
    out = await _run([t1])
    assert world["runs"] == []  # no model turn
    task_id = ts.router_task_id(t1, 0, 0)  # keyed by the trigger: the automation's `ref.taskId` (A5)
    task = await repo.get_task(WS, task_id)
    assert task and task["status"] == "queued" and task["specialist"] == FIN and task["a2aTaskId"] == "a2a-task-1"
    assert (await repo.get_trigger(WS, t1))["routedTo"] == {"taskId": task_id}  # Q10 finds the task here on a replay
    assert task["audience"] == {"kind": "room", "roomId": ROOM, "userIds": []} and task.get("ownerUserId") is None
    assert world["posts"] == [
        {
            "room": ROOM,
            "id": world["posts"][0]["id"],
            "text": "Starting the weekly digest.",
            "event_id": f"router:{BATCH}:0",
            "thread_root": None,
            "task_refs": [{"taskId": task_id, "role": "started"}],
            "remember": False,
        }
    ]
    assert out.started_task_ids == [task_id] and world["starts"] == [(task_id, "router")]


@pytest.mark.asyncio
async def test_reply_to_mail_trigger_uses_deliver_reply(world, monkeypatch):
    sent = []

    async def deliver_reply(ws, trigger, text, *, idempotency_key):
        sent.append((trigger["triggerId"], text, idempotency_key))

    monkeypatch.setitem(sys.modules, "core.services.source_replies", types.SimpleNamespace(deliver_reply=deliver_reply))
    t1 = await _trig("mail:abc", source="mail", ref={"text": "Can you send the invoice?", "from": "bob@acme.test"})
    world["decisions"] = [{"kind": "reply", "triggerIds": [t1], "text": "Sent it just now."}]
    await _run([t1])
    assert sent == [(t1, "Sent it just now.", f"router:{BATCH}:0")] and world["posts"] == []
    assert "bob@acme.test: Can you send the invoice?" in world["runs"][0][1].input


@pytest.mark.asyncio
async def test_router_run_polls_until_done(world):
    t1 = await _trig("chat:m1")
    world["first_status"] = "running"
    world["decisions"] = [{"kind": "reply", "triggerIds": [t1], "text": "Hi"}]
    await _run([t1])
    assert world["polls"] == [ts.router_run_id(WS, BATCH)] and world["posts"][0]["text"] == "Hi"


@pytest.mark.asyncio
async def test_apply_fallback_posts_once_per_respond(world):
    t1 = await _trig("chat:m1")
    t2 = await _trig("chat:m2", admission="candidate")
    await acts.apply_fallback(WS, [t1, t2])
    await acts.apply_fallback(WS, [t1, t2])
    assert [(p["event_id"], p["text"]) for p in world["posts"]] == [(f"router-fallback:{t1}", router_prompt.FALLBACK)]
    assert world["posts"][0]["remember"] is False


def test_validate_decisions_keeps_order_and_drops_only_bad():
    room_task = {"taskId": str(uuid.UUID(int=1)), "audience": {"kind": "room", "roomId": "r1"}}
    other_room = {"taskId": str(uuid.UUID(int=2)), "audience": {"kind": "room", "roomId": "r2"}}
    ws_task = {"taskId": str(uuid.UUID(int=3)), "audience": {"kind": "workspace"}}
    kept = router_prompt.validate_decisions(
        [
            {"kind": "ignore", "triggerIds": ["a", "zzz"]},
            {
                "kind": "propose_tasks",
                "triggerIds": ["a"],
                "ack": "x",
                "tasks": [{"title": "ok", "brief": "b"}, {"title": "bad", "brief": "b", "specialist": "nope"}],
            },
            {
                "kind": "propose_tasks",
                "triggerIds": ["a"],
                "ack": "x",
                "tasks": [{"title": "bad", "brief": "b", "specialist": "nope"}],
            },
            "garbage",
            {"kind": "pass_through", "triggerIds": ["a"], "taskId": room_task["taskId"]},
            {
                "kind": "pass_through",
                "triggerIds": ["a"],
                "taskId": other_room["taskId"],
            },  # room A never reaches room B
            {"kind": "pass_through", "triggerIds": ["a"], "taskId": ws_task["taskId"]},
            {"kind": "pass_through", "triggerIds": ["a"], "taskId": str(uuid.UUID(int=9))},  # not open
        ],
        trigger_ids={"a"},
        roster_ids={FIN},
        open_tasks={t["taskId"]: t for t in (room_task, other_room, ws_task)},
        scope=lambda tid: ("shared", SAM, "r1"),
        in_scope=acts._task_in_scope,
    )
    assert [type(d).__name__ for d in kept] == ["Ignore", "ProposeTasks", "PassThrough", "PassThrough"]
    assert kept[0].trigger_ids == ["a"] and [t.title for t in kept[1].tasks] == ["ok"]
    assert [d.task_id for d in kept[2:]] == [room_task["taskId"], ws_task["taskId"]]
    # A DM (private lane) reaches only person tasks naming that user.
    lane = ts.router_lane_key(SAM)
    assert acts._task_in_scope({"audience": {"kind": "person", "userIds": [SAM]}}, lane, SAM, None)
    assert not acts._task_in_scope(room_task, lane, SAM, "r1")
    assert not acts._task_in_scope({"audience": {"kind": "person", "userIds": [PRIYA]}}, lane, SAM, None)


def test_render_batch_shape():
    text = router_prompt.render_batch(
        [
            {
                "triggerId": "chat:1",
                "source": "chat",
                "admission": "respond",
                "text": "a\nb  c",
                "who": "Sam",
                "where": "#general",
                "threaded": True,
                "routedTo": {"specialist": FIN},
            }
        ],
        ROSTER,
        [],
        lane=ts.router_lane_key(SAM),
    )
    assert text.startswith("# Batch (private lane)")
    assert "- [chat:1] respond · chat · #general · in a thread · likely specialist: Finance" in text
    assert "  Sam: a b c" in text and "- none" in text


@pytest.mark.asyncio
async def test_visibility_clamped_to_the_trigger(world):
    t_room = await _trig("chat:m1")
    t_mail = await _trig("mail:m2", source="mail", ref={"text": "hi", "deliverRoomId": None})
    world["decisions"] = [
        _start([t_room], visibility="workspace"),  # a room trigger never widens to the workspace
        _start([t_room], visibility="person"),
        _start([t_mail], visibility="room"),  # no room: workspace
    ]
    await _run([t_room, t_mail])
    kinds = [(await repo.get_task(WS, ts.router_task_id(BATCH, i, 0)))["audience"]["kind"] for i in range(3)]
    assert kinds == ["room", "person", "workspace"]
    # Private lane: always the person's, whatever the model asked.
    lane = ts.router_lane_key(SAM)
    t_dm = await _trig("chat:m3", lane=lane)
    world["decisions"] = [_start([t_dm], visibility="workspace")]
    await _run([t_dm], lane=lane, batch=str(uuid.uuid4()))
    assert (await repo.get_task(WS, world["starts"][-1][0]))["audience"]["kind"] == "person"


@pytest.mark.asyncio
async def test_person_task_without_an_actor_drops_only_that_decision(world):
    start = {"title": "x", "brief": "y", "ack": "ok", "visibility": "person"}
    t1 = await _trig("sched:s9:1", source="schedule", actor=None, ref={"deliverRoomId": ROOM, "start": start})
    t2 = await _trig("chat:m2")
    world["decisions"] = [{"kind": "reply", "triggerIds": [t2], "text": "Fine."}]
    out = await _run([t1, t2])
    assert out.started_task_ids == [] and world["starts"] == []
    texts = {p["event_id"]: p["text"] for p in world["posts"]}
    assert texts == {f"router:{BATCH}:0": router_prompt.FALLBACK, f"router:{BATCH}:1": "Fine."}  # the batch went on


@pytest.mark.asyncio
async def test_routed_to_set_on_every_trigger_a_start_covers(world):
    t1, t2 = await _trig("chat:m1"), await _trig("chat:m2")
    world["decisions"] = [_start([t1, t2])]
    await _run([t1, t2])
    task_id = ts.router_task_id(BATCH, 0, 0)
    assert [(await repo.get_trigger(WS, t))["routedTo"] for t in (t1, t2)] == [{"taskId": task_id}] * 2
    assert len(world["noted"]) == 2 and len(world["posts"]) == 1


@pytest.mark.asyncio
async def test_pass_through_without_ack_still_routes(world):
    open_id = await _open_task({"kind": "workspace"})
    t1 = await _trig("chat:m1")
    world["decisions"] = [{"kind": "pass_through", "triggerIds": [t1], "taskId": open_id}]
    await _run([t1])
    assert world["posts"] == [] and world["inputs"] == [(open_id, t1, "message")]
    assert (await repo.get_trigger(WS, t1))["routedTo"] == {"taskId": open_id} and len(world["noted"]) == 1


@pytest.mark.asyncio
async def test_pass_through_never_crosses_rooms_or_dms(world):
    other = await _open_task({"kind": "room", "roomId": str(uuid.uuid4())})
    t1 = await _trig("chat:m1")
    world["decisions"] = [{"kind": "pass_through", "triggerIds": [t1], "taskId": other, "ack": "Passed."}]
    await _run([t1])
    assert world["inputs"] == [] and world["posts"][0]["text"] == router_prompt.FALLBACK
    assert other not in world["runs"][0][1].input  # another room's task is not even offered


@pytest.mark.asyncio
async def test_ignored_respond_trigger_gets_the_fallback_line(world):
    t1 = await _trig("chat:m1")
    t2 = await _trig("chat:m2", admission="candidate")
    world["decisions"] = [{"kind": "ignore", "triggerIds": [t1, t2]}]
    await _run([t1, t2])
    assert [(p["event_id"], p["text"]) for p in world["posts"]] == [(f"router:{BATCH}:0", router_prompt.FALLBACK)]
    assert world["posts"][0]["remember"] is False  # the fallback line is boilerplate: not remembered
