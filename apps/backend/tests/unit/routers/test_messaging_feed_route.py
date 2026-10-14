"""``GET /messaging/feed``: the Activity page's first read ensures the workspace feed (so a
workspace that predates it needs no backfill), seats the caller, and returns ``{roomId, topic}``."""

from unittest.mock import AsyncMock, patch

SERVICE = "core.services.messaging"
FEED = "3f0c9d8e-1b2a-5c4d-9e8f-0a1b2c3d4e5f"


def test_feed_ensures_the_room_seats_the_caller_and_returns_its_brief(client, mock_auth_context):
    owner = mock_auth_context.org_id
    with (
        patch(f"{SERVICE}.ensure_feed_room", AsyncMock(return_value=FEED)) as ensure,
        patch(f"{SERVICE}.join_feed_room", AsyncMock(return_value=True)) as join,
        patch(f"{SERVICE}.messaging_repo.get_room", AsyncMock(return_value={"roomId": FEED, "topic": "Money only."})),
    ):
        response = client.get("/api/v1/messaging/feed")
    assert response.status_code == 200, response.text
    assert response.json() == {"roomId": FEED, "topic": "Money only."}
    # The reader is only the last-resort owner candidate (messaging._feed_owner).
    ensure.assert_awaited_once_with(owner, mock_auth_context.user_id)
    join.assert_awaited_once_with(owner, mock_auth_context.user_id)


def test_feed_not_ready_is_retryable(client):
    with patch(f"{SERVICE}.ensure_feed_room", AsyncMock(return_value=None)):
        response = client.get("/api/v1/messaging/feed")
    assert response.status_code == 503, response.text


def test_the_agent_feed_read_takes_the_hook_token(client):
    with (
        patch("routers.messaging._require_hook_token", AsyncMock()) as hook,
        patch(f"{SERVICE}.agent_feed", AsyncMock(return_value={"roomId": FEED, "topic": None, "posts": []})),
    ):
        response = client.get("/api/v1/messaging/feed/u/org_1")
    assert response.status_code == 200 and response.json()["roomId"] == FEED
    assert hook.await_args.args[0] == "org_1"
