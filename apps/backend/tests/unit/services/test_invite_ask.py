"""T08: after the first completed run in #general of a one-person workspace, the CEO asks
"Who should see this with you?" with the invite link -- once per workspace, ever."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import messaging

OWNER = "org_1"
CEO = str(uuid.UUID(int=20))
GENERAL = messaging.default_room_id(OWNER)
DM = "lightbulb-dm"


@pytest.fixture
def fx(monkeypatch):
    company = MagicMock(starter_agent_ids={"ceo": CEO})
    repo = MagicMock(resolve_owner_row=AsyncMock(return_value=company))
    monkeypatch.setattr("core.repositories.workspace_repo.WorkspaceRepo", MagicMock(return_value=repo))
    claimed: set[str] = set()

    async def claim(room_id, name):
        if (room_id, name) in claimed:
            return False
        claimed.add((room_id, name))
        return True

    async def release(room_id, name):
        claimed.discard((room_id, name))

    monkeypatch.setattr(messaging.messaging_repo, "claim_room_marker", claim)
    monkeypatch.setattr(messaging.messaging_repo, "release_room_marker", release)
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value={"id": CEO}))
    rooms = {DM: {"roomId": DM, "kind": "dm"}, "other-room": {"roomId": "other-room", "kind": "channel"}}
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(side_effect=rooms.get))
    front = AsyncMock(return_value=None)  # the switch is off unless a test turns it on
    monkeypatch.setattr(messaging, "_front_agent_id", front)
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": CEO, "name": "Ada"}]))
    create = AsyncMock(return_value=({}, False))
    monkeypatch.setattr(messaging.messaging_repo, "create_message", create)
    org = AsyncMock(return_value={"members_count": 1})
    monkeypatch.setattr(messaging.clerk_admin, "get_organization", org)
    return MagicMock(create=create, org=org, claimed=claimed, front=front)


def _row(room_id=GENERAL):
    return {"roomId": room_id, "runId": "r1", "triggerMessageId": "m1"}


@pytest.mark.asyncio
async def test_the_ceo_asks_once_in_a_one_person_workspace(fx):
    await messaging.ask_who_should_see(OWNER, _row(), "completed")
    await messaging.ask_who_should_see(OWNER, _row(), "completed")

    fx.create.assert_awaited_once()
    kwargs = fx.create.call_args.kwargs
    assert kwargs["author"] == {"kind": "agent", "id": CEO, "displayName": "Ada"}
    assert kwargs["text"].startswith("Who should see this with you?")
    assert "/chat?panel=invite)" in kwargs["text"]
    assert kwargs["dispatch_targets"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("room_id, outcome", [("other-room", "completed"), (GENERAL, "error"), (GENERAL, "aborted")])
async def test_only_a_completed_run_in_general_asks(fx, room_id, outcome):
    await messaging.ask_who_should_see(OWNER, _row(room_id), outcome)
    fx.create.assert_not_awaited()
    assert not fx.claimed


@pytest.mark.asyncio
async def test_a_workspace_with_two_people_is_never_asked(fx):
    fx.org.return_value = {"members_count": 2}
    await messaging.ask_who_should_see(OWNER, _row(), "completed")
    fx.create.assert_not_awaited()
    assert fx.claimed  # kept: the ask is for the first session of a one-person workspace


@pytest.mark.asyncio
async def test_a_clerk_read_that_cannot_answer_lets_a_later_run_ask(fx):
    fx.org.return_value = None
    await messaging.ask_who_should_see(OWNER, _row(), "completed")
    fx.create.assert_not_awaited()
    assert not fx.claimed
    fx.org.return_value = {"members_count": 1}
    await messaging.ask_who_should_see(OWNER, _row(), "completed")
    fx.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_with_the_switch_the_ask_follows_the_first_run_into_the_lightbulb_dm_once_signed_lightbulb(fx):
    """Super-agent: the first run is in the owner's Lightbulb DM; one ask per workspace, in Lightbulb's name."""
    await messaging.ask_who_should_see(OWNER, _row(DM), "completed")
    fx.create.assert_not_awaited()  # switch off: a DM is not where the first run is
    fx.front.return_value = CEO
    await messaging.ask_who_should_see(OWNER, _row(DM), "completed")
    await messaging.ask_who_should_see(OWNER, _row(GENERAL), "completed")
    fx.create.assert_awaited_once()
    kwargs = fx.create.call_args.kwargs
    assert kwargs["room_id"] == DM and kwargs["author"]["displayName"] == "Lightbulb"
