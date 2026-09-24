"""Hook-token gate + wiring for the out-of-turn agent post (mirrors test_messaging_run_events.py)."""

from unittest.mock import AsyncMock, patch

from core.services.messaging import AgentMessageReceiptResponse

OWNER, TOKEN = "org_a", "hook-token-for-a"
AGENT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ROOM = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
URL = f"/api/v1/internal/messaging/agent-posts/{OWNER}"
BODY = {"eventId": "post:a:1", "agentId": AGENT, "text": "Morning brief"}


def test_missing_bearer_is_401(client):
    assert client.post(URL, json=BODY).status_code == 401


def test_valid_token_reaches_the_service(client):
    receipt = AgentMessageReceiptResponse(message_id=AGENT, room_id=ROOM, created_at="2026-09-24T00:00:00Z")
    with (
        patch(
            "routers.messaging.container_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": OWNER, "hook_token": TOKEN}),
        ),
        patch("routers.messaging.service.post_agent_message", AsyncMock(return_value=receipt)) as post,
    ):
        r = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 201 and r.json()["roomId"] == ROOM
    assert post.call_args.args[0] == OWNER and post.call_args.args[1].room_id is None
