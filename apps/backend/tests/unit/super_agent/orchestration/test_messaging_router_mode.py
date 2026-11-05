"""Router-room messaging (wave-2 O Task 7; contracts-v2 §6, §10): the admit row, one voice, Lightbulb-only seats,
`@lightbulb` reserved, specialist pins, card replies forwarded. Review focus 5: switch off ⇒ today's path."""

import uuid
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext

from core.services import messaging
from core.services.messaging import ApiProblem
from core.services import task_conversation as tc

OWNER = "org_1"
SAM = "user_sam"
ROOM = str(uuid.UUID(int=9))
CEO = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FIN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
TASK = "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"
CARD = str(uuid.UUID(int=77))
ROSTER = [{"id": CEO, "name": "Aden", "role": "ceo"}, {"id": FIN, "name": "Finance", "role": "specialist"}]


def _human(user=SAM):
    return {
        "kind": "human",
        "id": user,
        "displayName": "Sam",
        "mentionHandle": None,
        "role": "owner",
        "listenMode": None,
        "joinedSeq": 1,
    }


def _agent(agent_id=CEO, handle="lightbulb", listen="active"):
    return {
        "kind": "agent",
        "id": agent_id,
        "displayName": "Aden",
        "mentionHandle": handle,
        "role": "member",
        "listenMode": listen,
        "joinedSeq": 1,
    }


@pytest.fixture
def world(monkeypatch):
    w = {
        "on": True,
        "meta": {"roomId": ROOM, "ownerId": OWNER, "kind": "channel", "lastSeq": 3},
        "members": [_human(), _agent()],
        "messages": {
            CARD: {
                "roomId": ROOM,
                "messageId": CARD,
                "messageSeq": 2,
                "taskRefs": [{"taskId": TASK, "role": "started"}],
            }
        },
        "created": [],
        "forwarded": [],
        "task": {"taskId": TASK, "specialist": FIN},
    }

    async def router_room(meta, owner_id):
        return w["on"]

    async def front(owner_id):
        return CEO if w["on"] else None

    async def create_message(**kw):
        w["created"].append(kw)
        return {
            "roomId": kw["room_id"],
            "messageId": str(uuid.uuid4()),
            "messageSeq": 4,
            "roomRevision": 1,
            "author": kw["author"],
            "text": kw["text"],
            "mentions": kw["mentions"],
            "createdAt": messaging.utc_now().isoformat(),
            "dispatchTargets": kw["dispatch_targets"],
        }, False

    async def get_message_by_id(room_id, message_id):
        return w["messages"].get(message_id)

    async def get_member(room_id, kind, member_id):
        return next((m for m in w["members"] if (m["kind"], m["id"]) == (kind, member_id)), None)

    async def forward(ws, task_id, message):
        w["forwarded"].append((task_id, message["messageId"]))

    async def get_task(ws, task_id):
        return w["task"] if task_id == TASK else None

    monkeypatch.setattr(messaging, "_router_room", router_room)
    monkeypatch.setattr(messaging, "_front_agent_id", front)
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(side_effect=lambda o, u, r: (w["meta"], _human(u))))
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: w["meta"]))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", get_member)
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(side_effect=lambda r: list(w["members"])))
    monkeypatch.setattr(messaging.messaging_repo, "has_foreign_members", AsyncMock(return_value=False))
    monkeypatch.setattr(messaging.messaging_repo, "get_message_by_id", get_message_by_id)
    monkeypatch.setattr(messaging.messaging_repo, "create_message", create_message)
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=ROSTER))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Sam"))
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(
        messaging, "address", AsyncMock(return_value={"agents": [], "humans": [], "anyone": False, "path": "none"})
    )
    monkeypatch.setattr(messaging, "_count_jev_miss", AsyncMock())
    monkeypatch.setattr(messaging, "_publish_message_created_soon", lambda item: None)
    monkeypatch.setattr(messaging, "_touch_last_interactive_quietly", AsyncMock())
    monkeypatch.setattr("core.services.onboarding_brief.capture_first_workspace_message", AsyncMock())
    monkeypatch.setattr(tc, "forward_card_reply", forward)
    monkeypatch.setattr("core.repositories.tasks_repo.get_task", get_task)
    return w


async def _send(text="pay acme", *, mentions=(), reply_to=None):
    return await messaging.create_human_message(
        AuthContext(user_id=SAM, org_id=OWNER),
        ROOM,
        messaging.CreateMessageRequest(
            client_message_id=uuid.uuid4(),
            text=text,
            mentions=[messaging.MemberRef(kind="agent", id=m) for m in mentions],
            reply_to_message_id=uuid.UUID(reply_to) if reply_to else None,
            thread_root_message_id=None,
        ),
    )


async def _post(agent_id=CEO, text="On it", progress=False):
    return await messaging.create_agent_message(
        OWNER,
        messaging.AgentMessageRequest(
            event_id=f"ev-{uuid.uuid4()}",
            room_id=uuid.UUID(ROOM),
            agent_id=uuid.UUID(agent_id),
            text=text,
            reply_to_message_id=None,
            thread_root_message_id=None,
            progress=progress,
        ),
    )


@pytest.mark.asyncio
async def test_router_room_message_writes_admit_row(world):
    await _send(mentions=[CEO])
    kw = world["created"][-1]
    assert kw["admit"] is True
    assert kw["dispatch_targets"] == [{"agentId": CEO, "eventKind": "user_request"}]  # stored for admission


@pytest.mark.asyncio
async def test_flag_off_mid_flight_takes_legacy_path(world):
    world["on"] = False
    await _send(mentions=[CEO])
    kw = world["created"][-1]
    assert kw["admit"] is False  # today's WORK#MESSAGE# rows, no admit row, no Router signal
    assert kw["dispatch_targets"] == [{"agentId": CEO, "eventKind": "user_request"}]
    assert world["forwarded"] == []


@pytest.mark.asyncio
async def test_specialist_handle_pins(world):
    """`@finance` names a roster agent that is not seated: it rides the targets as `user_request`, which
    admission turns into `ref.pinnedSpecialist` (test_admission::test_specialist_mention_pins)."""
    await _send("@finance pay acme", mentions=[FIN])
    kw = world["created"][-1]
    assert kw["mentions"] == [{"kind": "agent", "id": FIN}]
    assert kw["dispatch_targets"] == [{"agentId": FIN, "eventKind": "user_request"}]
    with pytest.raises(ApiProblem) as err:
        await _send(mentions=[str(uuid.UUID(int=5))])
    assert err.value.error.code == "MEMBER_NOT_FOUND"


@pytest.mark.asyncio
async def test_card_reply_forwarded_once(world):
    await _send("yes, the March one", reply_to=CARD)
    assert [t for t, _ in world["forwarded"]] == [TASK]
    # A replay writes nothing and forwards nothing.
    real = messaging.messaging_repo.create_message

    async def replayed(**kw):
        item, _ = await real(**kw)
        return item, True

    messaging.messaging_repo.create_message = replayed
    await _send("yes, the March one", reply_to=CARD)
    assert len(world["forwarded"]) == 1
    # A reply to an ordinary message forwards nothing.
    world["messages"][CARD] = {"roomId": ROOM, "messageId": CARD, "messageSeq": 2}
    await _send("ok", reply_to=CARD)
    assert len(world["forwarded"]) == 1


@pytest.mark.asyncio
async def test_agent_message_in_router_room_wakes_nobody(world):
    world["members"].append(_agent(FIN, "finance"))
    await _post(text="@finance take this")
    kw = world["created"][-1]
    assert kw["dispatch_targets"] == []
    assert kw["mentions"] == [{"kind": "agent", "id": FIN}]  # the mention stays stored, nobody runs


@pytest.mark.asyncio
async def test_worker_post_outside_its_task_room_refused(world):
    world["members"].append(_agent(FIN, "finance"))  # seated from before the switch: still refused
    with pytest.raises(ApiProblem) as err:
        await _post(agent_id=FIN, text="done")
    assert err.value.error.code == "ROOM_FORBIDDEN"
    assert world["created"] == []
    # Another task's room: refused too.
    world["meta"] = {**world["meta"], "hidden": True, "task": {"taskId": str(uuid.UUID(int=3))}}
    with pytest.raises(ApiProblem):
        await _post(agent_id=FIN, text="done")
    # Its own task room: the specialist posts there without a seat (room_agent's stand-in).
    world["meta"] = {**world["meta"], "task": {"taskId": TASK}}
    world["members"] = [_human(), _agent()]
    await _post(agent_id=FIN, text="done")
    assert world["created"][-1]["author"]["id"] == FIN and world["created"][-1]["dispatch_targets"] == []


@pytest.mark.asyncio
async def test_lightbulb_handle_reserved(world):
    agents = [{"id": CEO, "name": "Lightbulb"}, {"id": FIN, "name": "Finance"}]
    assert messaging.mention_handles(agents) == {CEO: "lightbulb", FIN: "finance"}  # legacy: unchanged
    reserved = messaging.mention_handles(agents, reserved=messaging.RESERVED_HANDLES)
    assert reserved[FIN] == "finance" and reserved[CEO].startswith("lightbulb-") and reserved[CEO] != "lightbulb"
    member = messaging.CreateRoomMember(kind="agent", id=CEO, role="member", listen_mode="active")
    assert (await messaging._resolved_member(OWNER, member, ROSTER))["mentionHandle"] == "lightbulb"
    other = messaging.CreateRoomMember(kind="agent", id=FIN, role="member", listen_mode="active")
    assert (await messaging._resolved_member(OWNER, other, ROSTER))["mentionHandle"] == "finance"
    world["on"] = False
    assert (await messaging._resolved_member(OWNER, member, ROSTER))["mentionHandle"] == "aden"


@pytest.mark.asyncio
async def test_new_router_room_seats_only_lightbulb(world, monkeypatch):
    monkeypatch.setattr(tc, "front_agent_id", AsyncMock(return_value=CEO))
    m = lambda kind, mid, listen=None: messaging.CreateRoomMember(kind=kind, id=mid, role="member", listen_mode=listen)  # noqa: E731
    seated = await messaging._lightbulb_only(OWNER, [m("human", "user_priya"), m("agent", FIN, "mentions")], "channel")
    assert [(x.kind, x.id, x.listen_mode) for x in seated] == [
        ("human", "user_priya", None),
        ("agent", CEO, "mentions"),
    ]
    seated = await messaging._lightbulb_only(OWNER, [m("human", "user_priya")], "channel")
    assert [(x.kind, x.id, x.listen_mode) for x in seated] == [("human", "user_priya", None), ("agent", CEO, "active")]
    seated = await messaging._lightbulb_only(OWNER, [m("agent", FIN, "active")], "dm")
    assert [(x.kind, x.id) for x in seated] == [("agent", CEO)]  # a DM to any agent is a DM to Lightbulb
    seated = await messaging._lightbulb_only(OWNER, [m("human", "user_priya")], "dm")
    assert [(x.kind, x.id) for x in seated] == [("human", "user_priya")]
