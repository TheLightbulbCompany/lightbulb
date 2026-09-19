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
async def test_a_paid_workspace_seeds_the_ceo_listening(monkeypatch):
    starter_ids, create_room = _seed_fixture(monkeypatch)
    _billing(monkeypatch, PAID)

    assert await messaging.ensure_default_room(OWNER, USER) is not None

    modes = _seeded_modes(create_room, starter_ids)
    assert modes["ceo"] == "active"
    assert {mode for key, mode in modes.items() if key != "ceo"} == {"mentions"}


def _upsert_fixture(monkeypatch, *, room_kind: str) -> AsyncMock:
    monkeypatch.setattr(
        messaging,
        "require_room_admin",
        AsyncMock(
            return_value=(
                {"kind": room_kind, "roomId": ROOM, "ownerId": OWNER},
                {"kind": "human", "id": USER, "role": "owner"},
            )
        ),
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
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
