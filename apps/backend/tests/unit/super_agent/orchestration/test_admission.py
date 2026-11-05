"""Admission (wave-2 O Task 6; contracts §10, §14.7). Review focus 1: a redelivered message admits once."""

import json
import uuid

import pytest
from boto3.dynamodb.types import TypeSerializer

from core import tasks_schema as ts
from core.repositories import messaging_repo
from core.repositories import tasks_repo as repo
from core.services import admission, openclaw_work
from core.temporal import client as temporal

from .test_tasks_repo import table  # noqa: F401

WS = "org_1"
ROOM = str(uuid.uuid4())
CEO = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FIN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _msg(seq=1, *, targets=(), root=None, author="sam"):
    return {
        "messageId": f"00000000-0000-4000-8000-{seq:012d}",
        "messageSeq": seq,
        "roomId": ROOM,
        "author": {"kind": "human", "id": author},
        "text": "pay acme",
        "dispatchTargets": list(targets),
        "threadRootMessageId": root,
        "createdAt": "2026-11-04T10:00:00Z",
    }


@pytest.fixture
def world(table, monkeypatch):  # noqa: F811
    w = {
        "meta": {"roomId": ROOM, "ownerId": WS, "kind": "channel", "name": "general"},
        "msgs": {},
        "router": [],
        "inputs": [],
        "jev": [],
        "jev_answer": {"to": "router", "specialist": FIN, "confidence": 0.9},
        "listen": "active",
        "on": True,
        "temporal_up": True,
        "front_seated": True,
        "gate": None,
        "posts": [],
    }

    async def flags(ws):
        return w["on"]

    async def get_room(room_id):
        return w["meta"]

    async def get_message(room_id, seq):
        return w["msgs"].get(seq)

    async def get_member(room_id, kind, mid):
        return {"id": CEO, "kind": "agent", "listenMode": w["listen"]} if mid == CEO and w["front_seated"] else None

    async def foreign(room_id, owner):
        return False

    async def front(ws):
        return CEO

    async def signal_router(ref):
        if not w["temporal_up"]:
            raise temporal.TemporalUnavailable("down")
        w["router"].append(ref)

    async def signal_input(ws, task_id, ref):
        w["inputs"].append((task_id, ref))

    async def route(meta, msg, members, history, specialists):
        w["jev"].append(specialists)
        return w["jev_answer"]

    async def nothing(*a, **k):
        return []

    async def gate(ws, task_id, trig):
        return w["gate"]

    async def post(ws, room_id, **kw):
        w["posts"].append((room_id, kw["text"], kw.get("thread_root")))
        return "m1"

    async def agents(ws):
        return [
            {"agentId": FIN, "name": "Finance", "listenMode": "active"},
            {"agentId": "obs", "name": "Observer", "listenMode": "observe"},
        ]

    monkeypatch.setattr(admission, "flags_for", flags)
    monkeypatch.setattr(messaging_repo, "get_room", get_room)
    monkeypatch.setattr(messaging_repo, "get_message", get_message)
    monkeypatch.setattr(messaging_repo, "get_member", get_member)
    monkeypatch.setattr(messaging_repo, "has_foreign_members", foreign)
    monkeypatch.setattr(messaging_repo, "list_members", nothing)
    monkeypatch.setattr(messaging_repo, "list_messages_before", nothing)
    monkeypatch.setattr("core.services.task_conversation.front_agent_id", front)
    monkeypatch.setattr("core.services.task_conversation.gate_input", gate)
    monkeypatch.setattr("core.services.task_conversation.post_as_lightbulb", post)
    monkeypatch.setattr("core.repositories.agent_registry_repo.list_agents", agents)
    monkeypatch.setattr(temporal, "signal_router", signal_router)
    monkeypatch.setattr(temporal, "signal_task_input", signal_input)
    monkeypatch.setattr(admission.jev, "route_for_router", route)
    return w


async def _admit(w, msg):
    w["msgs"][msg["messageSeq"]] = msg
    await admission.admit_pointer({"kind": "admit", "roomId": ROOM, "throughSeq": msg["messageSeq"]})
    return ts.trigger_id("chat", msg["messageId"])


@pytest.mark.parametrize(
    "meta,expected",
    [
        ({"kind": "channel"}, True),
        ({"kind": "dm"}, True),
        ({"kind": "channel", "task": {"taskId": "t"}}, True),
        ({"kind": "channel", "app": {"app_id": "a"}}, False),
        ({"kind": "channel", "session": {"id": "s"}}, False),
        ({"kind": "channel", "meeting": {"id": "m"}}, False),
    ],
)
async def test_router_room_exclusions(world, meta, expected):
    assert await admission.router_room({"roomId": ROOM, "ownerId": WS, **meta}, WS) is expected


async def test_router_room_needs_lightbulb_seated(world):
    """Audit #3: a specialist's DM (no Lightbulb seat) keeps today's path; Lightbulb could not answer there."""
    world["front_seated"] = False
    assert await admission.router_room({"roomId": ROOM, "ownerId": WS, "kind": "dm"}, WS) is False


async def test_router_room_off_when_switch_off(world):
    world["on"] = False
    assert await admission.router_room(world["meta"], WS) is False


def test_lane_for_dm_is_private():
    assert admission.lane_for({"kind": "dm"}, {"kind": "human", "id": "sam"}) == ts.router_lane_key("sam")
    assert admission.lane_for({"kind": "channel"}, {"kind": "human", "id": "sam"}) == "shared"


async def test_admit_explicit_ask_respond(world):
    tid = await _admit(world, _msg(targets=[{"agentId": CEO, "eventKind": "user_request"}]))
    assert world["router"] == [admission.TriggerRef(WS, tid, "shared", "respond", ROOM)]
    assert world["jev"] == []
    trig = await repo.get_trigger(WS, tid)
    assert trig["admission"] == "respond" and trig["actorUserId"] == "sam"


async def test_admit_message_ingests_memory_once(world, monkeypatch):
    seen = []

    async def ingest(ws, meta, msg, *, task=None):
        seen.append((ws, msg["messageId"]))
        return True

    monkeypatch.setattr(admission.room_memory, "ingest_message", ingest)
    msg = _msg(targets=[{"agentId": CEO, "eventKind": "user_request"}])
    await _admit(world, msg)
    assert seen == [(WS, msg["messageId"])]


async def test_specialist_mention_pins(world):
    tid = await _admit(world, _msg(targets=[{"agentId": FIN, "eventKind": "user_request"}]))
    assert (await repo.get_trigger(WS, tid))["ref"]["pinnedSpecialist"] == FIN


async def _subscribed_task(viewers=("sam",)) -> tuple[str, str]:
    task_id = ts.new_task_id()
    await repo.create_task(
        WS,
        {
            "taskId": task_id,
            "identifier": "LB-1",
            "title": "t",
            "status": "queued",
            "audience": {"kind": "person", "userIds": list(viewers)},
            "lane": "shared",
            "triggerId": "chat:x",
        },
    )
    root = str(uuid.uuid4())
    await repo.subscribe(WS, task_id, f"room:{ROOM}:{root}", created_by="router")
    return task_id, root


async def test_admit_thread_reply_passes_through(world):
    task_id, root = await _subscribed_task()
    tid = await _admit(world, _msg(root=root))
    assert world["inputs"] == [(task_id, admission.TaskInputRef(tid, "message"))]
    assert world["router"] == [] and world["jev"] == []


async def test_thread_reply_by_a_non_viewer_is_routed_not_passed(world):
    """Audit #16: a reply in a person-only task's thread by someone who cannot view it never reaches the task."""
    _, root = await _subscribed_task(viewers=("priya",))
    await _admit(world, _msg(root=root))
    assert world["inputs"] == [] and len(world["jev"]) == 1


async def test_pass_through_gated(world):
    """§14.10: a reply that would steer or re-run a task passes the chat gate first; blocked ⇒ the copy, no input."""
    world["gate"] = "Your plan is inactive."
    _, root = await _subscribed_task()
    await _admit(world, _msg(root=root))
    assert world["inputs"] == [] and world["router"] == []
    assert world["posts"] == [(ROOM, "Your plan is inactive.", root)]


async def test_admit_unaddressed_asks_jev_once(world):
    tid = await _admit(world, _msg())
    assert len(world["jev"]) == 1
    trig = await repo.get_trigger(WS, tid)
    assert trig["admission"] == "candidate" and trig["routedTo"] == {"specialist": FIN}


async def test_observe_specialist_never_candidate(world):
    await _admit(world, _msg())
    assert world["jev"][0] == {FIN: "Finance"}


async def test_jev_down_addressed_only(world):
    world["jev_answer"] = {"fallback": True}
    await _admit(world, _msg())
    assert world["router"] == []
    await _admit(world, _msg(2, targets=[{"agentId": CEO, "eventKind": "user_request"}]))
    assert len(world["router"]) == 1


async def test_lightbulb_mentions_only_room_skips_jev(world):
    world["listen"] = "mentions"
    await _admit(world, _msg())
    assert world["jev"] == [] and world["router"] == []


async def test_admit_redelivery_is_idempotent(world):
    msg = _msg(targets=[{"agentId": CEO, "eventKind": "user_request"}])
    tid = await _admit(world, msg)
    await _admit(world, msg)  # SQS redelivery / consumer retry
    trig = await repo.get_trigger(WS, tid)
    assert trig["version"] == 1  # one TRIG row
    # the Router dedups on trigger_id: both signals carry the same id, so one decision
    assert {r.trigger_id for r in world["router"]} == {tid}


async def test_admit_temporal_down_raises_workretry(world):
    world["temporal_up"] = False
    with pytest.raises(openclaw_work.WorkRetry):
        await _admit(world, _msg(targets=[{"agentId": CEO, "eventKind": "user_request"}]))


async def test_enqueue_trigger_dedup(world, monkeypatch):
    rows = []

    async def put_admit(ws, trigger_id):
        rows.append(trigger_id)

    monkeypatch.setattr(messaging_repo, "put_admit_trigger", put_admit)
    trig = ts.Trigger(
        ws=WS,
        trigger_id="sched:s1:100",
        source="schedule",
        admission="respond",
        lane="shared",
        received_at="2026-11-04T10:00:00Z",
        created_at="x",
        updated_at="x",
    )
    assert await admission.enqueue_trigger(WS, trig, room_id=ROOM) is True
    assert await admission.enqueue_trigger(WS, trig, room_id=ROOM) is False
    # The admit row is re-put on a replay (a MODIFY the outbox Pipe ignores), so a crash between the
    # TRIG put and the admit put never strands the trigger.
    assert rows == ["sched:s1:100", "sched:s1:100"]
    assert (await repo.get_trigger(WS, "sched:s1:100"))["ref"]["deliverRoomId"] == ROOM
    await admission.admit_pointer({"kind": "admit_trigger", "ownerId": WS, "triggerId": "sched:s1:100"})
    assert world["router"][-1] == admission.TriggerRef(WS, "sched:s1:100", "shared", "respond", ROOM)


def _stream_body(payload: dict) -> str:
    ser = TypeSerializer()
    return json.dumps({"dynamodb": {"NewImage": {"payload": {"M": {k: ser.serialize(v) for k, v in payload.items()}}}}})


def test_admit_pointers_deserialize():
    p = openclaw_work.deserialize_pointer(_stream_body({"kind": "admit", "roomId": ROOM, "throughSeq": 3}))
    assert p == {"kind": "admit", "roomId": ROOM, "throughSeq": 3}
    p = openclaw_work.deserialize_pointer(_stream_body({"kind": "admit_trigger", "ownerId": WS, "triggerId": "x"}))
    assert p["triggerId"] == "x"
    with pytest.raises(ValueError):
        openclaw_work.deserialize_pointer(_stream_body({"kind": "admit", "roomId": ROOM}))
