"""``GET /messaging/feed/u/{owner_id}``: the agent feed read, hook-token gated."""

from unittest.mock import AsyncMock, patch

SERVICE = "core.services.messaging"
FEED = "3f0c9d8e-1b2a-5c4d-9e8f-0a1b2c3d4e5f"


def test_the_agent_feed_read_takes_the_hook_token(client):
    with (
        patch("routers.messaging._require_hook_token", AsyncMock()) as hook,
        patch(f"{SERVICE}.agent_feed", AsyncMock(return_value={"roomId": FEED, "topic": None, "posts": []})),
    ):
        response = client.get("/api/v1/messaging/feed/u/org_1")
    assert response.status_code == 200 and response.json()["roomId"] == FEED
    assert hook.await_args.args[0] == "org_1"
