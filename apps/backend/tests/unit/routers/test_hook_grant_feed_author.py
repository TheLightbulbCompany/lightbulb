"""The run-event hook grant on the REAL room_agent path (test_hook_grant_reader mocks it): in the feed
a roster author is admitted; a non-roster agent, a fired one, or another workspace's feed is refused."""

import time
import uuid
from unittest.mock import AsyncMock, patch

import jwt
import pytest

from core.repositories import messaging_repo
from core.services import hook_grants
from core.services.messaging import RunEventResponse

OWNER = "org_feed"
FEED = messaging_repo.feed_room_id(OWNER)
OTHER = "org_other"
ROW = "11111111-1111-4111-8111-111111111111"
AUTHOR = "10000000-0000-4000-8000-000000000004"
STRANGER = "30000000-0000-4000-8000-000000000001"
FIRED = "10000000-0000-4000-8000-000000000009"
RUN = "44444444-4444-4444-8444-444444444444"
KEY = "test-only-hook-grant-signing-key"


def _grant(agent, room):
    now = int(time.time())
    claims = {
        "kind": hook_grants.PURPOSE,
        "aud": hook_grants.AUDIENCE,
        "ownerId": OWNER,
        "rowId": ROW,
        "agentId": agent,
        "runId": RUN,
        "roomId": room,
        "actions": ["run.event"],
        "iat": now,
        "exp": now + 60,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, KEY, algorithm="HS256", headers={"kid": "1"})


def _post(client, agent, *, room=FEED, room_owner=OWNER):
    run = {
        "ownerId": OWNER,
        "runId": RUN,
        "agentId": agent,
        "roomId": room,
        "status": "started",
        "deadlineAt": int(time.time()) + 60,
    }
    with (
        patch.dict("os.environ", {"HOOK_GRANT_SIGNING_KEY": KEY}),
        patch("routers.messaging.container_repo.get_by_owner_id", AsyncMock(return_value={"id": ROW})),
        patch.object(messaging_repo, "get_run", AsyncMock(return_value=run)),
        patch.object(messaging_repo, "get_room", AsyncMock(return_value={"roomId": room, "ownerId": room_owner})),
        patch.object(messaging_repo, "get_member", AsyncMock(return_value=None)),  # nobody is seated
        patch(
            "core.services.messaging.load_agent_roster",
            AsyncMock(return_value=[{"id": AUTHOR, "name": "Growth"}]),  # FIRED is no longer on it
        ),
        patch(
            "routers.messaging.service.handle_run_event",
            AsyncMock(return_value=RunEventResponse(run_id=RUN, status="applied")),
        ) as handle,
    ):
        response = client.post(
            f"/api/v1/internal/messaging/runs/{OWNER}/events",
            json={"runId": RUN, "type": "run.heartbeat", "at": "2026-10-15T00:00:00Z"},
            headers={"Authorization": f"Bearer {_grant(agent, room)}"},
        )
    return response, handle


def test_a_roster_author_is_admitted(client):
    response, handle = _post(client, AUTHOR)
    assert response.status_code == 200
    handle.assert_awaited_once()


@pytest.mark.parametrize("agent", [STRANGER, FIRED])
def test_a_non_roster_or_fired_agent_is_refused(client, agent):
    response, handle = _post(client, agent)
    assert response.status_code == 403
    handle.assert_not_awaited()


def test_another_workspaces_feed_is_refused(client):
    response, handle = _post(client, AUTHOR, room=messaging_repo.feed_room_id(OTHER), room_owner=OTHER)
    assert response.status_code == 403
    handle.assert_not_awaited()
