"""Unauthenticated /launch route — the public competition-sheet payload."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

CLOSES = datetime.now(timezone.utc) + timedelta(days=10)


@pytest.fixture
def configured(launch_schedule):
    with launch_schedule(CLOSES):
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


@patch("routers.launch.launch_waitlist_repo")
async def test_launch_info_shape_and_no_pot_keys(mock_wl, configured, unauthenticated_async_client):
    mock_wl.count = AsyncMock(return_value=42)
    r = await unauthenticated_async_client.get("/api/v1/launch")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "phase",
        "sheet_closes_at",
        "competition_start_at",
        "competition_end_at",
        "signups",
        "comp_slots",
        "referral_jump",
    }
    # The pot is admin-only — NO pot key may ever appear in a public payload.
    assert not any("pot" in k for k in body)
    assert body["phase"] == "open"
    assert body["signups"] == 42
    assert body["comp_slots"] == 200
    assert body["referral_jump"] == 10
    assert body["competition_end_at"] > body["competition_start_at"] > body["sheet_closes_at"]
    assert r.headers["cache-control"] == "public, max-age=30"


@patch("routers.launch.launch_waitlist_repo")
async def test_launch_info_reports_closed_after_an_early_close(mock_wl, launch_schedule, unauthenticated_async_client):
    mock_wl.count = AsyncMock(return_value=42)
    with launch_schedule(CLOSES, closed_at=datetime.now(timezone.utc) - timedelta(hours=1)):
        r = await unauthenticated_async_client.get("/api/v1/launch")
    assert r.status_code == 200 and r.json()["phase"] == "closed"


async def test_waitlist_post_route_is_gone(configured, unauthenticated_async_client):
    r = await unauthenticated_async_client.post("/api/v1/launch/waitlist", json={"email": "a@x.com"})
    assert r.status_code in (404, 405)


@patch("routers.launch.launch_waitlist_repo")
async def test_launch_info_is_served_from_the_cache_within_the_ttl(mock_wl, configured, unauthenticated_async_client):
    """The public read is a table scan and the client controls Cache-Control."""
    mock_wl.count = AsyncMock(return_value=42)
    first = await unauthenticated_async_client.get("/api/v1/launch")
    mock_wl.count = AsyncMock(side_effect=AssertionError("should be cached"))
    second = await unauthenticated_async_client.get("/api/v1/launch")
    assert second.status_code == 200 and second.json() == first.json()
