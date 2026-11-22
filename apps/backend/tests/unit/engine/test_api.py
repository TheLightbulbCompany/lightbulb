"""The engine's operations for products (`core.engine.api`, engine-service plan Task 4), against the moved bodies.

The `*_base` tests drive the entry points products call today (`caller_turn`, `create_human_message`,
the answer and action routes) and pin what they store: they pass on the code before the move and
after it. The others call `core.engine.api` and assert the same ids, rows and answers, so a call or meeting that spans
the deploy replays rather than doubles."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from moto import mock_aws

from core import dynamodb, tasks_schema as ts
from core.auth import AuthContext
from core.engine import api as engine
from core.engine.contract import EngineError, OpenIn, Principal as Who, StartIn as TaskStart
from core.repositories import messaging_repo, questions_repo, tasks_repo
from core.services import admission, messaging, questions, task_conversation, tasks_access
from core.temporal import client as temporal_client

WS = OWNER = "user_owner"  # the tenant is one person
FRONT = "a0000000-0000-4000-8000-000000000001"
CALL = "c0000000-0000-4000-8000-000000000001"
DM = "d0000000-0000-4000-8000-000000000001"
CALLER = "+15555550123"
TASK = "7e8bf6dd-9efb-52b8-ad56-6ed3f06754c0"


@pytest.fixture
def tables(monkeypatch):
    """The messaging and tasks tables, both real (moto): the rows a live call holds are what is asserted."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        key = [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}]
        resource.create_table(
            TableName="test-messaging",
            KeySchema=key,
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("pk", "sk")],
            BillingMode="PAY_PER_REQUEST",
        )
        attrs = {"pk", "sk"} | {a for g in ts.GSIS for a in (g.pk_attr, g.sk_attr)}
        resource.create_table(
            TableName=dynamodb.table_name("tasks"),
            KeySchema=key,
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in sorted(attrs)],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": g.name,
                    "KeySchema": [
                        {"AttributeName": g.pk_attr, "KeyType": "HASH"},
                        {"AttributeName": g.sk_attr, "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
                for g in ts.GSIS
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        monkeypatch.setattr(messaging_repo.settings, "MESSAGING_TABLE", "test-messaging")
        # the Router signal after a commit: the outbox row is what is asserted, never Temporal
        monkeypatch.setattr(admission, "_admit_trigger", AsyncMock())
        monkeypatch.setattr(admission, "_admit_message", AsyncMock())
        yield resource.Table("test-messaging")


def _human(uid, role="owner", name="Sam"):
    return {"kind": "human", "id": uid, "displayName": name, "mentionHandle": None, "role": role, "listenMode": None}


def _agent():
    return {
        "kind": "agent",
        "id": FRONT,
        "displayName": "Lightbulb",
        "mentionHandle": "lightbulb",
        "role": "member",
        "listenMode": "active",
    }


async def _room(room_id, kind, extra=None, members=None):
    await messaging_repo.create_room(
        owner_id=WS,
        room_id=room_id,
        client_room_id=f"c:{room_id}",
        creator_user_id=OWNER,
        name="r",
        kind=kind,
        members=members or [_human(OWNER), _agent()],
        req_hash=messaging_repo.request_hash(room_id),
        extra=extra or {},
    )


async def _agent_line(room_id, text, key, attrs=None):
    msg, _ = await messaging_repo.create_message(
        room_id=room_id, owner_id=WS, idemp_pk=messaging_repo.owner_pk(WS), idemp_sk=key, req_hash=key,
        author={"kind": "agent", "id": FRONT, "displayName": "Lightbulb"}, text=text, mentions=[],
        reply_to_message_id=None, thread_root_message_id=None, run_id=None, dispatch_targets=[],
        source_agent_id=None, client_message_id=None, author_member_check=False, attrs=attrs,
    )  # fmt: skip
    return msg


def _rows(table, prefix):
    return [i for i in table.scan()["Items"] if str(i["sk"]).startswith(prefix)]


def _receipts(table):
    """The message idempotency receipts (a room's create leaves its own)."""
    return [i for i in _rows(table, "IDEMP#") if "messageId" in i]


def _stable(row: dict) -> dict:
    """A stored row without its clock fields."""
    return {k: v for k, v in row.items() if k not in {"createdAt", "updatedAt", "receivedAt", "expiresAt"}}


# --- a caller's line on a phone call (contact) --------------------------------------------------------------------


@pytest.fixture
def call(tables, monkeypatch):
    from routers import voice_phone

    monkeypatch.setattr(voice_phone, "agent_config", AsyncMock(return_value={}))
    monkeypatch.setattr(voice_phone, "call_mode", lambda cfg, c: "live")
    return SimpleNamespace(vp=voice_phone, table=tables)


async def _call_room():
    await _room(
        CALL,
        "call",
        extra={"call": {"status": "live", "caller": CALLER, "callerName": ""}, "hidden": True, "ownerUserId": OWNER},
    )


def _expected_trigger(n: int, text: str, message_id: str) -> dict:
    from routers.voice_phone import CALLER_HINT

    return {
        "triggerId": f"voice:{CALL}:turn-{n}",
        "source": "voice",
        "admission": "respond",
        "lane": "shared",
        "actorUserId": OWNER,
        "ref": {
            "roomId": CALL,
            "messageId": message_id,
            "callId": CALL,
            "origin": "phone_contact",
            "from": f"Caller {CALLER}",
            "text": f"{text}\n\n{CALLER_HINT}",
            "deliverRoomId": CALL,
        },
    }


async def test_caller_turn_ids_base(call):
    """Pinned before the move: the caller's message, its agent idempotency receipt keyed `call-turn:{call}:{n}` with
    the (call, n, text) hash, and one `voice:{call}:turn-{n}` trigger with its one admission row. A retried turn is
    the same message and the same trigger."""
    await _call_room()
    text = "Open Sunday?"
    out = await call.vp.caller_turn(CALL, call.vp.TurnIn(n=0, text=text), WS)
    again = await call.vp.caller_turn(CALL, call.vp.TurnIn(n=0, text=text), WS)
    assert out == again == {"messageId": out["messageId"], "triggerId": f"voice:{CALL}:turn-0"}
    msg = await messaging_repo.get_message_by_id(CALL, out["messageId"])
    assert (msg["messageSeq"], msg["silent"], msg["via"], msg["clientMessageId"], msg["dispatchTargets"]) == (
        1, True, "voice", None, [],
    )  # fmt: skip
    assert msg["author"] == {"kind": "human", "id": f"caller:{CALL}", "displayName": f"Caller {CALLER}"}
    receipt = await messaging_repo._get(messaging_repo.owner_pk(WS), f"IDEMP#AGENT#call-turn:{CALL}:0")
    assert receipt["requestHash"] == messaging_repo.request_hash(CALL, 0, text)
    assert (receipt["messageId"], receipt["messageSeq"]) == (out["messageId"], 1)
    trig = await tasks_repo.get_trigger(WS, out["triggerId"])
    assert {k: trig[k] for k in _expected_trigger(0, text, "")} == _expected_trigger(0, text, out["messageId"])
    assert trig["version"] == 1
    assert [r["sk"] for r in _rows(call.table, "WORK#ADMIT#")] == [
        messaging_repo.work_admit_trigger_sk(out["triggerId"])
    ]


async def _open_thread_world(call, monkeypatch, blocked=None):
    await _call_room()
    await _agent_line(CALL, "Vera is checking Saturday.", "ack-1", {"taskRefs": [{"taskId": TASK, "role": "started"}]})
    task = {"taskId": TASK, "origin": "phone_contact", "status": "idle", "audience": {"kind": "room", "roomId": CALL}}
    monkeypatch.setattr(tasks_repo, "get_task", AsyncMock(return_value=task))
    monkeypatch.setattr(task_conversation, "gate_input", AsyncMock(return_value=blocked))
    monkeypatch.setattr(task_conversation, "post_as_lightbulb", AsyncMock(return_value="gate-line"))
    monkeypatch.setattr(admission, "_to_task", AsyncMock())
    monkeypatch.setattr(admission, "enqueue_trigger", AsyncMock())


async def test_caller_turn_open_thread_base(call, monkeypatch):
    """The call's open caller thread takes the line: the trigger is written routed to it and the task is signalled."""
    await _open_thread_world(call, monkeypatch)
    out = await call.vp.caller_turn(CALL, call.vp.TurnIn(n=1, text="The first time works."), WS)
    assert out == {"messageId": out["messageId"], "triggerId": f"voice:{CALL}:turn-1", "taskId": TASK}
    trig = await tasks_repo.get_trigger(WS, out["triggerId"])
    assert trig["routedTo"] == {"taskId": TASK}
    assert {k: trig[k] for k in _expected_trigger(1, "", "")} == _expected_trigger(
        1, "The first time works.", out["messageId"]
    )
    admission._to_task.assert_awaited_once_with(WS, TASK, out["triggerId"])
    admission.enqueue_trigger.assert_not_awaited()
    task_conversation.post_as_lightbulb.assert_not_awaited()


async def test_caller_turn_gate_blocked_base(call, monkeypatch):
    """The open thread's gate refuses: its line is posted in the call room in reply, and no task is signalled."""
    await _open_thread_world(call, monkeypatch, blocked="Paused for now.")
    out = await call.vp.caller_turn(CALL, call.vp.TurnIn(n=1, text="Hello?"), WS)
    assert out == {"messageId": out["messageId"], "triggerId": f"voice:{CALL}:turn-1"}
    task_conversation.post_as_lightbulb.assert_awaited_once_with(
        WS,
        CALL,
        text="Paused for now.",
        event_id=f"task-gate:{TASK}:voice:{CALL}:turn-1",
        remember=False,
        in_reply_to=out["messageId"],
        attrs={"voiceWaiting": False},
    )
    admission._to_task.assert_not_awaited()


def _contact_line(n: int, text: str):
    from core.engine.contract import LineIn, Principal
    from routers.voice_phone import CALLER_HINT

    who = f"Caller {CALLER}"
    return LineIn(
        tenant_id=WS,
        conversation_id=CALL,
        principal=Principal(kind="contact", id=f"caller:{CALL}", display_name=who),
        text=text,
        idempotency_key=f"call-turn:{CALL}:{n}",
        turn=n,
        via="voice",
        requester_user_id=OWNER,
        trigger_key=f"turn-{n}",
        hint=CALLER_HINT,
        origin="phone_contact",
        product_ref={"callId": CALL, "from": who},
    )


async def test_contact_line_ids_identical(call, monkeypatch):
    """A turn written by today's `caller_turn` and retried through `append_line` (a call spanning the deploy) is the
    same message, the same receipt and one trigger; the open thread and its gate behave as `caller_turn`'s."""
    from core.engine import api

    await _call_room()
    text = "Open Sunday?"
    before = await call.vp.caller_turn(CALL, call.vp.TurnIn(n=0, text=text), WS)
    out = await api.append_line(_contact_line(0, text))
    assert (out.message_id, out.seq, out.trigger_id, out.task_id) == (
        before["messageId"], 1, f"voice:{CALL}:turn-0", None,
    )  # fmt: skip
    assert len(_receipts(call.table)) == 1
    assert (await tasks_repo.get_trigger(WS, out.trigger_id))["version"] == 1
    assert len(_rows(call.table, "WORK#ADMIT#")) == 1

    await _open_thread_world(call, monkeypatch)  # (the room exists: _room replays)
    out = await api.append_line(_contact_line(1, "The first time works."))
    assert out.task_id == TASK and (await tasks_repo.get_trigger(WS, out.trigger_id))["routedTo"] == {"taskId": TASK}
    admission._to_task.assert_awaited_once_with(WS, TASK, out.trigger_id)

    task_conversation.gate_input.return_value = "Paused for now."
    out = await api.append_line(_contact_line(2, "Hello?"))
    assert out.task_id is None and admission._to_task.await_count == 1
    assert task_conversation.post_as_lightbulb.await_args.kwargs["event_id"] == f"task-gate:{TASK}:{out.trigger_id}"


# --- a member's line (the orb, owner phone mode, a Meet ask) ------------------------------------------------------


@pytest.fixture
async def dm(tables, monkeypatch):
    monkeypatch.setattr(messaging, "_router_room", AsyncMock(return_value=True))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Sam"))
    monkeypatch.setattr(messaging, "_publish_message_created_soon", lambda item: None)
    monkeypatch.setattr(messaging, "_touch_last_interactive_quietly", AsyncMock())
    monkeypatch.setattr("core.services.onboarding_brief.capture_first_workspace_message", AsyncMock())
    await _room(DM, "dm")
    return tables


async def _base_member_line(key: str, text: str, answered: bool = False):
    """What `voice_owner.turn` and `voice_phone._owner_turn` post today."""
    return await messaging.create_human_message(
        AuthContext(user_id=OWNER, org_id=WS),
        DM,
        messaging.CreateMessageRequest(
            client_message_id=uuid.uuid5(uuid.NAMESPACE_URL, key),
            text=text,
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
        via="voice",
        **({"wake": False} if answered else {}),
    )


def _member_line(key: str, text: str, answers=None):
    from core.engine.contract import LineIn, Principal

    return LineIn(
        tenant_id=WS,
        conversation_id=DM,
        principal=Principal(kind="member", id=OWNER),
        text=text,
        idempotency_key=key,
        via="voice",
        answers=answers,
    )


async def test_member_line_base(dm):
    """Pinned before the move: a voice line in a Router DM is one message keyed on the uuid5 of its key, with one
    `WORK#ADMIT#` row; the same key again is the same message."""
    key = "voice-turn:run-1"
    first = await _base_member_line(key, "What's on today?")
    again = await _base_member_line(key, "What's on today?")
    assert (str(again.message_id), first.message_seq) == (str(first.message_id), 1)
    cmid = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
    assert [r["sk"] for r in _receipts(dm)] == [messaging_repo.human_idemp_sk(OWNER, cmid)]
    assert [r["sk"] for r in _rows(dm, "WORK#ADMIT#")] == [messaging_repo.work_admit_sk(1)]
    msg = await messaging_repo.get_message_by_id(DM, str(first.message_id))
    assert (msg["clientMessageId"], msg["via"]) == (cmid, "voice")


async def test_member_line_ids_identical(dm):
    """`append_line` for a member writes what the base sequence writes (client id, receipt, seq), and replaying a
    key the base sequence already used returns that message and writes no second admit row."""
    from core.engine import api

    fresh = await api.append_line(_member_line("voice-turn:run-9", "Hi"))
    cmid = str(uuid.uuid5(uuid.NAMESPACE_URL, "voice-turn:run-9"))
    assert (await messaging_repo.get_message_by_id(DM, fresh.message_id))["clientMessageId"] == cmid
    assert [r["sk"] for r in _receipts(dm)] == [messaging_repo.human_idemp_sk(OWNER, cmid)]
    assert (fresh.seq, fresh.answered, fresh.trigger_id, fresh.task_id) == (1, False, None, None)

    base = await _base_member_line("voice-turn:run-1", "What's on today?")
    out = await api.append_line(_member_line("voice-turn:run-1", "What's on today?"))
    assert (out.message_id, out.seq) == (str(base.message_id), base.message_seq) == (out.message_id, 2)
    assert [r["sk"] for r in _rows(dm, "WORK#ADMIT#")] == [
        messaging_repo.work_admit_sk(1),
        messaging_repo.work_admit_sk(2),
    ]


async def test_outbox_row_in_message_transaction(dm, monkeypatch):
    """The `WORK#ADMIT#` row rides in the message's own transaction: a cancelled transaction leaves neither."""
    from core.engine import api

    real, seen = messaging_repo._transact, []

    async def spy(items):
        seen.append(items)
        await real(items)

    monkeypatch.setattr(messaging_repo, "_transact", spy)
    out = await api.append_line(_member_line("k-1", "Hi"))
    [items] = seen
    puts = [i["Put"]["Item"]["sk"] for i in items if "Put" in i]
    assert messaging_repo.msg_sk(out.seq) in puts and messaging_repo.work_admit_sk(out.seq) in puts

    async def cancelled(items):  # one more condition that fails: DynamoDB cancels every item of the transaction
        check = {
            "TableName": "test-messaging",
            "Key": {"pk": "x", "sk": "y"},
            "ConditionExpression": "attribute_exists(sk)",
        }
        await real([*items, {"ConditionCheck": check}])

    monkeypatch.setattr(messaging_repo, "_transact", cancelled)
    monkeypatch.setattr(messaging_repo, "_SEQ_RETRY_ATTEMPTS", 1)
    with pytest.raises(Exception):  # noqa: B017 -- whichever error the send maps the cancellation to
        await api.append_line(_member_line("k-2", "Hello"))
    assert messaging_repo.msg_sk(out.seq + 1) not in [r["sk"] for r in dm.scan()["Items"]]
    assert [r["sk"] for r in _rows(dm, "WORK#ADMIT#")] == [messaging_repo.work_admit_sk(out.seq)]


async def test_said_answer_wakes_nobody(dm, monkeypatch):
    """`answers` names the Worker question this room was just told: the line answers it through the card's path
    and is stored waking nobody (no dispatch target, no admit row)."""
    from core.engine import api
    from core.engine.contract import QuestionRef

    await _agent_line(DM, "Which day?", "q-line", {"kind": "question", "taskRefs": [{"taskId": TASK, "qid": "q-1"}]})
    submitted = AsyncMock()
    monkeypatch.setattr(api, "_submit_answer", submitted)
    out = await api.append_line(_member_line("voice-turn:run-2", "Tuesday.", QuestionRef(task_id=TASK, qid="q-1")))
    assert out.answered is True
    ws, task_id, qid, me, body = submitted.await_args.args
    assert (ws, task_id, qid, me, body.option_id, body.text) == (WS, TASK, "q-1", OWNER, "answer", "Tuesday.")
    assert body.client_answer_id == uuid.uuid5(uuid.NAMESPACE_URL, "voice-turn:run-2").hex
    msg = await messaging_repo.get_message_by_id(DM, out.message_id)
    assert msg["dispatchTargets"] == [] and _rows(dm, "WORK#ADMIT#") == []


# --- a scripted task, a source event, the voice's lines ------------------------------------------------------------


def _voice_start_expected(lane: str) -> dict:
    start = {
        "kind": "start_task",
        "triggerIds": ["voice:call-9:run-9"],
        "title": "Find a slot",
        "brief": "Caller asks.",
        "specialist": None,
        "ack": "On it: Find a slot",
        "visibility": "room",
    }
    return {
        "triggerId": "voice:call-9:run-9",
        "source": "voice",
        "admission": "respond",
        "lane": lane,
        "actorUserId": OWNER,
        "ref": {
            "deliverRoomId": DM,
            "callId": "call-9",
            "utteranceId": "run-9",
            "agentId": FRONT,
            "taskId": ts.router_task_id("voice:call-9:run-9", 0, 0),
            "start": start,
            "origin": "phone_contact",
        },
    }


def _picked(row: dict, expected: dict) -> dict:
    out = {k: row.get(k) for k in expected}
    out["ref"] = {**out["ref"], "start": {k: out["ref"]["start"].get(k) for k in expected["ref"]["start"]}}
    return out


def _notes_args():
    from core.voice.meetings import meeting_notes

    trigger_id, title, meeting = f"{meeting_notes.NOTES_PREFIX}room-1:3", "Weekly sync", {}
    return meeting_notes, trigger_id, title, meeting


async def _stored_trigger(tid):
    return _stable(await tasks_repo.get_trigger(WS, tid))


def _drop_trigger(tid):
    """Forget a trigger the base path wrote, so the operation writes it afresh and the two rows can be compared."""
    dynamodb._dynamodb_resource.Table(dynamodb.table_name("tasks")).delete_item(
        Key={"pk": ts.ws_pk(WS), "sk": ts.trigger_sk(tid)}
    )


async def test_start_task_trigger_and_task_ids(tables):
    """Voice (lane auto in a DM = the requester's lane) and meeting-notes shapes: the trigger row `start_task`
    stores is the one today's code stores."""
    from core.engine import api
    from core.engine.contract import Principal, StartIn

    await _room(DM, "dm")
    out = await api.start_task(StartIn(
        tenant_id=WS, principal=Principal(kind="member", id=OWNER), conversation_id=DM,
        trigger_id="voice:call-9:run-9", source="voice", lane="auto", title="Find a slot", brief="Caller asks.",
        origin="phone_contact", product_ref={"callId": "call-9", "utteranceId": "run-9", "agentId": FRONT},
    ))  # fmt: skip
    expected = _voice_start_expected(ts.router_lane_key(OWNER))
    assert (out.trigger_id, out.task_id, out.created, out.qid) == (
        "voice:call-9:run-9", expected["ref"]["taskId"], True, None,
    )  # fmt: skip
    assert _picked(await tasks_repo.get_trigger(WS, out.trigger_id), expected) == expected

    meeting_notes, tid, title, meeting = _notes_args()
    await _room("out-room", "channel")
    task_id = ts.router_task_id(tid, 0, 0)
    # the trigger the notes start stored before it moved onto start_task (meeting_notes._notes_trigger)
    now = dynamodb.utc_now_iso()
    old = ts.StartTask(
        kind="start_task", trigger_ids=[tid], title=f"Meeting notes: {title}"[:200],
        brief=meeting_notes._notes_brief(title, meeting), specialist=FRONT, ack=f"Writing the notes for {title}.",
        visibility="room",
    )  # fmt: skip
    await admission.enqueue_trigger(WS, ts.Trigger(
        ws=WS, trigger_id=tid, source="webhook", admission="respond", lane="shared", actor_user_id=OWNER,
        received_at=now, created_at=now, updated_at=now,
        ref={"taskId": task_id, "meetingNotes": {"requestId": tid}, "start": old.model_dump(by_alias=True),
             "confirmAll": True},
    ), room_id="out-room")  # fmt: skip
    base = await _stored_trigger(tid)
    _drop_trigger(tid)
    out = await api.start_task(meeting_notes._notes_start(WS, tid, "out-room", title, FRONT, OWNER, meeting))
    assert (out.task_id, out.created) == (task_id, True)
    assert out.task_id == api.start_task_id(tid)
    assert await _stored_trigger(tid) == base

    ask = {"qid": "q-call", "kind": "question"}
    out = await api.start_task(StartIn(
        tenant_id=WS, principal=Principal(kind="member", id=OWNER), conversation_id=DM, trigger_id="voice:c:r",
        source="voice", title="t", brief="b", ask=ask,
    ))  # fmt: skip
    assert out.qid == "q-call" and (await tasks_repo.get_trigger(WS, "voice:c:r"))["ref"]["ask"] == ask


async def test_submit_event_candidate(tables):
    """A meeting's follow-ups event: the trigger `submit_event` stores is the one `meeting_followups.enqueue` does."""
    from core.dynamodb import utc_now_iso
    from core.engine import api
    from core.engine.contract import EventIn, Principal

    tid, text = "meeting-followups:room-1:3", "Meeting completed. Review the original meeting source."
    followups = {"requestId": "meeting-notes:room-1:3", "occurrence": 3, "meetingRoomId": "room-1"}
    await _room("room-1", "channel")
    now = utc_now_iso()
    await admission.enqueue_trigger(WS, ts.Trigger(
        ws=WS, created_at=now, updated_at=now, trigger_id=tid, source="webhook", admission="candidate",
        lane="shared", actor_user_id=OWNER, received_at=now,
        ref={"roomId": "room-1", "origin": "meeting", "confirmAll": True, "meetingFollowups": followups, "text": text},
    ))  # fmt: skip
    base = await _stored_trigger(tid)
    _drop_trigger(tid)
    event = EventIn(
        tenant_id=WS, trigger_id=tid, source="webhook", admission="candidate",
        principal=Principal(kind="member", id=OWNER), text=text, conversation_id="room-1", origin="meeting",
        confirm_all=True, product_ref={"meetingFollowups": followups},
    )  # fmt: skip
    out = await api.submit_event(event)
    assert (out.trigger_id, out.created) == (tid, True)
    assert await _stored_trigger(tid) == base
    assert (await api.submit_event(event)).created is False


# --- a conversation that is not the tenant's ------------------------------------------------------------------


def _foreign(room_id, kind="dm"):
    """A room of another workspace."""
    return messaging_repo.create_room(
        owner_id="org_other", room_id=room_id, client_room_id=f"c:{room_id}", creator_user_id="user_other",
        name="r", kind=kind, members=[_human("user_other"), _agent()], req_hash=messaging_repo.request_hash(room_id),
    )  # fmt: skip


FOREIGN = "f0000000-0000-4000-8000-000000000001"


async def _refused(call):
    from core.engine.contract import EngineError

    with pytest.raises(EngineError) as err:
        await call
    assert (err.value.status, err.value.detail) == (404, {"code": "not_found"})


async def test_start_task_refuses_foreign_conversation(tables, monkeypatch):
    """One tenant's task never lands in another's room: refused before anything is written."""
    from core.engine import api
    from core.engine.contract import Principal, StartIn

    await _foreign(FOREIGN)
    enqueue = AsyncMock()
    monkeypatch.setattr(admission, "enqueue_trigger", enqueue)
    req = StartIn(tenant_id=WS, principal=Principal(kind="member", id=OWNER), conversation_id=FOREIGN,
                  trigger_id="voice:c:r", source="voice", lane="shared", title="t", brief="b")  # fmt: skip
    await _refused(api.start_task(req))
    await _refused(api.start_task(req.model_copy(update={"conversation_id": "no-such-room"})))
    enqueue.assert_not_awaited()


@pytest.mark.parametrize("kind", ["member", "contact", "system"])
async def test_append_line_refuses_foreign_conversation(tables, monkeypatch, kind):
    from core.engine import api

    await _foreign(FOREIGN)
    for name in ("create_message", "_transact"):
        monkeypatch.setattr(messaging_repo, name, AsyncMock(side_effect=AssertionError("wrote")))
    monkeypatch.setattr(task_conversation, "post_as_lightbulb", AsyncMock(side_effect=AssertionError("posted")))
    line = (_member_line("k", "hi") if kind == "member" else _contact_line(0, "hi")).model_copy(
        update={"conversation_id": FOREIGN}
    )
    if kind == "system":
        line = line.model_copy(update={"principal": line.principal.model_copy(update={"kind": "system"})})
    await _refused(api.append_line(line))


async def test_submit_event_refuses_foreign_conversation(tables, monkeypatch):
    from core.engine import api
    from core.engine.contract import EventIn

    await _foreign(FOREIGN, "channel")
    enqueue = AsyncMock()
    monkeypatch.setattr(admission, "enqueue_trigger", enqueue)
    await _refused(api.submit_event(EventIn(
        tenant_id=WS, trigger_id="meeting-followups:x:1", source="webhook", admission="candidate", principal=None,
        text="t", conversation_id=FOREIGN,
    )))  # fmt: skip
    enqueue.assert_not_awaited()


async def test_warm_skips_a_foreign_conversation(tables, monkeypatch):
    """Fire-and-forget, so nothing to raise to: a foreign room wakes nothing; an own room wakes as before."""
    import asyncio

    from core.engine import api

    await _foreign(FOREIGN)
    await _room(DM, "dm")
    calls, fired = [], []
    monkeypatch.setattr(api.container_rpc, "call", AsyncMock(side_effect=lambda *a, **k: calls.append(a[:2])))
    monkeypatch.setattr(api, "_fire", lambda coro: fired.append(asyncio.ensure_future(coro)))
    api.warm(WS, [FRONT], FOREIGN)
    await asyncio.gather(*fired)
    assert calls == []
    api.warm(WS, [FRONT], DM)
    await asyncio.gather(*fired)
    assert calls == [(WS, "health"), (WS, "profile.warm")]


@pytest.mark.parametrize("missing", ["via", "turn"])
async def test_contact_line_needs_via_and_turn(call, missing):
    from core.engine import api
    from core.engine.contract import EngineError

    await _call_room()
    with pytest.raises(EngineError) as err:
        await api.append_line(_contact_line(0, "hi").model_copy(update={missing: None}))
    assert (err.value.status, err.value.detail) == (400, {"code": "invalid_line", "missing": [missing]})
    assert _receipts(call.table) == []


async def test_said_answer_refused_by_a_product_hook_is_a_message(dm, monkeypatch):
    """As at base: an HTTPException from inside the answer path (the call's product hook: `call_not_found`) leaves
    the line a message to the Router, not an error."""
    from fastapi import HTTPException

    from core.engine import api
    from core.engine.contract import QuestionRef

    await _agent_line(DM, "Which day?", "q-line", {"kind": "question", "taskRefs": [{"taskId": TASK, "qid": "q-1"}]})
    monkeypatch.setattr(api, "_submit_answer", AsyncMock(side_effect=HTTPException(404, "call_not_found")))
    ref = QuestionRef(task_id=TASK, qid="q-1")
    assert await api._answer_said_question(WS, DM, OWNER, ref, "Tuesday.", "k") is False
    out = await api.append_line(_member_line("voice-turn:run-3", "Tuesday.", ref))
    assert out.answered is False and len(_rows(dm, "WORK#ADMIT#")) == 1  # the line woke the Router


# --- answers and actions: the same status and body, raised as EngineError -----------------------------------------


class VersionConflict(Exception):
    pass


Q = {"qid": "q-1", "taskId": TASK, "state": "open", "options": [{"id": "approve", "label": "OK"}], "answerers": [OWNER]}

#: (code, status, how the world refuses, expected body)
ANSWER_ERRORS = [
    ("not_found", 404, {"task": None}, {"code": "not_found"}),
    ("not_open", 409, {"q": {**Q, "state": "answered", "answeredBy": "user_ada", "answeredAt": "t"}},
     {"code": "not_open", "state": "answered", "by": "user_ada", "at": "t"}),
    ("task_closed", 409, {"task": {"taskId": TASK, "status": "resolved"}}, {"code": "task_closed"}),
    ("not_answerer", 403, {"can": False}, {"code": "not_answerer"}),
    ("invalid_option", 400, {"option": "nope"}, {"code": "invalid_option"}),
    ("TEMPORAL_UNAVAILABLE", 503, {"update": temporal_client.TemporalUnavailable()},
     {"code": "TEMPORAL_UNAVAILABLE", "retryable": True}),
    ("stale_version", 409, {"update": VersionConflict("stale")}, {"code": "stale_version"}),
    ("answer_failed", 502, {"update": RuntimeError("boom")}, {"code": "answer_failed"}),
]  # fmt: skip


def _answer_world(monkeypatch, case: dict):
    monkeypatch.setattr(
        tasks_repo, "get_task", AsyncMock(return_value=case.get("task", {"taskId": TASK, "status": "idle"}))
    )
    monkeypatch.setattr(tasks_access, "can_view", AsyncMock(return_value=True))
    monkeypatch.setattr(questions_repo, "get_question", AsyncMock(return_value=case.get("q", Q)))
    monkeypatch.setattr(questions, "can_answer_now", AsyncMock(return_value=case.get("can", True)))
    monkeypatch.setattr(temporal_client, "update_task", AsyncMock(side_effect=case.get("update")))
    return case.get("option", "approve")


@pytest.mark.parametrize("code,status,case,body", ANSWER_ERRORS, ids=[c[0] for c in ANSWER_ERRORS])
async def test_answer_route_errors_base(monkeypatch, code, status, case, body):
    """Pinned before the move: each refusal of the answer route, status and JSON body."""
    from routers import questions as router_mod

    option = _answer_world(monkeypatch, case)
    app = FastAPI()
    app.include_router(router_mod.router, prefix="/api/v1")
    app.dependency_overrides[router_mod.person_only] = lambda: AuthContext(user_id=OWNER, org_id=WS)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/api/v1/tasks/{TASK}/questions/q-1/answer", json={"optionId": option, "clientAnswerId": "c1"}
        )
    assert (r.status_code, r.json()) == (status, {"detail": body})


@pytest.mark.parametrize("code,status,case,body", ANSWER_ERRORS, ids=[c[0] for c in ANSWER_ERRORS])
async def test_answer_errors_map(monkeypatch, code, status, case, body):
    from core.engine import api
    from core.engine.contract import AnswerIn, EngineError, Principal

    option = _answer_world(monkeypatch, case)
    with pytest.raises(EngineError) as err:
        await api.answer(AnswerIn(
            tenant_id=WS, task_id=TASK, qid="q-1", principal=Principal(kind="member", id=OWNER), option_id=option,
            client_answer_id="c1",
        ))  # fmt: skip
    assert (err.value.code, err.value.status, err.value.detail) == (code, status, body)


def _act_world(monkeypatch, version=3):
    tree = {"task": {"taskId": TASK, "version": version, "status": "idle", "triggerId": None}, "questions": []}
    monkeypatch.setattr(tasks_repo, "get_task_tree", AsyncMock(return_value=tree))
    monkeypatch.setattr(tasks_access, "can_view", AsyncMock(return_value=True))
    update = AsyncMock()
    monkeypatch.setattr(temporal_client, "update_task", update)
    return update


async def test_act_version_conflict_base(monkeypatch):
    from routers import tasks as router_mod

    update = _act_world(monkeypatch)
    app = FastAPI()
    app.include_router(router_mod.router, prefix="/api/v1")
    app.dependency_overrides[router_mod.get_current_user] = lambda: AuthContext(user_id=OWNER, org_id=WS)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(f"/api/v1/tasks/{TASK}/resolve", json={"version": 2})
    assert (r.status_code, r.json()) == (409, {"detail": {"code": "version_conflict", "version": 3}})
    update.assert_not_awaited()


async def test_act_version_conflict(monkeypatch):
    from core.engine import api
    from core.engine.contract import ActIn, EngineError, Principal

    update = _act_world(monkeypatch)
    me = Principal(kind="member", id=OWNER)
    with pytest.raises(EngineError) as err:
        await api.act(ActIn(tenant_id=WS, task_id=TASK, action="resolve", principal=me, version=2))
    assert (err.value.code, err.value.status, err.value.detail) == (
        "version_conflict", 409, {"code": "version_conflict", "version": 3},
    )  # fmt: skip
    update.assert_not_awaited()
    monkeypatch.setattr(api, "_mark_action", AsyncMock())
    monkeypatch.setattr(api, "_summary", AsyncMock(return_value={"id": TASK}))
    assert await api.act(ActIn(tenant_id=WS, task_id=TASK, action="stop", principal=me, version=3)) == {"id": TASK}
    ws, task_id, action, request = update.await_args.args
    assert (ws, task_id, action, request.by_user_id) == (WS, TASK, "stop", OWNER)


# --- annotate: a product's own fields on the conversation's record -------------------------------------------------


def _annotate(**kw):
    from core.engine.contract import AnnotateIn

    return AnnotateIn(tenant_id=WS, conversation_id=CALL, **kw)


def _spy_transact(monkeypatch) -> list[list[dict]]:
    real, seen = messaging_repo._transact, []

    async def spy(items):
        seen.append(items)
        return await real(items)

    monkeypatch.setattr(messaging_repo, "_transact", spy)
    return seen


def _same_request(items: list[dict], room_id: str) -> list[dict]:
    """A transaction with its room id and clock values blanked: two writes on twin rooms compare equal."""
    import json

    text = json.dumps(items, default=str).replace(room_id, "<room>")
    out = json.loads(text)
    for item in out:
        item["Update"]["ExpressionAttributeValues"].pop(":now", None)
    return out


async def test_annotate_rejects_engine_fields(tables):
    from core.engine import api
    from core.engine.contract import EngineError

    await _room(CALL, "call", extra={"call": {"status": "live"}})
    for field in ("name", "hidden", "frontAgentId", "archivedAt", "revision"):
        with pytest.raises(EngineError) as refused:
            await api.annotate(_annotate(set={"call": {}, field: "x"}))
        assert (refused.value.status, refused.value.detail) == (400, {"code": "not_product_field", "fields": [field]})
    assert (await messaging_repo.get_room(CALL))["call"] == {"status": "live"}  # nothing written


async def test_annotate_another_tenants_room_is_not_found(tables):
    from core.engine import api
    from core.engine.contract import AnnotateIn, EngineError

    await _room(CALL, "call", extra={"call": {"status": "live"}})
    for tenant, room in (("org_other", CALL), (WS, "no-such-room")):
        with pytest.raises(EngineError) as refused:
            await api.annotate(AnnotateIn(tenant_id=tenant, conversation_id=room, set={"call": {"status": "x"}}))
        assert refused.value.status == 404
    assert (await messaging_repo.get_room(CALL))["call"] == {"status": "live"}


async def test_annotate_set_is_patch_room_meta(tables, monkeypatch):
    """The same transaction `patch_room_meta` writes, CAS on the revision the caller read included."""
    from botocore.exceptions import ClientError

    from core.engine import api

    twin = "c0000000-0000-4000-8000-000000000002"
    for room in (CALL, twin):
        await _room(room, "call", extra={"call": {"status": "live"}})
    revision = (await messaging_repo.get_room(CALL))["revision"]
    seen = _spy_transact(monkeypatch)
    await messaging_repo.patch_room_meta(twin, {"call": {"status": "done"}}, expected_revision=revision)
    assert await api.annotate(_annotate(set={"call": {"status": "done"}}, expected_revision=revision)) is True
    assert _same_request(seen[1], CALL) == _same_request(seen[0], twin)
    assert (await messaging_repo.get_room(CALL))["call"] == {"status": "done"}
    with pytest.raises(ClientError) as lost:  # a writer since the caller's read: the caller retries, as today
        await api.annotate(_annotate(set={"call": {"status": "stale"}}, expected_revision=revision))
    assert messaging_repo.is_cancelled(lost.value)


async def test_annotate_if_absent(tables, monkeypatch):
    """`set_meeting_field_if_absent`'s claim: written once, False for the second writer; the same transaction."""
    from core.engine import api

    twin = "c0000000-0000-4000-8000-000000000002"
    for room in (CALL, twin):
        await _room(room, "channel", extra={"meeting": {"uid": "u1"}})
    seen = _spy_transact(monkeypatch)
    assert await messaging_repo.set_meeting_field_if_absent(twin, "ticketNonce", "n1", {"botClaimAt": 5})
    claim = {"meeting": {"ticketNonce": "n1", "botClaimAt": 5}}
    assert await api.annotate(_annotate(set=claim, if_absent="ticketNonce")) is True
    assert _same_request(seen[1], CALL) == _same_request(seen[0], twin)
    assert (await messaging_repo.get_room(CALL))["meeting"] == {"uid": "u1", "ticketNonce": "n1", "botClaimAt": 5}
    again = {"meeting": {"ticketNonce": "n2", "botClaimAt": 6}}
    assert await api.annotate(_annotate(set=again, if_absent="ticketNonce")) is False
    assert (await messaging_repo.get_room(CALL))["meeting"]["ticketNonce"] == "n1"


@pytest.mark.parametrize("field", ["callToken", "liveSlot", "ownerUserId"])
async def test_annotate_rejects_fields_the_store_would_drop(tables, field):
    """Product fields the record's writer would silently drop are refused, so `annotate` never answers True for
    nothing written."""
    from core.engine import api
    from core.engine.contract import EngineError

    await _room(CALL, "call", extra={"call": {"status": "live"}})
    with pytest.raises(EngineError) as refused:
        await api.annotate(_annotate(set={field: "x"}))
    assert (refused.value.status, refused.value.detail) == (400, {"code": "not_product_field", "fields": [field]})
    assert field not in await messaging_repo.get_room(CALL)


# --- conversations: products read and open them, the tenant's only (Task 6) ------------------------------------

OTHER = "user_other"


@pytest.fixture
def front(monkeypatch):
    monkeypatch.setattr(task_conversation, "_front_agent", AsyncMock(return_value=_agent()))


async def test_get_conversation_reads_the_record_and_its_members(tables):
    await _room(CALL, "call", extra={"call": {"status": "live"}, "callToken": "t", "hidden": True})
    conv = await engine.get_conversation(WS, CALL, with_members=True)
    assert (conv.tenant_id, conv.kind, conv.revision) == (WS, "call", 1)
    assert conv.annotations == {"call": {"status": "live"}, "callToken": "t"}  # product fields only, never `hidden`
    assert {m["id"] for m in conv.members} == {OWNER, FRONT}
    assert (await engine.get_conversation(WS, CALL)).members == []  # members only when asked


async def test_find_conversation_names_the_tenant_and_never_a_deleted_one(tables):
    await _room(CALL, "call", extra={"callToken": "t"})
    conv = await engine.find_conversation(CALL)
    assert (conv.tenant_id, conv.annotations["callToken"]) == (WS, "t")
    tables.update_item(
        Key={"pk": messaging_repo.room_pk(CALL), "sk": "META"},
        UpdateExpression="SET deletedAt = :d",
        ExpressionAttributeValues={":d": "2026-11-21T00:00:00Z"},
    )
    assert await engine.find_conversation(CALL) is None
    assert await engine.find_conversation(DM) is None


async def test_list_conversations_is_the_tenants_newest_of_a_kind(tables):
    await _room(CALL, "call", extra={"call": {"n": 1}})
    await _room(DM, "dm")
    convs = await engine.list_conversations(WS, ["call"], 10)
    assert [(c.conversation_id, c.annotations) for c in convs] == [(CALL, {"call": {"n": 1}})]
    assert (
        await engine.list_conversations(WS, ["call", "dm"], 1)
        and len(await engine.list_conversations(WS, ["dm"], 5)) == 1
    )


async def test_open_conversation_is_idempotent(tables, front):
    req = OpenIn(
        tenant_id=WS,
        conversation_id=CALL,
        client_key="call:room-1",
        kind="call",
        name="Call · Sam",
        creator=Who(kind="member", id=OWNER, display_name="Studio"),
        hidden=True,
        seat_front_agent=True,
        annotations={"call": {"status": "live"}, "callToken": "t1"},
    )
    first = await engine.open_conversation(req)
    again = await engine.open_conversation(req.model_copy(update={"name": "Call · Samantha", "annotations": {}}))
    assert first.front_agent_id == FRONT and again.conversation_id == first.conversation_id == CALL
    assert again.annotations["callToken"] == "t1"  # the record the first open made
    rows = [i for i in tables.scan()["Items"] if i["pk"] == messaging_repo.owner_pk(WS) and i["sk"].startswith("ROOM#")]
    assert len(rows) == 1  # one conversation
    meta = await messaging_repo.get_room(CALL)
    assert meta["hidden"] is True and meta["frontAgentId"] == FRONT
    assert {m["id"] for m in await messaging_repo.list_members(CALL)} == {OWNER, FRONT}


async def test_open_conversation_hashes_tenant_kind_and_client_key(tables, front):
    req = OpenIn(
        tenant_id=WS,
        conversation_id=CALL,
        client_key="call:room-1",
        kind="call",
        creator=Who(kind="member", id=OWNER),
    )
    await engine.open_conversation(req)
    receipt = await messaging_repo._get(
        messaging_repo.room_pk(CALL), messaging_repo.human_idemp_sk(OWNER, "call:room-1")
    )
    assert receipt["requestHash"] == messaging_repo.request_hash(WS, "call", "call:room-1")


async def test_open_conversation_refuses_another_tenants_conversation(tables, front):
    await _room(CALL, "call")  # the id is WS's
    req = OpenIn(
        tenant_id=OTHER, conversation_id=CALL, client_key="c:x", kind="call", creator=Who(kind="member", id=OWNER)
    )
    with pytest.raises(EngineError) as refused:
        await engine.open_conversation(req)
    assert refused.value.status == 404


async def test_open_conversation_without_a_front_agent_still_opens(tables, monkeypatch):
    monkeypatch.setattr(task_conversation, "_front_agent", AsyncMock(side_effect=task_conversation.NoFrontAgent("x")))
    req = OpenIn(
        tenant_id=WS, conversation_id=CALL, client_key="c:1", kind="call", creator=Who(kind="member", id=OWNER),
        seat_front_agent=True,
    )  # fmt: skip
    assert (await engine.open_conversation(req)).front_agent_id is None


async def test_open_conversation_refuses_engine_fields(tables):
    req = OpenIn(
        tenant_id=WS, conversation_id=CALL, client_key="c:1", kind="call", creator=Who(kind="member", id=OWNER),
        annotations={"frontAgentId": "a"},
    )  # fmt: skip
    with pytest.raises(EngineError) as e:
        await engine.open_conversation(req)
    assert e.value.code == "not_product_field"


async def test_read_lines_says_the_agent_lines_and_the_cursor(tables):
    await _room(CALL, "call")
    first = await _agent_line(CALL, "Hello.", "IDEMP#AGENT#k1")
    assert await engine.read_lines(WS, CALL, None) == {"messages": [], "after": int(first["messageSeq"])}
    out = await engine.read_lines(WS, CALL, 0)
    assert [m["text"] for m in out["messages"]] == ["Hello."] and out["after"] == int(first["messageSeq"])


async def test_read_history_is_the_newest_messages_oldest_first(tables):
    await _room(CALL, "call")
    for i in range(3):
        await _agent_line(CALL, f"line {i}", f"IDEMP#AGENT#h{i}")
    assert [r["text"] for r in await engine.read_history(WS, CALL, 2)] == ["line 1", "line 2"]


async def test_get_task_reads_only_the_tenants_task(tables):
    tasks_repo._table().put_item(Item={"pk": ts.ws_pk(WS), "sk": ts.task_sk("t-1"), "taskId": "t-1", "status": "idle"})
    assert (await engine.get_task(WS, "t-1"))["status"] == "idle"
    assert await engine.get_task(OTHER, "t-1") is None


async def test_get_front_agent_is_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(task_conversation, "front_agent_id", AsyncMock(return_value=FRONT))
    assert await engine.get_front_agent(WS) == FRONT
    monkeypatch.setattr(task_conversation, "front_agent_id", AsyncMock(side_effect=task_conversation.NoFrontAgent("x")))
    assert await engine.get_front_agent(WS) is None


async def test_start_task_names_the_opening_questions_qid(tables, monkeypatch):
    from core.services import admission

    await _room(CALL, "call")
    enq = AsyncMock(return_value=True)
    monkeypatch.setattr(admission, "enqueue_trigger", enq)
    req = TaskStart(
        tenant_id=WS, principal=Who(kind="member", id=OWNER), conversation_id=CALL, trigger_id="voice:c:r",
        source="voice", title="Answer", brief="b", ask={"kind": "question", "title": "Answer"},
    )  # fmt: skip
    out = await engine.start_task(req)
    assert out.qid == questions.qid_for("voice:c:r:ask") == enq.await_args.args[1].ref["ask"]["qid"]
    assert (await engine.start_task(req)).qid == out.qid  # a retried start opens the same Question
    given = await engine.start_task(req.model_copy(update={"ask": {**req.ask, "qid": "q-own"}}))
    assert given.qid == "q-own"


_READS = {
    "get_conversation": lambda tenant: engine.get_conversation(tenant, CALL, with_members=True),
    "list_conversations": lambda tenant: engine.list_conversations(tenant, ["call"], 10),
    "read_lines": lambda tenant: engine.read_lines(tenant, CALL, 0),
    "read_history": lambda tenant: engine.read_history(tenant, CALL, 50),
}


@pytest.mark.parametrize("op", sorted(_READS))
async def test_reads_refuse_another_tenants_conversation(tables, op):
    """The owner reads it; another tenant gets nothing (None or empty) or 404, never the conversation."""
    await _room(CALL, "call")
    await _agent_line(CALL, "Hi.", "IDEMP#AGENT#r1")
    assert await _READS[op](WS)
    try:
        assert not await _READS[op](OTHER)
    except EngineError as e:
        assert e.status == 404


@pytest.mark.parametrize("kind", ["call", "dm"])
async def test_a_call_room_cannot_be_deleted(tables, kind):
    """The one door that tombstones a room (`messaging.delete_room`, DELETE /messaging/rooms/{id}) deletes only a
    channel: a call's room, and the owner's DM with the assistant, are refused, so no Phone route meets a deleted
    conversation while the product owns it."""
    room = CALL if kind == "call" else DM
    await _room(room, kind)
    with pytest.raises(messaging.ApiProblem) as e:
        await messaging.delete_room(AuthContext(user_id=OWNER, org_id=WS, org_role="org:admin"), room)
    assert e.value.error.code == "ROOM_NOT_DELETABLE"
    assert not (await messaging_repo.get_room(room)).get("deletedAt")


# --- Meetings' operations -------------------------------------------------------------------------------------------

ENG = "e0000000-0000-4000-8000-000000000002"
MEET = "f0000000-0000-4000-8000-000000000001"


@pytest.fixture
def roster(monkeypatch):
    agents = [{"id": FRONT, "name": "Lightbulb"}, {"id": ENG, "name": "Engineering"}]
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=agents))
    monkeypatch.setattr(messaging, "_front_agent_id", AsyncMock(return_value=FRONT))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Sam"))
    return agents


def _meeting_open(**over):
    return OpenIn(**{"tenant_id": WS, "conversation_id": MEET, "client_key": "meeting:uid-1", "kind": "channel",
                     "name": "Weekly", "creator": Who(kind="member", id=OWNER), "hidden": True,
                     "annotations": {"meeting": {"uid": "uid-1"}}, **over})  # fmt: skip


async def test_a_channel_seats_the_creator_and_every_agent_on_mentions(tables, roster):
    conv = await engine.open_conversation(_meeting_open())
    assert (conv.kind, conv.front_agent_id, conv.annotations) == ("channel", FRONT, {"meeting": {"uid": "uid-1"}})
    members = {m["id"]: m for m in await messaging_repo.list_members(MEET)}
    assert (members[OWNER]["role"], members[OWNER]["displayName"]) == ("owner", "Sam")
    assert {(m["mentionHandle"], m["listenMode"]) for k, m in members.items() if k != OWNER} == {
        ("lightbulb", "mentions"), ("engineering", "mentions"),
    }  # fmt: skip
    assert (await messaging_repo.get_room(MEET))["hidden"] is True
    again = await engine.open_conversation(_meeting_open(name="Renamed", annotations={}))  # a replay
    assert again.annotations == {"meeting": {"uid": "uid-1"}}


async def test_a_deleted_channels_id_is_not_found(tables, roster):
    await engine.open_conversation(_meeting_open())
    meta = await messaging_repo.get_room(MEET)
    await messaging_repo.delete_room(meta=meta, deleted_by=OWNER, new_delivery_key="k2", live_events=[])
    with pytest.raises(EngineError) as gone:
        await engine.open_conversation(_meeting_open())
    assert gone.value.status == 404
    assert await engine.get_conversation(WS, MEET) is None


async def test_seat_roster_seats_only_the_agents_not_yet_in(tables, roster):
    await _room(MEET, "channel", members=[_human(OWNER), _agent()])
    await engine.seat_roster(WS, MEET)
    assert {m["id"] for m in await messaging_repo.list_members(MEET)} == {OWNER, FRONT, ENG}
    with pytest.raises(EngineError):
        await engine.seat_roster(OTHER, MEET)


async def test_a_notice_line_is_the_platforms_and_a_retry_is_the_first(tables):
    from core.engine.contract import LineIn

    await _room(MEET, "channel")
    line = LineIn(tenant_id=WS, conversation_id=MEET, principal=Who(kind="notice", id="lightbulb"),
                  text="**Weekly** moved.", idempotency_key="meeting:uid-1:1:x")  # fmt: skip
    first = await engine.append_line(line)
    again = await engine.append_line(line.model_copy(update={"text": "reworded"}))
    assert again.message_id == first.message_id
    [row] = await messaging_repo.list_messages_before(MEET, 10**19, 10)
    assert (row["author"]["kind"], row["text"]) == ("system", "**Weekly** moved.")
    await messaging_repo.patch_room_meta(MEET, {"archivedAt": "2026-11-01T00:00:00Z"})
    with pytest.raises(EngineError) as refused:
        await engine.append_line(line.model_copy(update={"idempotency_key": "k2"}))
    assert refused.value.status == 409
    with pytest.raises(EngineError):
        await engine.append_line(line.model_copy(update={"tenant_id": OTHER}))


async def test_get_conversation_as_a_member_answers_as_the_room_door(tables):
    await _room(MEET, "channel")
    conv = await engine.get_conversation(WS, MEET, with_members=True, member=OWNER)
    assert conv.conversation_id == MEET and {m["id"] for m in conv.members} == {OWNER, FRONT}
    with pytest.raises(EngineError) as no:
        await engine.get_conversation(WS, MEET, member="user_stranger")
    assert no.value.status in (403, 404) and no.value.detail["code"] in ("ROOM_FORBIDDEN", "ROOM_NOT_FOUND")
    await messaging_repo.patch_room_meta(MEET, {"archivedAt": "2026-11-01T00:00:00Z"})
    assert (await engine.get_conversation(WS, MEET)).archived is True


async def test_get_lines_and_conversation_tasks_are_the_tenants_only(tables):
    await _room(CALL, "call")
    msg = await _agent_line(CALL, "Hi.", "IDEMP#AGENT#r1")
    assert [r and r["text"] for r in await engine.get_lines(WS, CALL, [msg["messageId"], "unknown"])] == ["Hi.", None]
    assert await engine.list_conversation_tasks(WS, CALL) == []
    for op in (engine.get_lines(OTHER, CALL, [msg["messageId"]]), engine.list_conversation_tasks(OTHER, CALL)):
        with pytest.raises(EngineError):
            await op


def test_start_task_id_is_the_scripted_starts_task():
    assert engine.start_task_id("meeting-notes:r:1") == ts.router_task_id("meeting-notes:r:1", 0, 0)


async def test_include_deleted_reads_a_deleted_conversation_only_for_its_tenant_and_writes_still_refuse(tables):
    from core.engine.contract import AnnotateIn, LineIn

    await _room(MEET, "channel", extra={"meeting": {"status": "in_call"}})
    meta = await messaging_repo.get_room(MEET)
    await messaging_repo.delete_room(meta=meta, deleted_by=OWNER, new_delivery_key="k2", live_events=[])
    assert await engine.get_conversation(WS, MEET) is None and await engine.find_conversation(MEET) is None
    conv = await engine.get_conversation(WS, MEET, include_deleted=True, with_members=True)
    assert conv.deleted and conv.annotations == {"meeting": {"status": "in_call"}} and conv.members
    assert (await engine.find_conversation(MEET, include_deleted=True)).tenant_id == WS
    assert await engine.get_conversation(OTHER, MEET, include_deleted=True) is None
    line = LineIn(tenant_id=WS, conversation_id=MEET, principal=Who(kind="notice", id="lightbulb"), text="x",
                  idempotency_key="k")  # fmt: skip
    for write in (
        engine.append_line(line),
        engine.annotate(AnnotateIn(tenant_id=WS, conversation_id=MEET, set={"meeting": {}})),
        engine.seat_roster(WS, MEET),
        engine.start_task(
            TaskStart(
                tenant_id=WS,
                principal=None,
                conversation_id=MEET,
                trigger_id="t",
                source="webhook",
                title="t",
                brief="b",
            )
        ),  # fmt: skip
    ):
        with pytest.raises(EngineError) as refused:
            await write
        assert refused.value.status == 404
