"""Unauthenticated /launch route. Sign-up IS the waitlist now — no email form."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

DROP = datetime.now(timezone.utc) + timedelta(days=10)


@pytest.fixture
def configured(launch_schedule):
    with launch_schedule(DROP):
        yield


@pytest.fixture(autouse=True)
def _fresh_bucket():
    from routers import launch

    launch._RATE.clear()
    launch._RESP_CACHE.clear()
    yield
    launch._RATE.clear()
    launch._RESP_CACHE.clear()


async def test_launch_info_404_when_unset(launch_schedule, unauthenticated_async_client):
    with launch_schedule():
        r = await unauthenticated_async_client.get("/api/v1/launch")
    assert r.status_code == 404


@patch("routers.launch.launch_entries_repo")
@patch("routers.launch.launch_waitlist_repo")
async def test_launch_info_shape(mock_wl, mock_entries, configured, unauthenticated_async_client):
    mock_wl.count = AsyncMock(return_value=42)
    mock_entries.count = AsyncMock(return_value=3)
    r = await unauthenticated_async_client.get("/api/v1/launch")
    assert r.status_code == 200
    body = r.json()
    assert body["phase"] == "waitlist"
    assert body["signups"] == 42
    assert body["competing"] == 3
    assert body["on_board"] == 3  # Task 4 switches this to count_with_key()
    assert body["pot_cents"] == 100_000 + 42 * 1_000  # pot is driven by signups, not entrants
    assert body["comp_slots"] == 220
    assert body["pot_per_entrant_cents"] == 1_000
    assert body["competition_start_at"] > body["drop_at"]
    assert r.headers["cache-control"] == "public, max-age=30"


async def test_waitlist_post_route_is_gone(configured, unauthenticated_async_client):
    r = await unauthenticated_async_client.post("/api/v1/launch/waitlist", json={"email": "a@x.com"})
    assert r.status_code in (404, 405)


@patch("routers.launch.launch_entries_repo")
@patch("routers.launch.launch_waitlist_repo")
async def test_launch_info_is_served_from_the_cache_within_the_ttl(
    mock_wl, mock_entries, configured, unauthenticated_async_client
):
    """The public read is a full table scan and the client controls Cache-Control."""
    mock_wl.count = AsyncMock(return_value=42)
    mock_entries.count = AsyncMock(return_value=3)
    first = await unauthenticated_async_client.get("/api/v1/launch")
    mock_entries.count = AsyncMock(side_effect=AssertionError("should be cached"))
    second = await unauthenticated_async_client.get("/api/v1/launch")
    assert second.status_code == 200 and second.json() == first.json()
