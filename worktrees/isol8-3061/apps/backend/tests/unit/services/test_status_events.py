"""Best-effort private status invalidation tests."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from core.services.status_events import STATUS_CHANGED_EVENT, publish_status_changed


@pytest.mark.asyncio
async def test_publish_status_changed_uses_private_appsync_invalidation():
    called = asyncio.Event()
    captured = {}

    async def publish(owner_id, event):
        captured.update(owner_id=owner_id, event=event)
        called.set()

    with patch("core.services.live_events.publish_owner_users", side_effect=publish):
        publish_status_changed("owner-1")
        await asyncio.wait_for(called.wait(), timeout=1)

    assert captured == {
        "owner_id": "owner-1",
        "event": {
            "schemaVersion": 1,
            "type": STATUS_CHANGED_EVENT,
            "eventId": captured["event"]["eventId"],
            "occurredAt": captured["event"]["occurredAt"],
            "payload": {"resource": "onboarding"},
        },
    }


def test_publish_status_changed_noop_for_falsy_owner_id():
    with patch("core.services.live_events.publish_owner_users", new_callable=AsyncMock) as publish:
        publish_status_changed(None)
        publish_status_changed("")
    publish.assert_not_called()


@pytest.mark.asyncio
async def test_publish_status_changed_swallows_publish_errors():
    called = asyncio.Event()

    async def fail(*_args):
        called.set()
        raise RuntimeError("boom")

    with patch("core.services.live_events.publish_owner_users", side_effect=fail):
        publish_status_changed("owner-1")
        await asyncio.wait_for(called.wait(), timeout=1)
