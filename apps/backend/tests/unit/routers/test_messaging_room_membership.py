"""Bridge -> backend room-membership read: a Paperclip wake runs in a room conversation only while
the agent is still a member of that live room."""

from unittest.mock import AsyncMock, patch

import pytest

OWNER, TOKEN = "org_a", "hook-token-for-a"
ROOM = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"
URL = f"/api/v1/internal/messaging/rooms/{OWNER}/{ROOM}/agents/{AGENT}"
H = {"Authorization": f"Bearer {TOKEN}"}
REPO = "core.services.messaging.messaging_repo"


@pytest.fixture
def repo():
    with (
        patch(
            "routers.messaging.container_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": OWNER, "hook_token": TOKEN}),
        ),
        patch(f"{REPO}.get_room", AsyncMock(return_value={"roomId": ROOM, "ownerId": OWNER})) as get_room,
        patch(f"{REPO}.get_member", AsyncMock(return_value={"kind": "agent", "id": AGENT})) as get_member,
    ):
        yield {"get_room": get_room, "get_member": get_member}


def test_needs_the_owners_hook_token(client, repo):
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers={"Authorization": "Bearer WRONG"}).status_code == 401
    repo["get_room"].assert_not_awaited()


def test_a_current_member_of_a_live_room_is_204(client, repo):
    assert client.get(URL, headers=H).status_code == 204
    repo["get_member"].assert_awaited_once_with(ROOM, "agent", AGENT)


@pytest.mark.parametrize(
    ("room", "member", "status"),
    [
        (None, {"id": AGENT}, 404),
        ({"ownerId": "org_other"}, {"id": AGENT}, 404),
        ({"ownerId": OWNER, "deletedAt": "2026-10-01T00:00:00Z"}, {"id": AGENT}, 404),
        ({"ownerId": OWNER, "archivedAt": "2026-10-01T00:00:00Z"}, {"id": AGENT}, 409),
        ({"ownerId": OWNER}, None, 404),
    ],
)
def test_anything_else_is_a_definite_no(client, repo, room, member, status):
    repo["get_room"].return_value = room
    repo["get_member"].return_value = member
    assert client.get(URL, headers=H).status_code == status
