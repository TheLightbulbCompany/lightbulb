"""Listening (``active``) is an always-on feature — an agent answering traffic
nobody addressed to it — so it rides the paid predicate. A free-tier workspace
seeds every agent on Mentions, cannot set Listening on a channel, and the work
consumer never wakes a free-tier container for ambient traffic."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.auth import AuthContext
from core.services import messaging
from core.services import openclaw_work as work
from core.services.openclaw_work import OpenClawWorkWorker, WorkRetry

OWNER = "org_1"
USER = "user_owner"
AGENT = str(uuid.UUID(int=1))
ROOM = str(uuid.UUID(int=9))
TRIAL = {"owner_id": OWNER, "subscription_status": "trial_local"}
PAID = {"owner_id": OWNER, "subscription_status": "active"}
REAL_REQUIRE_ROOM_MEMBER = messaging.require_room_member


def _billing(monkeypatch, account) -> AsyncMock:
    read = AsyncMock(return_value=account)
    monkeypatch.setattr(messaging.billing_repo, "get_by_owner_id", read)
    return read


def _seed_fixture(monkeypatch):
    starter_ids = {key: str(uuid.UUID(int=index + 20)) for index, key in enumerate(messaging._STARTER_KEYS)}
    company = MagicMock(status="active", starter_agent_ids=starter_ids)
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=company)
    monkeypatch.setattr("core.repositories.paperclip_repo.PaperclipRepo", MagicMock(return_value=repo))
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=None))
    monkeypatch.setattr(
        messaging,
        "load_agent_roster",
        AsyncMock(return_value=[{"id": agent_id, "name": key} for key, agent_id in starter_ids.items()]),
    )
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Owner"))
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", AsyncMock())
    create_room = AsyncMock(return_value={"kind": "channel"})
    monkeypatch.setattr(messaging.messaging_repo, "create_room", create_room)
    monkeypatch.setattr(messaging.messaging_repo, "create_message", AsyncMock(return_value=({}, False)))
    monkeypatch.setattr(messaging, "capture_product_event", MagicMock())
    return starter_ids, create_room


def _seeded_modes(create_room: AsyncMock, starter_ids: dict[str, str]) -> dict[str, str]:
    members = create_room.call_args.kwargs["members"]
    return {key: next(m["listenMode"] for m in members if m["id"] == agent_id) for key, agent_id in starter_ids.items()}


@pytest.mark.asyncio
async def test_a_free_tier_workspace_seeds_every_agent_on_mentions(monkeypatch):
    starter_ids, create_room = _seed_fixture(monkeypatch)
    _billing(monkeypatch, TRIAL)

    assert await messaging.ensure_default_room(OWNER, USER) is not None

    assert set(_seeded_modes(create_room, starter_ids).values()) == {"mentions"}


@pytest.mark.asyncio
async def test_a_paid_workspace_seeds_every_agent_listening(monkeypatch):
    starter_ids, create_room = _seed_fixture(monkeypatch)
    _billing(monkeypatch, PAID)

    assert await messaging.ensure_default_room(OWNER, USER) is not None

    assert set(_seeded_modes(create_room, starter_ids).values()) == {"active"}


def _upsert_fixture(monkeypatch, *, room_kind: str, role: str = "owner", existing: dict | None = None) -> AsyncMock:
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(
            return_value=(
                {"kind": room_kind, "roomId": ROOM, "ownerId": OWNER},
                {"kind": "human", "id": USER, "role": role},
            )
        ),
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=existing))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": AGENT, "name": "Friday"}]))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[]))
    upsert = AsyncMock(return_value={"kind": room_kind})
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert)
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock())
    monkeypatch.setattr(messaging, "capture_product_event", MagicMock())
    return upsert


def _member(listen_mode: str) -> messaging.UpsertRoomMemberRequest:
    return messaging.UpsertRoomMemberRequest(kind="agent", id=AGENT, role="member", listenMode=listen_mode)


@pytest.mark.asyncio
async def test_the_free_tier_cannot_set_listening_on_a_channel(monkeypatch):
    upsert = _upsert_fixture(monkeypatch, room_kind="channel")
    _billing(monkeypatch, TRIAL)

    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.upsert_member(AuthContext(user_id=USER, org_id=OWNER), ROOM, _member("active"))

    assert exc.value.error.code == "PLAN_REQUIRED"
    assert exc.value.status_code == 403
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_paid_workspace_sets_listening(monkeypatch):
    upsert = _upsert_fixture(monkeypatch, room_kind="channel")
    _billing(monkeypatch, PAID)

    await messaging.upsert_member(AuthContext(user_id=USER, org_id=OWNER), ROOM, _member("active"))

    assert upsert.call_args.kwargs["member"]["listenMode"] == "active"


IN_ROOM = {"kind": "agent", "id": AGENT, "role": "member", "listenMode": "mentions"}


@pytest.mark.asyncio
async def test_a_member_of_a_paid_workspace_can_set_an_agents_listening(monkeypatch):
    upsert = _upsert_fixture(monkeypatch, room_kind="channel", role="member", existing=IN_ROOM)
    _billing(monkeypatch, PAID)

    await messaging.upsert_member(AuthContext(user_id=USER, org_id=OWNER), ROOM, _member("active"))

    assert upsert.call_args.kwargs["member"]["listenMode"] == "active"


@pytest.mark.asyncio
async def test_a_vendor_side_member_cannot_change_the_customers_agents_mode(monkeypatch):
    upsert = _upsert_fixture(monkeypatch, room_kind="channel", role="member", existing=IN_ROOM)
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(
            return_value=(
                {"kind": "channel", "roomId": ROOM, "ownerId": OWNER, "shared": True},
                {"kind": "human", "id": USER, "role": "member", "homeOwnerId": "org_lightbulb"},
            )
        ),
    )
    _billing(monkeypatch, PAID)

    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.upsert_member(AuthContext(user_id=USER, org_id="org_lightbulb"), ROOM, _member("active"))

    assert exc.value.error.code == "ROOM_FORBIDDEN"
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_member_on_the_free_tier_cannot_change_a_mode(monkeypatch):
    upsert = _upsert_fixture(monkeypatch, room_kind="channel", role="member", existing=IN_ROOM)
    _billing(monkeypatch, TRIAL)

    for mode in ("active", "observe"):
        with pytest.raises(messaging.ApiProblem) as exc:
            await messaging.upsert_member(AuthContext(user_id=USER, org_id=OWNER), ROOM, _member(mode))
        assert exc.value.status_code == 403
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_member_of_a_paid_workspace_still_cannot_add_an_agent(monkeypatch):
    upsert = _upsert_fixture(monkeypatch, room_kind="channel", role="member", existing=None)
    _billing(monkeypatch, PAID)

    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.upsert_member(AuthContext(user_id=USER, org_id=OWNER), ROOM, _member("mentions"))

    assert exc.value.error.code == "ROOM_FORBIDDEN"
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_non_member_cannot_change_a_mode(monkeypatch):
    _upsert_fixture(monkeypatch, room_kind="channel", existing=IN_ROOM)
    # The real membership check: the caller has no member row in this workspace's room.
    monkeypatch.setattr(messaging, "require_room_member", REAL_REQUIRE_ROOM_MEMBER)
    monkeypatch.setattr(
        messaging.messaging_repo, "get_room", AsyncMock(return_value={"roomId": ROOM, "ownerId": OWNER, "lastSeq": 3})
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
    upsert = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert)
    _billing(monkeypatch, PAID)

    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.upsert_member(AuthContext(user_id=USER, org_id=OWNER), ROOM, _member("active"))

    assert exc.value.error.code == "ROOM_FORBIDDEN"
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_mentions_costs_no_billing_read(monkeypatch):
    upsert = _upsert_fixture(monkeypatch, room_kind="channel")
    read = _billing(monkeypatch, TRIAL)

    await messaging.upsert_member(AuthContext(user_id=USER, org_id=OWNER), ROOM, _member("mentions"))

    upsert.assert_awaited_once()
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_dm_keeps_its_active_default_on_the_free_tier(monkeypatch):
    """Every DM message is addressed, so the mode is inert there — and every
    DM is created with ``active`` (packages/shared useRooms.selectDm)."""
    upsert = _upsert_fixture(monkeypatch, room_kind="dm")
    read = _billing(monkeypatch, TRIAL)

    await messaging.upsert_member(AuthContext(user_id=USER, org_id=OWNER), ROOM, _member("active"))

    upsert.assert_awaited_once()
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_free_tier_cannot_create_a_channel_with_a_listening_agent(monkeypatch):
    _billing(monkeypatch, TRIAL)
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": AGENT, "name": "Friday"}]))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Owner"))
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", AsyncMock())
    create_room = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "create_room", create_room)
    request = messaging.CreateRoomRequest(
        clientRoomId=uuid.uuid4(),
        name="ops",
        kind="channel",
        members=[messaging.CreateRoomMember(kind="agent", id=AGENT, role="member", listenMode="active")],
    )

    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.create_room(AuthContext(user_id=USER, org_id=OWNER), request)

    assert exc.value.error.code == "PLAN_REQUIRED"
    create_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambient_work_never_reaches_the_runtime_for_a_free_tier_owner(monkeypatch):
    """A row that still says ``active`` on a free-tier room (seeded before
    the gate) must not wake the container for traffic its agent will never
    answer."""
    _billing(monkeypatch, TRIAL)
    row = AsyncMock(return_value={"gateway_token": "gw", "agentcore_runtime_arn": "arn:r1"})
    monkeypatch.setattr(work.container_repo, "get_by_owner_id", row)
    metric = MagicMock()
    monkeypatch.setattr(work, "put_metric", metric)
    worker = OpenClawWorkWorker(sqs_client=MagicMock())

    await worker._dispatch_message({"ownerId": OWNER, "roomId": ROOM}, {"id": AGENT}, {"messageId": "m1"}, "room_event")

    # The gate is ahead of the row read, so an ambient message for a free-tier owner costs
    # nothing at all -- no read, and above all no ingress invoke (which is what starts the
    # microVM and the metered inference behind it).
    row.assert_not_awaited()
    assert metric.call_args.kwargs["dimensions"] == {"outcome": "plan_gate", "event_kind": "room_event"}


@pytest.mark.asyncio
async def test_ambient_work_still_reaches_the_runtime_for_a_paid_owner(monkeypatch):
    _billing(monkeypatch, PAID)
    row = AsyncMock(return_value=None)  # not provisioned yet -> WorkRetry, past the gate
    monkeypatch.setattr(work.container_repo, "get_by_owner_id", row)
    monkeypatch.setattr(work, "put_metric", MagicMock())
    worker = OpenClawWorkWorker(sqs_client=MagicMock())

    with pytest.raises(WorkRetry):
        await worker._dispatch_message(
            {"ownerId": OWNER, "roomId": ROOM}, {"id": AGENT}, {"messageId": "m1"}, "room_event"
        )

    row.assert_awaited_once()


def _welcome_text(monkeypatch) -> str:
    create_message = messaging.messaging_repo.create_message
    kwargs = create_message.call_args.kwargs
    assert kwargs["req_hash"] == messaging.messaging_repo.request_hash(kwargs["text"], kwargs["author"]["id"])
    return kwargs["text"]


@pytest.mark.asyncio
async def test_a_paid_workspace_keeps_the_plain_welcome(monkeypatch):
    _seed_fixture(monkeypatch)
    _billing(monkeypatch, PAID)

    await messaging.ensure_default_room(OWNER, USER)

    assert _welcome_text(monkeypatch) == messaging.DEFAULT_ROOM_WELCOME


@pytest.mark.asyncio
async def test_a_free_tier_workspace_gets_the_same_plain_welcome(monkeypatch):
    """A plain first message in the starter #general reaches the CEO on every plan
    (#2960: no silent human messages), so the free tier needs no @-instruction."""
    _seed_fixture(monkeypatch)
    _billing(monkeypatch, TRIAL)

    await messaging.ensure_default_room(OWNER, USER)

    assert _welcome_text(monkeypatch) == messaging.DEFAULT_ROOM_WELCOME
    assert not hasattr(messaging, "DEFAULT_ROOM_WELCOME_FREE")


ROOM_A = str(uuid.UUID(int=30))
ROOM_B = str(uuid.UUID(int=31))
AGENT_2 = str(uuid.UUID(int=2))


def _room_meta(room_id: str, *, kind: str = "channel", owner: str = OWNER) -> dict:
    return {"roomId": room_id, "ownerId": owner, "kind": kind, "revision": 1}


def _agent_member(agent_id: str, mode: str | None, *, mode_set_by: str | None = None) -> dict:
    member = {"kind": "agent", "id": agent_id, "role": "member", "listenMode": mode, "displayName": agent_id}
    if mode_set_by is not None:
        member["modeSetBy"] = mode_set_by
    return member


def _promote_listening_fixture(monkeypatch, *, room_ids: list[str], rooms: dict, members: dict):
    monkeypatch.setattr(messaging.messaging_repo, "list_owner_room_ids", AsyncMock(return_value=room_ids))
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(side_effect=lambda rid: rooms.get(rid)))
    monkeypatch.setattr(
        messaging.messaging_repo, "list_members", AsyncMock(side_effect=lambda rid: members.get(rid, []))
    )
    # Each upsert returns a distinct, identifiable meta so a test can prove publish used the
    # LAST flip in a room, not an earlier one -- i.e. publish really happened once per room.
    upsert = AsyncMock(side_effect=lambda **kw: {"roomId": kw["room_id"], "lastFlipped": kw["member"]["id"]})
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert)
    publish = AsyncMock()
    monkeypatch.setattr(messaging, "_publish_room_metadata", publish)
    return upsert, publish


@pytest.mark.asyncio
async def test_promote_listening_flips_every_mentions_agent_across_every_room(monkeypatch):
    rooms = {ROOM_A: _room_meta(ROOM_A), ROOM_B: _room_meta(ROOM_B)}
    members = {
        ROOM_A: [_agent_member(AGENT, "mentions"), _agent_member(AGENT_2, "mentions")],
        ROOM_B: [_agent_member(AGENT, "mentions")],
    }
    upsert, publish = _promote_listening_fixture(monkeypatch, room_ids=[ROOM_A, ROOM_B], rooms=rooms, members=members)

    await messaging.promote_listening(OWNER)

    flipped = {
        (c.kwargs["room_id"], c.kwargs["member"]["id"], c.kwargs["member"]["listenMode"]) for c in upsert.call_args_list
    }
    assert flipped == {(ROOM_A, AGENT, "active"), (ROOM_A, AGENT_2, "active"), (ROOM_B, AGENT, "active")}
    # One publish per CHANGED room, not one per flipped agent -- ROOM_A flips two agents but
    # publishes once, carrying the meta from the last upsert in that room.
    published = {call.args[0]["roomId"]: call.args[0]["lastFlipped"] for call in publish.call_args_list}
    assert published == {ROOM_A: AGENT_2, ROOM_B: AGENT}


@pytest.mark.asyncio
async def test_promote_listening_skips_a_dm(monkeypatch):
    rooms = {ROOM_A: _room_meta(ROOM_A, kind="dm")}
    members = {ROOM_A: [_agent_member(AGENT, "mentions")]}
    upsert, publish = _promote_listening_fixture(monkeypatch, room_ids=[ROOM_A], rooms=rooms, members=members)

    await messaging.promote_listening(OWNER)

    upsert.assert_not_awaited()
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_listening_skips_a_room_owned_by_another_workspace(monkeypatch):
    """A shared room can appear in this owner's room list without this owner's policy applying
    to it -- its META still belongs to the workspace that created it."""
    rooms = {ROOM_A: _room_meta(ROOM_A, owner="org_other")}
    members = {ROOM_A: [_agent_member(AGENT, "mentions")]}
    upsert, publish = _promote_listening_fixture(monkeypatch, room_ids=[ROOM_A], rooms=rooms, members=members)

    await messaging.promote_listening(OWNER)

    upsert.assert_not_awaited()
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_listening_skips_the_vendor_room(monkeypatch):
    """The vendor room's own META is owned by THIS owner (#2644), so the base ownerId check
    alone would not catch it -- it needs the explicit vendor-room check."""
    monkeypatch.setattr(messaging.settings, "VENDOR_ORG_ID", "org_vendor")
    vendor_room = messaging.messaging_repo.vendor_room_id(OWNER)
    rooms = {vendor_room: _room_meta(vendor_room)}
    members = {vendor_room: [_agent_member(AGENT, "mentions")]}
    upsert, publish = _promote_listening_fixture(monkeypatch, room_ids=[vendor_room], rooms=rooms, members=members)

    await messaging.promote_listening(OWNER)

    upsert.assert_not_awaited()
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_listening_skips_a_room_with_a_foreign_member(monkeypatch):
    rooms = {ROOM_A: _room_meta(ROOM_A)}
    members = {
        ROOM_A: [
            _agent_member(AGENT, "mentions"),
            {"kind": "human", "id": "user_2", "role": "member", "homeOwnerId": "org_other"},
        ]
    }
    upsert, publish = _promote_listening_fixture(monkeypatch, room_ids=[ROOM_A], rooms=rooms, members=members)

    await messaging.promote_listening(OWNER)

    upsert.assert_not_awaited()
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_listening_never_overrides_observe_active_or_a_users_choice(monkeypatch):
    rooms = {ROOM_A: _room_meta(ROOM_A)}
    members = {
        ROOM_A: [
            _agent_member(AGENT, "observe"),
            _agent_member(AGENT_2, "active"),
            _agent_member(str(uuid.UUID(int=3)), "mentions", mode_set_by="user"),
        ]
    }
    upsert, publish = _promote_listening_fixture(monkeypatch, room_ids=[ROOM_A], rooms=rooms, members=members)

    await messaging.promote_listening(OWNER)

    upsert.assert_not_awaited()
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_listening_is_best_effort_per_room(monkeypatch, caplog):
    """A room that fails to promote is logged; the rest of the owner's rooms still promote."""
    rooms = {ROOM_A: _room_meta(ROOM_A), ROOM_B: _room_meta(ROOM_B)}
    members = {ROOM_A: [_agent_member(AGENT, "mentions")], ROOM_B: [_agent_member(AGENT, "mentions")]}
    upsert, publish = _promote_listening_fixture(monkeypatch, room_ids=[ROOM_A, ROOM_B], rooms=rooms, members=members)

    async def failing_get_room(room_id):
        if room_id == ROOM_A:
            raise RuntimeError("ddb down")
        return rooms[room_id]

    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(side_effect=failing_get_room))

    await messaging.promote_listening(OWNER)

    upsert.assert_awaited_once()
    assert upsert.call_args.kwargs["room_id"] == ROOM_B
    assert "listening promotion failed" in caplog.text


@pytest.mark.asyncio
async def test_an_admin_set_mode_is_marked_as_the_users_choice(monkeypatch):
    upsert = _upsert_fixture(monkeypatch, room_kind="channel")

    await messaging.upsert_member(AuthContext(user_id=USER, org_id=OWNER), ROOM, _member("mentions"))

    assert upsert.call_args.kwargs["member"]["modeSetBy"] == "user"
