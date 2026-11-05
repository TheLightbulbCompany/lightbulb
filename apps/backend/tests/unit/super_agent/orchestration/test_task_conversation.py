"""The hidden task room and the one voice that posts results (wave-2 O Task 5; contracts §6)."""

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
    rooms: dict[str, dict] = {}
    members: dict[str, list[dict]] = {}
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

    async def create_agent_message(owner_id, req):
        prior = next((p for p in posts if p["req"].event_id == req.event_id), None)
        if prior:
            return prior["receipt"]
        receipt = messaging.AgentMessageReceiptResponse(
            message_id=uuid.uuid4(), room_id=req.room_id, created_at="2026-11-04T10:00:00Z"
        )
        posts.append({"req": req, "receipt": receipt})
        return receipt

    async def create_message(**kw):
        key = (kw["room_id"], kw["idemp_sk"])
        prior = next((p for p in posts if p.get("key") == key), None)
        if prior:
            return prior, True
        posts.append({"key": key, "messageId": str(uuid.uuid4()), **kw})
        return posts[-1], False

    monkeypatch.setattr(tc.session_chat, "_default_agents", default_agents)
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
    assert req.specialist == "finance" and req.kind == "result"
    assert [r.model_dump(by_alias=True) for r in req.task_refs] == refs
    assert messaging._message_attrs(req)["taskRefs"] == refs
    assert mid == str(chat["posts"][0]["receipt"].message_id)


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
    assert messaging._message_attrs(req) is None
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
    assert not {"taskRefs", "specialist", "routedTo", "forwardedFrom", "kind"} & set(wire)
    with_attrs = messaging.canonical_message(
        item | {"specialist": "finance", "taskRefs": [{"taskId": TID, "role": "started"}]}
    )
    wire = with_attrs.model_dump(by_alias=True, mode="json")
    assert wire["specialist"] == "finance" and wire["taskRefs"] == [{"taskId": TID, "role": "started"}]
