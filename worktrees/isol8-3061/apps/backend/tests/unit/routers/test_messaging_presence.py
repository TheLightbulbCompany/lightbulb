"""Presence heartbeat + snapshot: recent heartbeats are online, stale ones age
out after PRESENCE_TTL_SECONDS, and workspaces never see each other."""

from unittest.mock import MagicMock, patch

import pytest

from routers import messaging as router


def _auth(user_id: str):
    auth = MagicMock()
    auth.org_id = "org_1"
    auth.user_id = user_id
    return auth


@pytest.mark.asyncio
async def test_presence_lists_recent_heartbeats_and_expires_stale_ones():
    router._presence_last.clear()
    clock = [1000.0]
    with (
        patch("routers.messaging.resolve_owner_id", return_value="owner_1"),
        patch("routers.messaging.time.monotonic", side_effect=lambda: clock[0]),
    ):
        await router.presence_heartbeat(auth=_auth("user_a"))
        clock[0] += 30
        await router.presence_heartbeat(auth=_auth("user_b"))
        assert (await router.presence_snapshot(auth=_auth("user_a")))["online"] == ["user_a", "user_b"]
        clock[0] += 70  # user_a is now 100s stale, user_b 70s
        assert (await router.presence_snapshot(auth=_auth("user_a")))["online"] == ["user_b"]
        assert "user_a" not in router._presence_last["owner_1"]


@pytest.mark.asyncio
async def test_presence_is_scoped_to_the_workspace():
    router._presence_last.clear()
    with patch("routers.messaging.resolve_owner_id", return_value="owner_1"):
        await router.presence_heartbeat(auth=_auth("user_a"))
    with patch("routers.messaging.resolve_owner_id", return_value="owner_2"):
        assert (await router.presence_snapshot(auth=_auth("user_z")))["online"] == []
