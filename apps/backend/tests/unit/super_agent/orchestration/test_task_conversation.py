"""The hidden task room and the one voice that posts results (wave-2 O Task 5; contracts §6)."""

from unittest.mock import AsyncMock
import uuid

import pytest

from core import tasks_schema as ts
from core.repositories import messaging_repo
from core.services import messaging, task_conversation as tc

WS = "org_1"
TID = "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"
CEO = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ORIGIN = str(uuid.uuid4())


def _task(audience):
    return {"pk": ts.ws_pk(WS), "taskId": TID, "title": "Pay the invoice", "audience": audience}


@pytest.fixture
def chat(monkeypatch):
    rooms: dict[str, dict] = {ORIGIN: {"roomId": ORIGIN, "ownerId": WS}}
    members: dict[str, list[dict]] = {ORIGIN: [{"id": CEO, "kind": "agent", "role": "member", "mentionHandle": "ceo"}]}
    posts: list[dict] = []

    async def default_agents(owner_id):
        return [{"kind": "agent", "id": CEO, "displayName": "CEO", "mentionHandle": "ceo", "listenMode": "mentions"}]

    async def create_room(**kw):
        rooms.setdefault(kw["room_id"], {"roomId": kw["room_id"], "ownerId": kw["owner_id"], **kw["extra"]})
        members.setdefault(kw["room_id"], [dict(m) for m in kw["members"]])
        return rooms[kw["room_id"]]

    async def get_room(room_id):
        return rooms.get(room_id)

    async def get_member(room_id, kind, member_id):
        return next((m for m in members.get(room_id, []) if (m["kind"], m["id"]) == (kind, member_id)), None)

    async def seat(owner_id, meta, user_id, **kw):
        members[meta["roomId"]].append({"kind": "human", "id": user_id})
        return meta

    async def create_agent_message(owner_id, req, *, attrs=None, in_reply_to=None):
        prior = next((p for p in posts if p["req"].event_id == req.event_id), None)
        if prior:
            return prior["receipt"]
        receipt = messaging.AgentMessageReceiptResponse(
            message_id=uuid.uuid4(), room_id=req.room_id, created_at="2026-11-04T10:00:00Z"
        )
        posts.append({"req": req, "receipt": receipt, "attrs": attrs, "in_reply_to": in_reply_to})
        return receipt

    async def create_message(**kw):
        key = (kw["room_id"], kw["idemp_sk"])
        prior = next((p for p in posts if p.get("key") == key), None)
        if prior:
            return prior, True
        posts.append({"key": key, "messageId": str(uuid.uuid4()), **kw})
        return posts[-1], False

    monkeypatch.setattr(tc.session_chat, "_default_agents", default_agents)
    monkeypatch.setattr(messaging_repo, "get_voice_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "core.repositories.workspace_repo.WorkspaceRepo.resolve_owner_row", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(messaging_repo, "list_members", AsyncMock(side_effect=lambda rid: members.get(rid, [])))

    monkeypatch.setattr(
        tc.agent_registry,
        "roster",
        AsyncMock(return_value=[{"id": CEO, "name": "CEO", "role": "ceo", "status": "active"}]),
    )

    monkeypatch.setattr(tc.session_chat, "_seat", seat)

    async def display_name(uid):
        return uid.title()

    monkeypatch.setattr(tc.messaging, "human_display_name", display_name)
    monkeypatch.setattr(messaging_repo, "create_room", create_room)
    monkeypatch.setattr(messaging_repo, "get_room", get_room)
    monkeypatch.setattr(messaging_repo, "get_member", get_member)
    monkeypatch.setattr(messaging_repo, "create_message", create_message)
    monkeypatch.setattr(tc.messaging, "create_agent_message", create_agent_message)
    monkeypatch.setattr(tc.messaging, "_publish_message_created_soon", lambda item: None)
    return {"rooms": rooms, "members": members, "posts": posts}


async def test_ensure_task_room_idempotent(chat):
    a = await tc.ensure_task_room(WS, _task({"kind": "workspace"}))
    b = await tc.ensure_task_room(WS, _task({"kind": "workspace"}))
    assert a == b == ts.task_room_id(TID)
    assert uuid.UUID(a)  # message routes take a uuid room id
    assert chat["rooms"][a]["hidden"] is True and chat["rooms"][a]["task"] == {"taskId": TID}


async def test_task_room_members_follow_audience(chat):
    room = await tc.ensure_task_room(WS, _task({"kind": "person", "userIds": ["sam"]}))
    assert {(m["kind"], m["id"]) for m in chat["members"][room]} == {("agent", CEO), ("human", "sam")}


async def test_task_detail_seats_viewer_in_task_room(chat):
    room = await tc.ensure_task_room(WS, _task({"kind": "workspace"}), seat_viewer="priya")
    await tc.ensure_task_room(WS, _task({"kind": "workspace"}), seat_viewer="priya")
    assert [m["id"] for m in chat["members"][room] if m["kind"] == "human"] == ["priya"]


async def test_post_as_lightbulb_sets_specialist_label_and_attrs(chat):
    refs = [{"taskId": TID, "role": "started"}]
    mid = await tc.post_as_lightbulb(
        WS, ORIGIN, text="Paid.", event_id="task:x:1", specialist="finance", task_refs=refs, kind="result"
    )
    req = chat["posts"][0]["req"]
    assert str(req.agent_id) == CEO and str(req.room_id) == ORIGIN
    assert chat["posts"][0]["attrs"] == {"taskRefs": refs, "specialist": "finance", "kind": "result"}
    assert mid == str(chat["posts"][0]["receipt"].message_id)


async def test_post_as_lightbulb_passes_the_message_it_answers(chat):
    await tc.post_as_lightbulb(WS, ORIGIN, text="Three PRs merged.", event_id="task-line:x:1", in_reply_to="m_voice")
    await tc.post_as_lightbulb(WS, ORIGIN, text="Paid.", event_id="task:x:2")
    assert [p["in_reply_to"] for p in chat["posts"]] == ["m_voice", None]


async def test_post_as_lightbulb_same_event_id_once(chat):
    a = await tc.post_as_lightbulb(WS, ORIGIN, text="Paid.", event_id="task:x:1")
    b = await tc.post_as_lightbulb(WS, ORIGIN, text="Paid.", event_id="task:x:1")
    assert a == b and len(chat["posts"]) == 1


async def test_forward_card_reply_one_line(chat):
    room = await tc.ensure_task_room(WS, _task({"kind": "workspace"}))
    msg = {
        "messageId": "m9",
        "roomId": ORIGIN,
        "text": "use the Amex",
        "author": {"kind": "human", "id": "sam", "displayName": "Sam"},
    }
    await tc.forward_card_reply(WS, TID, msg)
    await tc.forward_card_reply(WS, TID, msg)
    lines = [p for p in chat["posts"] if p.get("room_id") == room]
    assert len(lines) == 1
    assert lines[0]["attrs"] == {"forwardedFrom": {"roomId": ORIGIN, "messageId": "m9"}}
    assert "use the Amex" in lines[0]["text"]


async def test_last_result_text_reads_final_result_line(chat, monkeypatch):
    rows = [
        {"author": {"kind": "agent", "id": CEO}, "kind": "result", "text": "first"},
        {"author": {"kind": "agent", "id": CEO}, "text": "progress"},
        {"author": {"kind": "agent", "id": CEO}, "kind": "result", "text": "second"},
        {"author": {"kind": "human", "id": "sam"}, "text": "thanks"},
    ]

    async def before(room_id, before_seq, limit):
        return rows

    async def get_room(room_id):
        return {"roomId": room_id, "lastSeq": 4}

    monkeypatch.setattr(messaging_repo, "list_messages_before", before)
    monkeypatch.setattr(messaging_repo, "get_room", get_room)
    assert await tc.last_result_text(WS, TID) == "second"


def test_agent_message_attrs_absent_when_flag_off():
    """Legacy posts carry none of the new attrs: request defaults are None and the wire drops them."""
    req = messaging.AgentMessageRequest(
        event_id="e1",
        room_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        text="hi",
        reply_to_message_id=None,
        thread_root_message_id=None,
    )
    assert not {"task_refs", "specialist", "routed_to", "forwarded_from", "kind"} & set(type(req).model_fields)
    item = {
        "messageId": str(uuid.uuid4()),
        "roomId": str(uuid.uuid4()),
        "messageSeq": 1,
        "roomRevision": 1,
        "author": {"kind": "agent", "id": str(uuid.uuid4()), "displayName": "CEO"},
        "text": "hi",
        "createdAt": "2026-11-04T10:00:00Z",
    }
    wire = messaging.canonical_message(item).model_dump(by_alias=True, mode="json")
    assert not {"taskRefs", "specialist", "routedTo", "forwardedFrom", "kind", "automationId"} & set(wire)
    with_attrs = messaging.canonical_message(
        item | {"specialist": "finance", "taskRefs": [{"taskId": TID, "role": "started"}]}
    )
    wire = with_attrs.model_dump(by_alias=True, mode="json")
    assert wire["specialist"] == "finance" and wire["taskRefs"] == [{"taskId": TID, "role": "started"}]
    # The confirm card's automation id is on the wire (read and live), so clients link the card to it
    card = messaging.canonical_message(item | {"kind": "automation_confirm", "automationId": "01JAUTO"})
    wire = card.model_dump(by_alias=True, mode="json")
    assert (wire["kind"], wire["automationId"]) == ("automation_confirm", "01JAUTO")


def test_runtime_cannot_set_task_attrs():
    """The hook-token agent-messages route parses AgentMessageRequest: attrs are not part of it (backend-only)."""
    body = {
        "eventId": "e1",
        "roomId": str(uuid.uuid4()),
        "agentId": str(uuid.uuid4()),
        "text": "hi",
        "replyToMessageId": None,
        "threadRootMessageId": None,
        "taskRefs": [{"taskId": TID, "role": "started"}],
        "specialist": "finance",
        "kind": "result",
    }
    try:
        req = messaging.AgentMessageRequest.model_validate(body)
    except Exception:
        return  # rejected outright
    assert "specialist" not in req.model_dump(by_alias=True)


async def test_front_agent_reads_registry_when_on(chat, monkeypatch):
    async def switch_on(ws):
        return True

    async def roster(ws):
        return [
            {"id": "x", "role": "engineer", "status": "active", "name": "Eng"},
            {"id": CEO, "role": "ceo", "status": "active", "name": "Lightbulb"},
        ]

    async def no_paperclip(ws):
        raise AssertionError("Paperclip read with the switch on")

    monkeypatch.setattr(tc.agent_registry, "roster", roster)
    monkeypatch.setattr(tc.session_chat, "_default_agents", no_paperclip)
    assert await tc.front_agent_id(WS) == CEO


async def test_post_as_lightbulb_goes_into_room_memory(chat, monkeypatch):
    """Audit item 12: Lightbulb's own posts reach room memory (agent posts get no admit row); progress lines and
    the Router's ack-only posts (`remember=False`) do not."""
    from core.services import room_memory

    seen: list[tuple[dict, dict]] = []

    async def ingest(ws, room, message, *, task=None):
        seen.append((room, message))
        return True

    async def switch_on(ws):
        return True

    chat["rooms"][ORIGIN] = {"roomId": ORIGIN, "ownerId": WS, "kind": "channel"}
    monkeypatch.setattr(room_memory.settings, "AGENTCORE_MEMORY_ID", "mem-1")
    monkeypatch.setattr(room_memory, "ingest_message", ingest)
    mid = await tc.post_as_lightbulb(WS, ORIGIN, text="Paid the invoice.", event_id="task:x:1", kind="result")
    await tc.post_as_lightbulb(WS, ORIGIN, text="Working: browser", event_id="task:x:p", progress=True)
    await tc.post_as_lightbulb(WS, ORIGIN, text="On it.", event_id="router:b:0", remember=False)
    await tc.post_as_lightbulb(WS, ORIGIN, text="Needs your OK", event_id="q:1", kind="question")
    ((room, message),) = seen
    assert room["roomId"] == ORIGIN
    assert message["messageId"] == mid and message["text"] == "Paid the invoice."
    assert message["author"] == {"kind": "agent", "id": CEO, "displayName": "Lightbulb"}


async def test_task_room_seat_never_outlives_can_view(chat, monkeypatch):
    """Audit #16: seats are never swept, so the room door asks `can_view` (read and send both go through it)."""
    from core.repositories import tasks_repo
    from core.services.messaging import ApiProblem

    task = {**_task({"kind": "person", "userIds": ["sam"]}), "status": "working"}
    room = await tc.ensure_task_room(WS, task, seat_viewer="priya")  # seated while she could see it

    async def get_task(ws, task_id):
        return task

    monkeypatch.setattr(tasks_repo, "get_task", get_task)
    meta, _ = await messaging.require_room_member(WS, "sam", room)
    assert meta["roomId"] == room
    with pytest.raises(ApiProblem) as exc:
        await messaging.require_room_member(WS, "priya", room)
    assert exc.value.error.code == "ROOM_NOT_FOUND"


def _stored(seq, task_refs):
    return {
        "roomId": ORIGIN,
        "messageId": str(uuid.UUID(int=seq)),
        "messageSeq": seq,
        "roomRevision": seq,
        "author": {"kind": "agent", "id": CEO, "displayName": "Lightbulb"},
        "text": "Lightbulb needs your OK",
        "createdAt": "2026-11-05T10:00:00Z",
        "taskRefs": task_refs,
        "kind": "question",
    }


async def test_room_with_a_question_card_lists(monkeypatch):
    """Dev bug 5 Nov: a question card's `qid` 400'd the whole room's message list. `qid` is part of TaskRef; any
    other stored key is ignored on read and a ref that still does not validate is dropped, never a 400."""
    from unittest.mock import AsyncMock

    from core.auth import AuthContext

    rows = [
        _stored(1, [{"taskId": TID, "role": "started", "qid": "q-1"}]),
        _stored(2, [{"taskId": TID, "role": "started", "later": "x"}, {"taskId": TID, "role": "mystery"}]),
        _stored(3, [{"taskId": TID, "role": "suggested"}]),
    ]
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=({"roomId": ORIGIN}, {})))
    monkeypatch.setattr(messaging.messaging_repo, "list_messages", AsyncMock(return_value=(rows, None)))
    page = await messaging.list_messages(AuthContext(user_id="user_sam", org_id=WS), ORIGIN, 0, 50)
    refs = [m.model_dump()["taskRefs"] for m in page.messages]
    assert refs == [
        [{"taskId": TID, "role": "started", "qid": "q-1"}],
        [{"taskId": TID, "role": "started"}],
        [{"taskId": TID, "role": "started"}],  # a stored "suggested" card (before 5 Nov 2026) reads as an ordinary one
    ]


async def test_agent_post_task_refs_are_strict_on_write():
    with pytest.raises(ValueError):
        await messaging.create_agent_message(
            WS,
            messaging.AgentMessageRequest(
                event_id="e1", room_id=uuid.UUID(ORIGIN), agent_id=uuid.UUID(CEO), text="x", reply_to_message_id=None
            ),
            attrs={"taskRefs": [{"taskId": TID, "role": "started", "nope": 1}]},
        )
