"""A removed member whose Clerk JWT has not expired yet still resolves the org: live-config
must refuse them and must never revive their private stream (the fence that keeps a removed
member off live events)."""

from unittest.mock import AsyncMock, patch

REPO = "core.repositories.messaging_repo"


def test_a_removed_member_is_403_and_the_stream_stays_off(client):
    removed = {"channelKey": "old-key", "active": False}
    with (
        patch(f"{REPO}.get_user_stream", AsyncMock(return_value=removed)),
        patch(f"{REPO}._transact", AsyncMock()) as transact,
    ):
        response = client.get("/api/v1/messaging/live-config")
    assert response.status_code == 403
    assert "WORKSPACE_MEMBERSHIP_REQUIRED" in response.text
    transact.assert_not_awaited()  # nothing was written: the stream is still inactive
