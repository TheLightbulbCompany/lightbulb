"""Each app's own conversation (docs/apps/app-conversation-design.md): start, ask, open, line routing."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.services import app_conversation as conv
from core.services import apps_room, clerk_admin, messaging, session_chat
from tests.unit.services.test_apps_service import (  # noqa: F401 -- fixtures
    OWNER,
    PERSON,
    SRC_V1,
    _all_active,
    _tar,
    published,
    svc,
    watched,
    world,
)

CEO = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FORGE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
AGENT = svc.BuilderCaller(owner_id=OWNER, actor_id=FORGE, actor_kind="agent")
ADA = AuthContext(user_id="user_1", org_id=OWNER)
BOB = AuthContext(user_id="user_bob", org_id=OWNER)
REAL_REQUIRE_ROOM_MEMBER = messaging.require_room_member  # the fixture fakes it; the door test needs the real one


@pytest.fixture
def chat(world, monkeypatch):  # noqa: F811
    rooms = {"room_1": {"roomId": "room_1", "ownerId": OWNER, "name": "general"}}
    members = {
        "room_1": [
            {"kind": "agent", "id": CEO, "displayName": "CEO", "mentionHandle": "ceo", "listenMode": "mentions"},
            {"kind": "human", "id": "user_1"},
        ]
    }
    posts: list[dict] = []
    said: list[tuple] = []

    async def create_room(**kw):
        rooms.setdefault(
            kw["room_id"],
            {"roomId": kw["room_id"], "ownerId": kw["owner_id"], "name": kw["name"], "kind": kw["kind"], **kw["extra"]},
        )
        members.setdefault(kw["room_id"], [dict(m) for m in kw["members"]])
        return rooms[kw["room_id"]]

    async def get_member(room_id, kind, member_id):
        return next((m for m in members.get(room_id, []) if (m["kind"], m["id"]) == (kind, member_id)), None)

    async def create_message(**kw):
        key = (kw["room_id"], kw["idemp_sk"])
        prior = next((p for p in posts if p["key"] == key), None)
        if prior:
            return prior, True
        posts.append({"messageId": f"m{len(posts) + 1}", "key": key, "createdAt": svc.utc_now_iso(), **kw})
        return posts[-1], False

    async def seat(owner_id, meta, user_id, *, full):
        members[meta["roomId"]].append({"kind": "human", "id": user_id, "role": "member"})
        return meta

    async def require_member(owner_id, user_id, room_id):
        if not await get_member(room_id, "human", user_id):
            raise messaging.ApiProblem("ROOM_FORBIDDEN", "no")
        return rooms[room_id], {}

    async def reads(owner_id, user_id, room_id, room_owner):
        return bool(await get_member(room_id, "human", user_id))

    async def human_message(auth, room_id, request, **kw):
        said.append((auth.user_id, room_id, request))
        return type("M", (), {"message_id": request.client_message_id})()

    repo = svc.messaging_repo
    monkeypatch.setattr(repo, "get_room", AsyncMock(side_effect=lambda r: rooms.get(r)))
    monkeypatch.setattr(repo, "create_room", AsyncMock(side_effect=create_room))
    monkeypatch.setattr(repo, "get_member", AsyncMock(side_effect=get_member))
    monkeypatch.setattr(repo, "list_members", AsyncMock(side_effect=lambda r: list(members.get(r, []))))
    monkeypatch.setattr(repo, "create_message", AsyncMock(side_effect=create_message))
    monkeypatch.setattr(repo, "get_user_stream", AsyncMock(return_value={"active": True}))
    monkeypatch.setattr(repo, "list_open_runs_for_room", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        repo, "list_messages_before", AsyncMock(side_effect=lambda r, b, n: [p for p in posts if p["room_id"] == r])
    )
    monkeypatch.setattr(clerk_admin, "list_organization_members", AsyncMock(return_value=[]))
    monkeypatch.setattr(session_chat, "_seat", seat)
    monkeypatch.setattr(
        session_chat,
        "_default_agents",
        AsyncMock(return_value=[{"kind": "agent", "id": CEO, "displayName": "CEO", "mentionHandle": "ceo"}]),
    )
    roster = [{"id": CEO, "name": "CEO"}, {"id": FORGE, "name": "Forge"}]
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=roster))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(side_effect=lambda u: {"user_1": "Ada"}.get(u, u)))
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(side_effect=require_member))
    monkeypatch.setattr(messaging, "still_reads_room", AsyncMock(side_effect=reads))
    monkeypatch.setattr(messaging, "create_human_message", AsyncMock(side_effect=human_message))
    return type("Chat", (), {"rooms": rooms, "members": members, "posts": posts, "said": said})


def _age(post, minutes=11):
    post["createdAt"] = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def _woken(post):
    return [t["agentId"] for t in post["dispatch_targets"]]


async def _start(**kw):
    return await conv.start(
        kw.pop("caller", AGENT),
        "booking",
        "build a booking page",
        origin_room_id=kw.pop("origin_room_id", "room_1"),
        requested_by=kw.pop("requested_by", "user_1"),
    )


async def test_start_reserves_the_app_and_hands_the_request_to_its_conversation(chat):
    res = await _start()
    app = await svc.apps_repo.get(res["app_id"])
    room = conv.conversation_room_id(res["app_id"])
    assert res["conversation_id"] == room == app["conversation_id"]
    assert (app["state"], app["origin_room_id"], app["room_id"], app["created_by"]) == (
        "requested",
        "room_1",
        "room_1",
        "user_1",
    )
    meta = chat.rooms[room]
    assert (meta["kind"], meta["hidden"], meta["app"]) == ("channel", True, {"app_id": res["app_id"]})
    assert [(m["kind"], m["id"]) for m in chat.members[room]] == [("agent", FORGE), ("human", "user_1")]
    (post,) = chat.posts
    assert post["room_id"] == room and post["text"] == "Ada (in #general): build a booking page"
    assert _woken(post) == [FORGE]
    line = f"Started Booking. It's being built in its own conversation in Canvas: {res['canvas_url']}"
    assert res["next"] == f'post this line in the room as written, then end your turn: "{line}"'


async def test_a_person_starting_from_the_cli_gets_the_ceo_and_a_workspace_app(chat):
    res = await _start(caller=PERSON, origin_room_id=None, requested_by=None)
    app = await svc.apps_repo.get(res["app_id"])
    assert (app["open_to"], app["room_id"], app["created_by"]) == ("workspace", None, "user_1")
    (post,) = chat.posts
    assert post["text"] == "Ada (via CLI): build a booking page" and _woken(post) == [CEO]


async def test_two_starts_for_one_name_make_one_app_and_one_request(chat, monkeypatch):
    first = await _start()
    # The builder took it up (a fresh run in the conversation): a retry is the same request, said once.
    run = {"status": "open", "dispatchedAt": svc.utc_now_iso()}
    monkeypatch.setattr(svc.messaging_repo, "list_open_runs_for_room", AsyncMock(return_value=[run]))
    again = await _start()
    # The owner index lags a create: a racing start finds the winner through the name guard.
    monkeypatch.setattr(svc.apps_repo, "get_by_name", AsyncMock(return_value=None))
    raced = await _start()
    assert first["app_id"] == again["app_id"] == raced["app_id"]
    assert len(await svc.apps_repo.list_for_owner(OWNER)) == 1
    assert len(chat.posts) == 1


async def test_a_second_start_is_heard_when_it_is_new_or_nobody_is_building(chat, monkeypatch):
    await _start()
    # A retry within 10 minutes: the request line is fresh, so it counts as heard (a run may still be opening).
    await _start()
    assert len(chat.posts) == 1
    # Nobody took it up for 10 minutes (no run, no source): a retry says it again, which wakes the builder.
    _age(chat.posts[0])
    await _start()
    assert len(chat.posts) == 2 and _woken(chat.posts[-1]) == [FORGE]
    # Someone else's request while the builder works is its own line, never dropped.
    run = {"status": "open", "dispatchedAt": svc.utc_now_iso()}
    monkeypatch.setattr(svc.messaging_repo, "list_open_runs_for_room", AsyncMock(return_value=[run]))
    chat.members["room_1"].append({"kind": "human", "id": BOB.user_id})
    await _start(requested_by=BOB.user_id)
    assert len(chat.posts) == 3 and chat.posts[-1]["text"] == "user_bob (in #general): build a booking page"
    await _start(requested_by=BOB.user_id)
    assert len(chat.posts) == 3


async def test_start_refuses_a_name_already_built(chat):
    await svc.put_source(OWNER, name="booking", room_id="room_1", commit="c1", archive=_tar(SRC_V1), actor=PERSON)
    with pytest.raises(svc.AppsRefused) as e:
        await _start()
    assert e.value.code == "app_exists"


async def test_the_first_upload_adopts_the_reserved_app(chat):
    res = await _start(caller=PERSON)
    assert (await svc.status(OWNER, res["app_id"]))["state"] == "requested"  # a read keeps the reservation
    room = res["conversation_id"]
    src = await svc.put_source(OWNER, name="booking", room_id=room, commit="c1", archive=_tar(SRC_V1), actor=AGENT)
    app = await svc.apps_repo.get(res["app_id"])
    assert src["app_id"] == res["app_id"]
    assert (app["state"], app["actor_id"], app["actor_kind"]) == ("building", FORGE, "agent")
    # `up` from the conversation keeps the room the request came from.
    await svc.up(
        OWNER,
        name="booking",
        room_id=room,
        services=[],
        open_to="room",
        budget_usd=0,
        source_id=src["source_id"],
        actor=AGENT,
        origin={"kind": "asked"},
    )
    assert (await svc.apps_repo.get(res["app_id"]))["room_id"] == "room_1"


async def test_a_relayed_message_reaches_the_builder_once(chat):
    res = await _start()
    origin = {"room_id": "room_1", "requested_by": "user_1"}
    for _ in range(2):
        out = await conv.ask(AGENT, res["app_id"], "make the button green", origin=origin, client_message_id="msg_9")
    asks = chat.posts[1:]
    assert len(asks) == 1 and out["message_id"] == asks[0]["messageId"]
    assert asks[0]["text"] == "Ada (in #general): make the button green" and _woken(asks[0]) == [FORGE]
    await conv.ask(AGENT, res["app_id"], "and bigger")
    assert chat.posts[-1]["text"] == "Forge (via CLI): and bigger"


async def test_an_older_app_keeps_its_room_until_its_conversation_is_opened(chat):
    src = await svc.put_source(OWNER, name="crew", room_id="room_1", commit="c1", archive=_tar(SRC_V1), actor=AGENT)
    app = await svc.apps_repo.get(src["app_id"])
    await apps_room.post_stuck_line(app, "Crew", "building")
    assert chat.posts[-1]["room_id"] == "room_1"
    assert app.get("conversation_id") is None
    # Bob can open it (a room app; he sits in the room) but is in no conversation yet: opening seats him.
    chat.members["room_1"].append({"kind": "human", "id": BOB.user_id})
    out = await conv.open_conversation(BOB, src["app_id"])
    room = out["room_id"]
    assert room == conv.conversation_room_id(src["app_id"])
    # Its builder is whom the room would pick (Forge never sat in #general).
    assert [(m["kind"], m["id"]) for m in chat.members[room]] == [("agent", CEO), ("human", BOB.user_id)]
    app = await svc.apps_repo.get(src["app_id"])
    assert app["conversation_id"] == room
    await apps_room.post_stuck_line(app, "Crew", "building")
    assert chat.posts[-1]["room_id"] == room
    assert (await conv.open_conversation(BOB, src["app_id"]))["room_id"] == room  # seated once
    assert sum(m["id"] == BOB.user_id for m in chat.members[room]) == 1


async def test_someone_who_cannot_open_the_app_is_not_seated(chat):
    res = await _start()  # a room app: Bob is not in #general
    with pytest.raises(svc.AppsRefused) as e:
        await conv.open_conversation(BOB, res["app_id"])
    assert e.value.code == "not_a_member"
    await svc.apps_repo.patch(res["app_id"], {"open_to": "workspace"})
    assert (await conv.open_conversation(BOB, res["app_id"]))["room_id"] == res["conversation_id"]


async def test_canvas_asks_as_the_person_addressed_to_the_builder(chat):
    res = await _start()
    out = await conv.ask_as_member(ADA, res["app_id"], "make it blue")
    ((user, room, request),) = chat.said
    assert (user, room, out["conversation_id"]) == ("user_1", res["conversation_id"], res["conversation_id"])
    assert [m.id for m in request.mentions] == [FORGE]


async def test_approval_line_goes_to_the_conversation(chat):
    res = await _start(caller=svc.BuilderCaller(owner_id=OWNER, actor_id=FORGE, actor_kind="agent"), requested_by=None)
    await apps_room.post_approval_line(
        owner_id=OWNER,
        user_id="user_1",  # the approver is not seated: Lightbulb says it, and it still wakes the builder
        workflow={"app_id": res["app_id"], "workflow_id": "w1", "provider_name": "vercel", "environment": "prod"},
    )
    post = chat.posts[-1]
    assert post["room_id"] == res["conversation_id"] and _woken(post) == [FORGE]
    assert post["text"] == "Approved hosting for Booking (live). Setting it up."


async def test_the_origin_room_hears_once_when_the_first_version_is_in_canvas(chat, world):  # noqa: F811
    res = await _start()
    src = await svc.put_source(
        OWNER, name="booking", room_id=res["conversation_id"], commit="c1", archive=_tar(SRC_V1), actor=AGENT
    )
    await svc.up(
        OWNER,
        name="booking",
        room_id=res["conversation_id"],
        services=["database"],
        open_to="room",
        budget_usd=0,
        source_id=src["source_id"],
        actor=AGENT,
        origin={"kind": "asked"},
    )
    await _all_active(world, res["app_id"])
    await svc.status(OWNER, res["app_id"])
    world.vercel.finish(f"dpl_{len(world.vercel.created())}")
    assert (await svc.status(OWNER, res["app_id"]))["state"] == "ready"
    await svc.status(OWNER, res["app_id"])
    ready = [p for p in chat.posts if p["room_id"] == "room_1"]
    assert [p["text"] for p in ready] == [f"Booking's first version is in Canvas: {res['canvas_url']}"]
    assert _woken(ready[0]) == []


async def test_an_agent_may_publish_when_a_person_asked_in_the_conversation(chat, monkeypatch):
    res = await _start()
    app = await svc.apps_repo.get(res["app_id"])
    asked = {"author": {"kind": "human"}, "text": "ship it", "createdAt": "2999-01-01T00:00:00Z"}
    relayed = {"author": {"kind": "system"}, "text": "Ada (in #general): ship it", "createdAt": "2999-01-01"}
    seen: list[str] = []

    async def messages(room_id, before, limit):
        seen.append(room_id)
        return [asked] if room_id == res["conversation_id"] else [relayed]

    monkeypatch.setattr(svc.messaging_repo, "list_messages_before", messages)
    assert await svc._asked_to_publish(app)
    asked["author"] = {"kind": "system"}
    assert not await svc._asked_to_publish(app)
    assert seen[-2:] == [res["conversation_id"], "room_1"]


@pytest.mark.parametrize("extra,woken", [({"app": {"app_id": "app_1"}, "hidden": True}, [FORGE]), ({}, [])])
async def test_every_message_in_an_apps_conversation_is_for_its_builder(monkeypatch, extra, woken):
    import uuid

    room = "11111111-1111-4111-8111-111111111111"
    meta = {"roomId": room, "ownerId": OWNER, "kind": "channel", "revision": 1, "lastSeq": 3, **extra}
    sender = {"kind": "human", "id": "user_1", "role": "member"}
    builder = {"kind": "agent", "id": FORGE, "displayName": "Forge", "role": "member", "listenMode": "mentions"}
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(meta, sender)))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[sender, builder]))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Ada"))
    address = AsyncMock(return_value=None)
    monkeypatch.setattr(messaging, "address", address)
    # Stop at the write: what it would dispatch is the point.
    created = AsyncMock(side_effect=messaging.messaging_repo.MessageForbidden("stop"))
    monkeypatch.setattr(messaging.messaging_repo, "create_message", created)
    with pytest.raises(messaging.ApiProblem):
        await messaging.create_human_message(
            ADA,
            room,
            messaging.CreateMessageRequest(
                client_message_id=uuid.uuid4(),
                text="make the button green",
                reply_to_message_id=None,
                thread_root_message_id=None,
            ),
        )
    targets = created.await_args.kwargs["dispatch_targets"]
    assert [t["agentId"] for t in targets] == woken
    assert all(t["eventKind"] == "user_request" for t in targets)
    assert address.await_count == (0 if extra else 1)  # the app's builder needs no reading of the text


# --- apps don't belong to rooms (Task 2) --------------------------------------------------------


async def _roomless(actor=PERSON):
    return await svc.put_source(OWNER, name="notes", commit="c1", archive=_tar(SRC_V1), actor=actor)


async def test_an_app_with_no_room_builds_publishes_and_shows_to_the_workspace(chat, world):  # noqa: F811
    src = await _roomless()
    app_id = src["app_id"]
    app = await svc.apps_repo.get(app_id)
    assert (app["room_id"], app["open_to"], app["created_by"]) == (None, "workspace", "user_1")
    # An up that still says "room" has no room to be open to: the app stays openable, by the workspace.
    await svc.up(
        OWNER,
        name="notes",
        services=["database"],
        open_to="room",
        budget_usd=0,
        source_id=src["source_id"],
        actor=PERSON,
        origin={"kind": "idea"},
    )
    assert (await svc.apps_repo.get(app_id))["open_to"] == "workspace"
    await _all_active(world, app_id)
    await svc.status(OWNER, app_id)
    world.vercel.finish(f"dpl_{len(world.vercel.created())}")
    assert (await svc.status(OWNER, app_id))["state"] == "ready"
    # A person running `app publish` is never gated (nobody had to ask in any room).
    assert (await svc.publish(OWNER, app_id, source_id=src["source_id"], actor=PERSON))["state"] == "building"
    rows = await svc.list_apps(OWNER, BOB)
    assert [(r["name"], r["can_open"], r["room_name"]) for r in rows] == [("notes", True, None)]
    url = await svc.open_app(OWNER, app_id, auth=BOB, target="preview")
    assert "/_lightbulb/session#token=" in url


async def test_sharing_without_a_room_only_me_and_no_room_mode(chat):
    src = await _roomless()
    app_id = src["app_id"]
    await svc.apps_repo.patch(app_id, {"preview_url": "https://notes-preview.apps.example.com"})
    with pytest.raises(svc.AppsRefused) as e:
        await svc.update_settings(OWNER, app_id, open_to="room")
    assert e.value.code == "no_room"
    await svc.update_settings(OWNER, app_id, open_to="me")
    assert [r["name"] for r in await svc.list_apps(OWNER, ADA)] == ["notes"]
    assert await svc.list_apps(OWNER, BOB) == []
    with pytest.raises(svc.AppsRefused) as e:
        await svc.open_app(OWNER, app_id, auth=BOB, target="preview")
    assert e.value.code == "not_allowed"
    assert "token=" in await svc.open_app(OWNER, app_id, auth=ADA, target="preview")
    with pytest.raises(svc.AppsRefused):
        await conv.open_conversation(BOB, app_id)


async def test_an_agent_publishes_a_roomless_app_only_when_asked_in_its_conversation(chat, monkeypatch):
    src = await _roomless(actor=AGENT)
    room = (await conv.open_conversation(ADA, src["app_id"]))["room_id"]
    app = await svc.apps_repo.get(src["app_id"])
    said: list[dict] = []
    monkeypatch.setattr(
        svc.messaging_repo, "list_messages_before", AsyncMock(side_effect=lambda r, b, n: said if r == room else [])
    )
    assert not await svc._asked_to_publish(app)
    said.append({"author": {"kind": "human"}, "text": "publish it", "createdAt": "2999-01-01T00:00:00Z"})
    assert await svc._asked_to_publish(app)


# --- Task 1 review --------------------------------------------------------------------------------


async def test_a_relay_seats_nobody_and_a_seat_is_not_access(chat, monkeypatch):
    res = await _start()  # a room app: Ada asked in #general and sits in its conversation
    room = res["conversation_id"]
    chat.members["room_1"].append({"kind": "human", "id": BOB.user_id})
    await conv.ask(AGENT, res["app_id"], "and a map", origin={"room_id": "room_1", "requested_by": BOB.user_id})
    assert chat.posts[-1]["text"] == "user_bob (in #general): and a map"
    assert BOB.user_id not in [m["id"] for m in chat.members[room]]
    # Bob opens it (he is in #general), then the creator narrows it to herself: Bob leaves, Ada stays.
    await conv.open_conversation(BOB, res["app_id"])
    left: list[str] = []

    async def unseat(room_id, user_id):
        chat.members[room_id] = [m for m in chat.members[room_id] if m["id"] != user_id]
        left.append(user_id)
        return True

    monkeypatch.setattr(session_chat, "_unseat", unseat)
    await svc.update_settings(OWNER, res["app_id"], open_to="me")
    assert left == [BOB.user_id] and "user_1" in [m["id"] for m in chat.members[room]]
    # Seated again by hand, a seat alone still opens nothing.
    chat.members[room].append({"kind": "human", "id": BOB.user_id})
    with pytest.raises(svc.AppsRefused):
        await conv.open_conversation(BOB, res["app_id"])


async def test_a_first_upload_that_loses_the_adoption_carries_on_with_the_winner(chat, monkeypatch):
    res = await _start()
    real = svc.apps_repo.patch

    async def raced(app_id, fields, *, expect=None):
        if expect == {"state": "requested"}:  # another upload adopted it a moment earlier
            await real(app_id, {"state": "building", "actor_id": CEO, "actor_kind": "agent"})
            return None
        return await real(app_id, fields, expect=expect)

    monkeypatch.setattr(svc.apps_repo, "patch", raced)
    src = await svc.put_source(OWNER, name="booking", commit="c1", archive=_tar(SRC_V1), actor=AGENT)
    app = await svc.apps_repo.get(res["app_id"])
    assert src["app_id"] == res["app_id"] and (app["state"], app["actor_id"]) == ("building", CEO)
    assert app["sources"]


# --- Task 2 review --------------------------------------------------------------------------------


async def test_invited_is_the_invitees_and_managers_never_the_room(chat, monkeypatch):
    monkeypatch.setattr(svc.apps_repo, "put_invite", AsyncMock())
    monkeypatch.setattr(svc.email_service, "send_email", AsyncMock())
    res = await _start()  # a room app from #general, created by Ada
    app_id = res["app_id"]
    await svc.apps_repo.patch(app_id, {"preview_url": "https://booking-preview.apps.example.com"})
    chat.members["room_1"].append({"kind": "human", "id": BOB.user_id})
    # Inviting from a room app keeps it a room app: its room still opens it.
    assert (await svc.invite(OWNER, app_id, emails=["x@example.com"], user_id="user_1"))["open_to"] == "room"
    assert "token=" in await svc.open_app(OWNER, app_id, auth=BOB, target="preview")
    # Only Ada, then one invite: "invited". Bob is in the origin room and still opens neither.
    await svc.update_settings(OWNER, app_id, open_to="me")
    assert (await svc.invite(OWNER, app_id, emails=["x@example.com"], user_id="user_1"))["open_to"] == "invited"
    with pytest.raises(svc.AppsRefused) as e:
        await svc.open_app(OWNER, app_id, auth=BOB, target="preview")
    assert e.value.code == "not_allowed"
    with pytest.raises(svc.AppsRefused):
        await conv.open_conversation(BOB, app_id)
    assert "token=" in await svc.open_app(OWNER, app_id, auth=ADA, target="preview")
    assert not [a for a in await svc.list_apps(OWNER, BOB) if a["can_open"]]


async def test_a_second_start_tells_the_agent_to_post_nothing(chat, monkeypatch):
    first = await _start()
    assert first["next"].startswith("post this line in the room") and first["relayed"]
    # Nobody building for 10 minutes: the request is said again (a new line), but the room hears nothing new.
    _age(chat.posts[0])
    again = await _start()
    assert again["next"] == "this app was already started; post nothing in the room" and again["relayed"]
    # The builder is on it: the same request replays, nothing new anywhere.
    run = {"status": "open", "dispatchedAt": svc.utc_now_iso()}
    monkeypatch.setattr(svc.messaging_repo, "list_open_runs_for_room", AsyncMock(return_value=[run]))
    busy = await _start()
    assert busy["next"] == again["next"] and not busy["relayed"]
    assert len(chat.posts) == 2
    # Nothing running, but the line just went out: heard, not posted a third time.
    monkeypatch.setattr(svc.messaging_repo, "list_open_runs_for_room", AsyncMock(return_value=[]))
    assert not (await _start())["relayed"] and len(chat.posts) == 2


async def test_the_loser_of_a_start_race_never_posts_a_second_request(chat, monkeypatch):
    first = await _start()
    # Nobody is building yet, and the owner index lags: this start loses the name to the first.
    monkeypatch.setattr(svc.apps_repo, "get_by_name", AsyncMock(return_value=None))
    raced = await _start()
    assert raced["app_id"] == first["app_id"]
    assert raced["next"] == "this app was already started; post nothing in the room" and not raced["relayed"]
    assert len(chat.posts) == 1


# --- final review --------------------------------------------------------------------------------


async def test_up_as_the_cli_sends_it_never_widens_a_started_app(chat):
    res = await _start()  # from #general: open to the room, asked for by Ada
    src = await svc.put_source(OWNER, name="booking", commit="c1", archive=_tar(SRC_V1), actor=AGENT, base_commit="new")
    # The real CLI: no room, "workspace", its own origin.
    await svc.up(
        OWNER,
        name="booking",
        room_id=None,
        services=[],
        open_to="workspace",
        budget_usd=0,
        source_id=src["source_id"],
        actor=AGENT,
        origin={"kind": "unknown"},
    )
    app = await svc.apps_repo.get(res["app_id"])
    assert (app["open_to"], app["room_id"], app["origin"]) == ("room", "room_1", {"kind": "asked"})


async def test_a_start_by_an_agent_for_a_person_makes_them_its_creator(chat):
    # Reserved first with nobody named (the agent forgot --requested-by), then asked for by Ada.
    first = await _start(requested_by=None)
    assert (await svc.apps_repo.get(first["app_id"]))["created_by"] is None
    _age(chat.posts[0])
    await _start()
    app = await svc.apps_repo.get(first["app_id"])
    assert app["created_by"] == "user_1"
    assert "user_1" in [m["id"] for m in chat.members[first["conversation_id"]]]
    assert chat.posts[-1]["text"] == "Ada (in #general): build a booking page"
    # Someone outside the room is never believed: Bob is not made its creator.
    second = await conv.start(AGENT, "notes", "notes", origin_room_id="room_1", requested_by=BOB.user_id)
    assert (await svc.apps_repo.get(second["app_id"]))["created_by"] is None


async def test_ask_as_the_cli_sends_it_posts_once_on_a_retry(chat):
    res = await _start()
    # `app ask booking --room room_1 --requested-by user_1 --message-id m-77`, its answer lost, then retried.
    outs = [
        await conv.ask(
            AGENT,
            res["app_id"],
            "make it blue",
            origin={"room_id": "room_1", "requested_by": "user_1"},
            client_message_id="m-77",
        )
        for _ in range(2)
    ]
    assert outs[0] == outs[1] and len(chat.posts) == 2
    assert chat.posts[-1]["text"] == "Ada (in #general): make it blue"


async def test_a_new_folder_uploads_only_as_the_first_version(chat):
    res = await _start()
    first = await svc.put_source(
        OWNER, name="booking", commit="c1", archive=_tar(SRC_V1), actor=AGENT, base_commit="new"
    )
    assert first["app_id"] == res["app_id"]
    # Its answer lost: the same commit again is a retry.
    await svc.put_source(OWNER, name="booking", commit="c1", archive=_tar(SRC_V1), actor=AGENT, base_commit="new")
    with pytest.raises(svc.AppsRefused) as e:
        await svc.put_source(OWNER, name="booking", commit="c2", archive=_tar(SRC_V1), actor=AGENT, base_commit="new")
    assert e.value.code == "stale_base" and "app pull" in e.value.next


async def test_start_refuses_an_apps_own_conversation_as_its_room(chat):
    res = await _start()
    with pytest.raises(svc.AppsRefused) as e:
        await conv.start(AGENT, "notes", "notes", origin_room_id=res["conversation_id"], requested_by="user_1")
    assert e.value.code == "room_is_app"


async def test_a_seat_in_an_apps_conversation_ends_with_the_right_to_open_it(chat):
    res = await _start()
    room = res["conversation_id"]
    chat.members["room_1"].append({"kind": "human", "id": BOB.user_id})
    await conv.open_conversation(BOB, res["app_id"])
    assert (await REAL_REQUIRE_ROOM_MEMBER(OWNER, BOB.user_id, room))[1]["id"] == BOB.user_id
    # Bob leaves #general; his seat is still there, but the door no longer lets him in. Ada (its creator) stays.
    chat.members["room_1"] = [m for m in chat.members["room_1"] if m["id"] != BOB.user_id]
    with pytest.raises(messaging.ApiProblem) as e:
        await REAL_REQUIRE_ROOM_MEMBER(OWNER, BOB.user_id, room)
    assert e.value.error.code == "ROOM_NOT_FOUND"
    await svc.apps_repo.patch(res["app_id"], {"open_to": "me"})
    assert (await REAL_REQUIRE_ROOM_MEMBER(OWNER, "user_1", room))[1]["id"] == "user_1"
    # A workspace admin keeps it (Clerk says so; no token here to say it).
    clerk_admin.list_organization_members.return_value = [{"user_id": BOB.user_id, "role": "org:admin"}]
    assert (await REAL_REQUIRE_ROOM_MEMBER(OWNER, BOB.user_id, room))[1]["id"] == BOB.user_id


async def test_a_person_pulls_or_asks_only_an_app_they_can_open(chat):
    res = await _start()
    await svc.put_source(OWNER, name="booking", commit="c1", archive=_tar(SRC_V1), actor=AGENT)
    await svc.apps_repo.patch(res["app_id"], {"open_to": "me"})
    bob = svc.BuilderCaller(owner_id=OWNER, actor_id=BOB.user_id, actor_kind="person")
    for call in (
        svc.latest_source(OWNER, res["app_id"], bob),
        conv.ask(bob, res["app_id"], "make it red"),
    ):
        with pytest.raises(svc.AppsRefused) as e:
            await call
        assert e.value.code == "not_allowed"
    # Its creator, and any agent of the workspace, still can.
    assert (await svc.latest_source(OWNER, res["app_id"], PERSON))[1] == "c1"
    assert (await svc.latest_source(OWNER, res["app_id"], AGENT))[1] == "c1"
    await conv.ask(PERSON, res["app_id"], "make it red")


@pytest.mark.asyncio
async def test_a_request_from_a_direct_message_says_so(monkeypatch):
    monkeypatch.setattr(conv.messaging, "human_display_name", AsyncMock(return_value="Ada"))
    monkeypatch.setattr(conv.messaging_repo, "get_room", AsyncMock(return_value={"kind": "dm"}))
    caller = svc.BuilderCaller(owner_id=OWNER, actor_id="agent_x", actor_kind="agent")
    assert await conv._attributed(caller, "build it", "user_ada", "dm_1") == "Ada (in a direct message): build it"


async def test_a_builder_in_an_apps_conversation_cannot_upload_a_second_app(chat, monkeypatch):
    res = await _start()
    room = conv.conversation_room_id(res["app_id"])
    monkeypatch.setattr(svc.messaging_repo, "get_run", AsyncMock(return_value={"roomId": room}))
    in_conversation = svc.BuilderCaller(owner_id=OWNER, actor_id=FORGE, actor_kind="agent", run_id="run_9")
    with pytest.raises(svc.AppsRefused) as e:
        await svc.put_source(OWNER, name="booking-page", commit="c1", archive=_tar(SRC_V1), actor=in_conversation)
    assert e.value.code == "wrong_app" and "lightbulb app new booking " in e.value.next
    assert await svc.apps_repo.get_by_name(OWNER, "booking-page") is None
    # Its own name is taken over as before.
    await svc.put_source(OWNER, name="booking", commit="c1", archive=_tar(SRC_V1), actor=in_conversation)
    assert (await svc.apps_repo.get(res["app_id"]))["state"] == "building"
