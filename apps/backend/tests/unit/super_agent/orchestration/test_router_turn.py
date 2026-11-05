"""`router_turn`: render, run, apply (wave-2 O Task 11; contracts-v2 §9, §10; G2, G18, G27).
Review focus 2: a retried `router_turn` applies once."""

import re
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
    w["ready"] = True

    async def runtime_ready(ws):
        return w["ready"]

    monkeypatch.setattr(acts, "_runtime_ready", runtime_ready)
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
    assert post["text"] == "On it: paying Acme."  # one ack per trigger: the first decision's (the cards say the rest)
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
async def test_same_summary_on_every_decision_posts_once(world):
    """Dev 5 Nov (#general seq 7): two start_task decisions on one ask both carried the summary ack, and the message
    showed it twice. One ack line per trigger, whatever the model repeats; a reply's text still shows."""
    t1 = await _trig("chat:m1")
    line = "I split this up: Growth has the agenda, and Customer Operations has the venues."
    world["decisions"] = [
        _start([t1], title="Agenda", ack=line),
        _start([t1], title="Venues", specialist=None, ack=line),
        {"kind": "reply", "triggerIds": [t1], "text": "Venues come with rough prices."},
    ]
    await _run([t1])
    (post,) = world["posts"]
    assert post["text"] == f"{line}\nVenues come with rough prices."
    assert len(post["task_refs"]) == 2


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
    assert identity.lane == lane
    assert identity.session_key == f"agent:{CEO}:lightbulb:router:{lane}:{ts.router_room_key(ROOM)}"
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
async def test_gate_chat_refusal_blocks_pass_through(world):
    """§14.10: a pass_through steers or re-runs a task, so it is gated like a start."""
    open_id = await _open_task({"kind": "room", "roomId": ROOM})
    t1 = await _trig("chat:m1")
    world["gate"] = {"blocked": True, "code": "TRIAL_LIMIT", "message": "Your trial's daily limit is reached."}
    world["decisions"] = [{"kind": "pass_through", "triggerIds": [t1], "taskId": open_id, "ack": "Passed on."}]
    await _run([t1])
    assert world["inputs"] == [] and (await repo.get_trigger(WS, t1)).get("routedTo") is None
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

    # The module activities imported, not sys.modules: once anything imported the real one, `from core.services
    # import source_replies` reads the package attribute and a sys.modules stand-in is never seen (suite order).
    monkeypatch.setattr(acts.source_replies, "deliver_reply", deliver_reply)
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
async def test_router_turn_waits_while_the_runtime_is_still_starting(world):
    """A new workspace's first message: no run is started (it would fail) and the workflow is told to wait."""
    from temporalio.exceptions import ApplicationError

    t1 = await _trig("chat:m1")
    world["ready"] = False
    with pytest.raises(ApplicationError) as err:
        await _run([t1])
    assert err.value.type == acts.RUNTIME_NOT_READY and err.value.non_retryable
    assert world["runs"] == [] and world["posts"] == []


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


@pytest.mark.asyncio
async def test_each_room_has_its_own_router_session(world):
    """Privacy: a Router run reads its session's previous conversation, so two rooms' batches (and a DM's) never
    share a session. A trigger with no room (agent mail) gets its own conversation's session, never the old per-lane
    key whose history mixed every room; a deliver room is the trigger's room."""
    other = str(uuid.uuid4())
    t1 = await _trig("chat:m1")
    t2 = await _trig("chat:m2", ref={"roomId": other, "messageId": str(uuid.uuid4())})
    t3 = await _trig("mail:x1", source="mail", ref={"text": "hi", "sessionKey": "mail:thread-a"})
    t4 = await _trig("mail:x2", source="mail", ref={"text": "hi", "sessionKey": "mail:thread-b"})
    t5 = await _trig("sched:s1", source="schedule", ref={"text": "digest", "deliverRoomId": other})
    for i, tid in enumerate((t1, t2, t3, t4, t5)):
        await _run([tid], batch=str(uuid.UUID(int=i + 1)))
    keys = [identity.session_key for identity, _ in world["runs"]]
    lane_key = f"agent:{CEO}:lightbulb:router:shared"
    assert keys[:2] == [f"{lane_key}:{ts.router_room_key(ROOM)}", f"{lane_key}:{ts.router_room_key(other)}"]
    assert lane_key not in (keys[2], keys[3]) and keys[2] != keys[3]  # two outside senders, two sessions
    assert keys[4] == keys[1]  # a deliver room's trigger shares its room's session
    assert all(re.fullmatch(ts.ROUTER_SESSION_KEY_PATTERN, k) for k in keys)
    assert ROOM not in keys[0] and ROOM not in str(world["runs"][0][0].to_wire())


@pytest.mark.asyncio
async def test_bare_trigger_id_still_starts_the_task(world):
    """Dev 5 Nov (#general seq 12): the model named the trigger without its `chat:` prefix; the start was dropped
    and its ack still went out as the run's prose. The bare tail of exactly one batch trigger resolves to it."""
    mid = str(uuid.uuid4())
    t1 = await _trig(f"chat:{mid}", ref={"roomId": ROOM, "messageId": mid})
    world["output"] = "Growth has the day-of checklist."
    world["decisions"] = [{**_start([mid], ack="Growth has the day-of checklist."), "triggerIds": [mid]}]
    out = await _run([t1])
    assert out.started_task_ids == [ts.router_task_id(BATCH, 0, 0)]
    assert world["posts"][0]["task_refs"] == [{"taskId": ts.router_task_id(BATCH, 0, 0), "role": "started"}]


@pytest.mark.asyncio
async def test_a_dropped_decision_never_posts_its_summary(world):
    """When every decision for a trigger is dropped (unknown trigger, off-roster specialist), the reply is the plain
    fallback, never the run's prose claiming the assignment."""
    t1 = await _trig("chat:m1")
    world["output"] = "Growth has the day-of checklist."
    world["decisions"] = [_start(["chat:nope"], ack="Growth has the day-of checklist.")]
    out = await _run([t1])
    assert out.started_task_ids == []
    assert world["posts"][0]["text"] == router_prompt.FALLBACK and world["posts"][0]["task_refs"] is None


@pytest.mark.asyncio
async def test_prose_without_decisions_still_answers(world):
    t1 = await _trig("chat:m1")
    world["output"] = "March 1 works."
    world["decisions"] = []
    await _run([t1])
    assert world["posts"][0]["text"] == "March 1 works."


def test_a_bare_id_naming_two_triggers_resolves_to_none():
    batch = {"chat:x1", "mail:x1", "chat:y2"}
    assert router_prompt._resolve("y2", batch) == "chat:y2"
    assert router_prompt._resolve("x1", batch) is None
    assert router_prompt._resolve("chat:x1", batch) == "chat:x1"


@pytest.mark.asyncio
async def test_router_sees_which_lightbulb_lines_really_started_work(world, monkeypatch):
    """Live dev 5 Nov: a dropped start's ack ("Growth has the checklist") sat in the Router's transcript and the
    next ask got "Already underway" with no task. The Router now sees the room's recent lines with the cards each
    Lightbulb line really carried, and the rule that only Open tasks are underway."""
    started = ts.router_task_id(BATCH, 9, 0)

    async def get_msg(room_id, message_id):
        return {"messageId": message_id, "messageSeq": 13, "text": "please make the checklist", "author": {}}

    async def before(room_id, seq, limit):
        assert seq == 13
        return [
            {"messageId": "a", "author": {"kind": "human", "displayName": "Sam"}, "text": "make a checklist"},
            {
                "messageId": "b",
                "author": {"kind": "agent", "id": CEO, "displayName": "Lightbulb"},
                "text": "Growth has the day-of checklist.",
            },
            {
                "messageId": "c",
                "author": {"kind": "agent", "id": CEO, "displayName": "Lightbulb"},
                "text": "On it: paying Acme.",
                "taskRefs": [{"taskId": started, "role": "started"}],
            },
            {"messageId": "d", "author": {"kind": "agent", "id": CEO}, "text": "Working: x", "progress": True},
        ]

    monkeypatch.setattr(acts.messaging_repo, "get_message_by_id", get_msg)
    monkeypatch.setattr(acts.messaging_repo, "list_messages_before", before)
    t1 = await _trig("chat:m1")
    world["decisions"] = [_start([t1])]
    await _run([t1])
    prompt = world["runs"][0][1].input
    assert "- Sam: make a checklist" in prompt
    assert "- Lightbulb: Growth has the day-of checklist. [no task card]" in prompt
    assert f"- Lightbulb: On it: paying Acme. [cards: {started} started]" in prompt
    assert "Working: x" not in prompt
    assert 'Work is underway only if it is in "Open tasks"' in world["runs"][0][1].instructions


@pytest.mark.asyncio
async def test_recent_follows_the_thread_and_the_first_room_trigger(world, monkeypatch):
    """A thread follow-up ("make it shorter") sees its thread; Recent comes from the first trigger with a room
    message even when another trigger leads the batch; Lightbulb's own lines keep up to 1200 characters."""
    root = str(uuid.uuid4())
    long_reply = "Here is the checklist. " + "x" * 900
    seen = {}

    async def get_msg(room_id, message_id):
        return {"messageId": message_id, "messageSeq": 20, "text": "make it shorter", "author": {}}

    async def thread(room_id, root_id, seq):
        seen["thread"] = (root_id, seq)
        return [
            {
                "messageId": root,
                "author": {"kind": "agent", "id": CEO, "displayName": "Lightbulb"},
                "text": long_reply,
                "taskRefs": [{"taskId": "t9", "role": "started"}],
            }
        ]

    async def before(*a):
        raise AssertionError("a thread follow-up reads its thread, not the room")

    monkeypatch.setattr(acts.messaging_repo, "get_message_by_id", get_msg)
    monkeypatch.setattr(acts.messaging_repo, "list_thread", thread)
    monkeypatch.setattr(acts.messaging_repo, "list_messages_before", before)
    t_sched = await _trig("sched:s1", source="schedule", ref={"text": "digest", "deliverRoomId": ROOM})
    t_chat = await _trig("chat:m1", ref={"roomId": ROOM, "messageId": str(uuid.uuid4()), "threadRoot": root})
    world["decisions"] = [{"kind": "reply", "triggerIds": [t_chat], "text": "Shorter: ok."}]
    await _run([t_sched, t_chat])
    prompt = world["runs"][0][1].input
    assert seen["thread"] == (root, 20)
    assert f"- Lightbulb: {long_reply} [cards: t9 started]" in prompt


@pytest.mark.asyncio
async def test_a_batch_without_a_room_message_says_no_lines_are_known(world):
    t1 = await _trig("mail:x1", source="mail", ref={"text": "hi", "sessionKey": "mail:thread-a"})
    world["decisions"] = [{"kind": "reply", "triggerIds": [t1], "text": "Hello."}]
    await _run([t1])
    assert "## Recent in this conversation\n- not available" in world["runs"][0][1].input


@pytest.mark.asyncio
async def test_a_deliver_room_that_is_not_a_room_never_fails_the_batch(world, monkeypatch):
    """Gate run 2 (A11): a Run-now trigger delivering to `acc-a1-66ac88` crashed _deliver (bad UUID), failed the
    batch and its fallback, and its task sat queued with no workflow. Now: no post, the task still starts."""
    t1 = await _trig("manual:s1:r1", source="manual", ref={"deliverRoomId": "acc-a1-66ac88"})
    world["decisions"] = [_start([t1])]
    out = await _run([t1])
    assert out.started_task_ids == [ts.router_task_id(BATCH, 0, 0)] and world["posts"] == []
    assert world["starts"] == [(ts.router_task_id(BATCH, 0, 0), "router")]

    async def gone(room_id):
        return None

    monkeypatch.setattr(acts.messaging_repo, "get_room", gone)  # a deleted room: same
    t2 = await _trig("chat:m2")
    world["decisions"] = [{"kind": "reply", "triggerIds": [t2], "text": "Hi."}]
    await _run([t2], batch=str(uuid.UUID(int=7)))
    assert world["posts"] == []


async def test_one_failed_start_never_skips_the_others(world, monkeypatch):
    """A start that fails is logged; the batch's later starts still go out, then the Activity fails so its retry
    (idempotent for the started ones) gives the failed one another go."""
    t1 = await _trig("chat:m1")
    world["decisions"] = [_start([t1]), _start([t1], title="Book the flights", specialist=None)]
    first = ts.router_task_id(BATCH, 0, 0)

    async def flaky(ws, task_id, reason):
        if task_id == first:
            raise RuntimeError("temporal hiccup")
        world["starts"].append((task_id, reason))

    monkeypatch.setattr(acts.temporal_client, "start_task_workflow", flaky)
    with pytest.raises(RuntimeError, match="start_task_workflow failed"):
        await _run([t1])
    assert world["starts"] == [(ts.router_task_id(BATCH, 1, 0), "router")]


@pytest.mark.asyncio
async def test_a_failed_start_still_records_the_started_ones_and_the_fallback_says_partial(world, monkeypatch):
    """Review on #4845: the starts that went out are recorded (routedTo, rules note) before the Activity raises,
    so a second failure loses nothing, and apply_fallback for a trigger with posted cards says only that some of
    it couldn't start."""
    t1 = await _trig("chat:m1")
    t2 = await _trig("chat:m2")
    world["decisions"] = [_start([t1]), _start([t2], title="Book the flights", specialist=None)]
    bad = ts.router_task_id(BATCH, 1, 0)

    async def flaky(ws, task_id, reason):
        if task_id == bad:
            raise RuntimeError("temporal hiccup")
        world["starts"].append((task_id, reason))

    monkeypatch.setattr(acts.temporal_client, "start_task_workflow", flaky)
    with pytest.raises(RuntimeError):
        await _run([t1, t2])
    assert (await repo.get_trigger(WS, t1))["routedTo"] == {"taskId": ts.router_task_id(BATCH, 0, 0)}
    assert (await repo.get_trigger(WS, t2)).get("routedTo") is None
    await acts.apply_fallback(WS, [t1, t2])
    lines = {p["event_id"]: p["text"] for p in world["posts"] if p["event_id"].startswith("router-fallback:")}
    assert lines == {f"router-fallback:{t1}": router_prompt.PARTIAL, f"router-fallback:{t2}": router_prompt.FALLBACK}


@pytest.mark.asyncio
async def test_a_retried_pass_through_signals_once(world):
    """A retried router_turn (same batch) never signals the task again: routedTo records the pass_through."""
    open_id = await _open_task({"kind": "room", "roomId": ROOM})
    t1 = await _trig("chat:m1")
    world["decisions"] = [{"kind": "pass_through", "triggerIds": [t1], "taskId": open_id}]
    await _run([t1])
    await _run([t1])
    assert world["inputs"] == [(open_id, t1, "message")]


@pytest.mark.asyncio
async def test_a_fanned_out_trigger_records_the_task_that_started(world, monkeypatch):
    """One ask, two starts: the first failed, the second started. The trigger records the started one, so the
    fallback says only that some of it couldn't start."""
    t1 = await _trig("chat:m1")
    world["decisions"] = [_start([t1]), _start([t1], title="Book the flights", specialist=None)]
    bad, good = ts.router_task_id(BATCH, 0, 0), ts.router_task_id(BATCH, 1, 0)

    async def flaky(ws, task_id, reason):
        if task_id == bad:
            raise RuntimeError("temporal hiccup")

    monkeypatch.setattr(acts.temporal_client, "start_task_workflow", flaky)
    with pytest.raises(RuntimeError):
        await _run([t1])
    assert (await repo.get_trigger(WS, t1))["routedTo"] == {"taskId": good}
    await acts.apply_fallback(WS, [t1])
    assert world["posts"][-1]["text"] == router_prompt.PARTIAL
