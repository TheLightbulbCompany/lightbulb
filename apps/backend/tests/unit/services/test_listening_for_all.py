"""Listening (``active``) is the default on every plan, the free trial included
(owner ruling 2026-09-24). No door reads billing for it: the #general seed, a
room's member upsert and the work consumer. Spend stays bounded by the trial
daily cap in the gate and by Jev picking at most one responder."""

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
async def test_a_trial_workspace_seeds_every_agent_listening(monkeypatch):
    starter_ids, create_room = _seed_fixture(monkeypatch)
    read = _billing(monkeypatch, TRIAL)

    assert await messaging.ensure_default_room(OWNER, USER) is not None

    assert set(_seeded_modes(create_room, starter_ids).values()) == {"active"}
    read.assert_not_awaited()


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
async def test_a_trial_owner_sets_listening_on_a_channel(monkeypatch):
    upsert = _upsert_fixture(monkeypatch, room_kind="channel")
    read = _billing(monkeypatch, TRIAL)

    await messaging.upsert_member(AuthContext(user_id=USER, org_id=OWNER), ROOM, _member("active"))

    assert upsert.call_args.kwargs["member"]["listenMode"] == "active"
    read.assert_not_awaited()


IN_ROOM = {"kind": "agent", "id": AGENT, "role": "member", "listenMode": "mentions"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["active", "mentions", "observe"])
async def test_any_member_of_a_trial_workspace_changes_an_agents_mode(monkeypatch, mode):
    upsert = _upsert_fixture(monkeypatch, room_kind="channel", role="member", existing=IN_ROOM)
    _billing(monkeypatch, TRIAL)

    await messaging.upsert_member(AuthContext(user_id=USER, org_id=OWNER), ROOM, _member(mode))

    assert upsert.call_args.kwargs["member"]["listenMode"] == mode


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

    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.upsert_member(AuthContext(user_id=USER, org_id="org_lightbulb"), ROOM, _member("active"))

    assert exc.value.error.code == "ROOM_FORBIDDEN"
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_member_still_cannot_add_an_agent(monkeypatch):
    upsert = _upsert_fixture(monkeypatch, room_kind="channel", role="member", existing=None)

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

    with pytest.raises(messaging.ApiProblem) as exc:
        await messaging.upsert_member(AuthContext(user_id=USER, org_id=OWNER), ROOM, _member("active"))

    assert exc.value.error.code == "ROOM_FORBIDDEN"
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambient_work_reaches_the_runtime_for_a_trial_owner(monkeypatch):
    read = _billing(monkeypatch, TRIAL)
    row = AsyncMock(return_value=None)  # not provisioned yet -> WorkRetry, past any gate
    monkeypatch.setattr(work.container_repo, "get_by_owner_id", row)
    monkeypatch.setattr(work, "put_metric", MagicMock())
    worker = OpenClawWorkWorker(sqs_client=MagicMock())

    with pytest.raises(WorkRetry):
        await worker._dispatch_message(
            {"ownerId": OWNER, "roomId": ROOM}, {"id": AGENT}, {"messageId": "m1"}, "room_event"
        )

    row.assert_awaited_once()
    read.assert_not_awaited()


def _welcome_text(monkeypatch) -> str:
    create_message = messaging.messaging_repo.create_message
    kwargs = create_message.call_args.kwargs
    assert kwargs["req_hash"] == messaging.messaging_repo.request_hash(kwargs["text"], kwargs["author"]["id"])
    return kwargs["text"]


@pytest.mark.asyncio
async def test_a_trial_workspace_gets_the_first_message_with_its_name(monkeypatch):
    _seed_fixture(monkeypatch)
    _billing(monkeypatch, TRIAL)
    monkeypatch.setattr(
        messaging.clerk_admin, "get_organization", AsyncMock(return_value={"name": "Anna's Frozen Treats"})
    )

    await messaging.ensure_default_room(OWNER, USER)

    text = _welcome_text(monkeypatch)
    assert text == messaging.default_room_welcome("Anna's Frozen Treats")
    assert text.startswith("Welcome to Anna's Frozen Treats. I'm Lightbulb.")
    for reply in messaging.FIRST_REPLIES:
        assert reply.rstrip(" ") in text


@pytest.mark.asyncio
async def test_the_first_message_falls_back_when_clerk_has_no_name(monkeypatch):
    _seed_fixture(monkeypatch)
    _billing(monkeypatch, TRIAL)
    monkeypatch.setattr(messaging.clerk_admin, "get_organization", AsyncMock(return_value=None))

    await messaging.ensure_default_room(OWNER, USER)

    assert _welcome_text(monkeypatch).startswith("Welcome to your workspace. I'm Lightbulb.")


@pytest.mark.asyncio
async def test_the_welcome_is_resolved_before_the_room_exists(monkeypatch):
    """A room is visible the moment create_room returns; a slow Clerk call after that would let the
    owner's first message take seq 1 and hide the starter chips for good."""
    _, create_room = _seed_fixture(monkeypatch)
    _billing(monkeypatch, TRIAL)
    order: list[str] = []

    async def get_organization(_owner_id):
        order.append("clerk")
        return {"name": "Anna's Frozen Treats"}

    create_room.side_effect = lambda **_kwargs: order.append("room") or {"kind": "channel"}
    monkeypatch.setattr(messaging.clerk_admin, "get_organization", get_organization)

    await messaging.ensure_default_room(OWNER, USER)

    assert order == ["clerk", "room"]


def test_the_first_message_never_starts_a_mention_or_channel_with_the_name():
    assert messaging.default_room_welcome("@growth  team").startswith("Welcome to growth team.")
    assert messaging.default_room_welcome("#general").startswith("Welcome to general.")


@pytest.mark.asyncio
async def test_an_admin_set_mode_is_marked_as_the_users_choice(monkeypatch):
    upsert = _upsert_fixture(monkeypatch, room_kind="channel")

    await messaging.upsert_member(AuthContext(user_id=USER, org_id=OWNER), ROOM, _member("mentions"))

    assert upsert.call_args.kwargs["member"]["modeSetBy"] == "user"
