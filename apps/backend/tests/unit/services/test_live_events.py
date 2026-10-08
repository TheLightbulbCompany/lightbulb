from unittest.mock import AsyncMock

import pytest

from core.services import live_events
from core.services.messaging import make_live_event

ROOM = "22222222-2222-4222-8222-222222222222"
KEY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
WS = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_publish_room_uses_the_workspace_scoped_path(monkeypatch):
    seen = []
    publisher = live_events.LiveEventPublisher()
    monkeypatch.setattr(publisher, "_publish", AsyncMock(side_effect=lambda channel, event: seen.append(channel)))
    monkeypatch.setattr(
        live_events.messaging_repo, "get_room", AsyncMock(return_value={"deliveryKey": KEY, "workspaceId": WS})
    )
    assert await publisher.publish_room(ROOM, make_live_event("room.typing.changed", {"roomId": ROOM}, room_id=ROOM))
    assert seen == [f"/ws/{WS}/rooms/{ROOM}/{KEY}"]


@pytest.mark.asyncio
async def test_publish_room_heals_a_room_without_a_workspace_id(monkeypatch):
    """A META row that predates workspaceId (roll-window rows, fixtures) is healed
    lazily rather than refused — see core.repositories.messaging_repo.workspace_id_for_meta."""
    seen = []
    publisher = live_events.LiveEventPublisher()
    monkeypatch.setattr(publisher, "_publish", AsyncMock(side_effect=lambda channel, event: seen.append(channel)))
    meta = {"deliveryKey": KEY, "ownerId": "org_1"}
    monkeypatch.setattr(live_events.messaging_repo, "get_room", AsyncMock(return_value=meta))
    heal = AsyncMock(return_value=WS)
    monkeypatch.setattr(live_events.messaging_repo, "workspace_id_for_meta", heal)
    assert await publisher.publish_room(ROOM, make_live_event("room.typing.changed", {"roomId": ROOM}, room_id=ROOM))
    assert seen == [f"/ws/{WS}/rooms/{ROOM}/{KEY}"]
    heal.assert_awaited_once_with(live_events.messaging_repo.room_pk(ROOM), "META", meta)


@pytest.mark.asyncio
async def test_publish_room_frames_uses_the_sibling_frames_path(monkeypatch):
    """AppSync caps a channel at 5 segments, so view frames go on /ws/<ws>/frames/<room>/<key>,
    the room channel with `rooms` swapped for `frames` — never appended to it."""
    seen = []
    publisher = live_events.LiveEventPublisher()
    monkeypatch.setattr(publisher, "_publish", AsyncMock(side_effect=lambda channel, event: seen.append(channel)))
    monkeypatch.setattr(
        live_events.messaging_repo, "get_room", AsyncMock(return_value={"deliveryKey": KEY, "workspaceId": WS})
    )
    event = make_live_event("room.browser.frame", {"roomId": ROOM}, room_id=ROOM)
    assert await publisher.publish_room(ROOM, event, frames=True)
    assert seen == [f"/ws/{WS}/frames/{ROOM}/{KEY}"]


@pytest.mark.asyncio
async def test_post_reuses_one_keep_alive_client(monkeypatch):
    posts = []

    class FakeClient:
        def __init__(self, **kw):
            posts.append(("new", kw["timeout"]))

        async def post(self, endpoint, content, headers):
            posts.append("post")
            return "ok"

    monkeypatch.setattr(live_events, "_http_client", None)
    monkeypatch.setattr(live_events.httpx, "AsyncClient", FakeClient)
    publisher = live_events.LiveEventPublisher()
    assert await publisher._post("https://x", b"{}", {}) == "ok"
    assert await publisher._post("https://x", b"{}", {}) == "ok"
    assert posts == [("new", live_events._TIMEOUT), "post", "post"]


@pytest.mark.asyncio
async def test_user_channel_is_none_without_a_user_stream(monkeypatch):
    monkeypatch.setattr(live_events.messaging_repo, "get_user_stream", AsyncMock(return_value=None))
    assert await live_events.LiveEventPublisher().user_channel("org_a", "user_a") is None


@pytest.mark.asyncio
async def test_publishes_in_flight_are_bounded_so_a_burst_queues_instead_of_exhausting_the_pool(monkeypatch):
    """51 members x 30 session registrations (2026-10-08, dev) drove thousands of concurrent publishes
    into the default pool and every one past it failed with PoolTimeout; now at most
    _MAX_INFLIGHT_PUBLISHES post at once and the rest wait."""
    import asyncio
    from types import SimpleNamespace

    monkeypatch.setattr(live_events, "_inflight", None)
    monkeypatch.setattr(live_events, "_MAX_INFLIGHT_PUBLISHES", 4)
    live, peak = {"n": 0}, {"max": 0}

    class Fake:
        async def post(self, *_a, **_k):
            live["n"] += 1
            peak["max"] = max(peak["max"], live["n"])
            await asyncio.sleep(0.01)
            live["n"] -= 1
            return SimpleNamespace(status_code=200, json=lambda: {"successful": [{}], "failed": []})

    publisher = live_events.LiveEventPublisher(http_client=Fake())
    await asyncio.gather(*(publisher._post("https://x", b"{}", {}) for _ in range(40)))
    assert peak["max"] == 4


async def _fake_signed_request(endpoint, body):
    from types import SimpleNamespace

    return SimpleNamespace(headers={"content-type": "application/json"})


@pytest.mark.asyncio
async def test_a_publish_that_cannot_get_a_slot_in_time_is_dropped_not_stalled(monkeypatch):
    """Codex on #3908: the wait for a slot must be bounded, or a slow AppSync stalls every request
    path that fans out hints (a desktop registration ack has 10 s)."""
    import asyncio
    from types import SimpleNamespace

    monkeypatch.setattr(live_events, "_inflight", None)
    monkeypatch.setattr(live_events, "_MAX_INFLIGHT_PUBLISHES", 1)
    monkeypatch.setattr(live_events, "_INFLIGHT_WAIT_S", 0.05)
    metrics = []
    monkeypatch.setattr(live_events, "put_metric", lambda *a, **k: metrics.append(k.get("dimensions")))
    release = asyncio.Event()

    class Slow:
        async def post(self, *_a, **_k):
            await release.wait()
            return SimpleNamespace(status_code=200, json=lambda: {"successful": [{}], "failed": []})

    publisher = live_events.LiveEventPublisher(http_client=Slow())
    first = asyncio.create_task(publisher._post("https://x", b"{}", {}))
    await asyncio.sleep(0)  # the first holds the only slot
    with pytest.raises(live_events.LivePublishQueueFull):
        await publisher._post("https://x", b"{}", {})
    # Through _send the drop is terminal: no retry, one queue_full sample, no transport sample.
    monkeypatch.setattr(live_events.settings, "APPSYNC_EVENTS_HTTP_ENDPOINT", "https://events.test/event")
    monkeypatch.setattr(publisher, "_signed_request", _fake_signed_request)
    with pytest.raises(live_events.LivePublishQueueFull):
        await publisher._send("/ws/w/users/k", '{"type":"x"}')
    assert [m["status_class"] for m in metrics if m and m.get("status_class")] == ["queue_full", "queue_full"]
    release.set()
    await first
