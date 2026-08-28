"""Unauthenticated Launch routes: /launch and /launch/waitlist."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

DROP = datetime.now(timezone.utc) + timedelta(days=10)


@pytest.fixture
def configured():
    with patch("core.services.launch_phase.settings.LAUNCH_DROP_AT", DROP.isoformat()):
        yield


@pytest.fixture(autouse=True)
def _fresh_bucket():
    from routers import launch

    launch._RATE.clear()
    launch._RESP_CACHE.clear()
    yield
    launch._RATE.clear()
    launch._RESP_CACHE.clear()


async def test_launch_info_404_when_unset(unauthenticated_async_client):
    with patch("core.services.launch_phase.settings.LAUNCH_DROP_AT", ""):
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
    assert body["waitlist_count"] == 42 and body["entrants"] == 3
    assert body["pot_cents"] == 100_000 + 3 * 1_000
    assert body["comp_slots"] == 220
    assert body["competition_start_at"] > body["drop_at"]
    assert r.headers["cache-control"] == "public, max-age=30"


@patch("routers.launch.launch_waitlist_repo")
async def test_join_waitlist_returns_position_and_code(mock_wl, configured, unauthenticated_async_client):
    mock_wl.normalize_email.side_effect = lambda s: s.strip().lower()
    mock_wl.join = AsyncMock(return_value={"email": "a@x.com", "position": 12, "referral_code": "ABCD2345"})
    r = await unauthenticated_async_client.post(
        "/api/v1/launch/waitlist", json={"email": " A@x.com", "ref": "K7QX2MPP"}
    )
    assert r.status_code == 200
    assert r.json() == {"position": 12, "referral_code": "ABCD2345", "in_comp_window": True}
    mock_wl.join.assert_awaited_once_with("a@x.com", referred_by="K7QX2MPP", source="waitlist")


@patch("routers.launch.launch_waitlist_repo")
async def test_join_waitlist_rejects_bad_email(mock_wl, configured, unauthenticated_async_client):
    mock_wl.normalize_email.side_effect = ValueError("invalid_email")
    r = await unauthenticated_async_client.post("/api/v1/launch/waitlist", json={"email": "nope"})
    assert r.status_code == 400 and r.json()["detail"] == "invalid_email"


@patch("routers.launch.launch_waitlist_repo")
async def test_join_waitlist_rate_limited_per_ip(mock_wl, configured, unauthenticated_async_client):
    mock_wl.normalize_email.side_effect = lambda s: s
    mock_wl.join = AsyncMock(return_value={"position": 1, "referral_code": "X"})
    for _ in range(10):
        assert (
            await unauthenticated_async_client.post("/api/v1/launch/waitlist", json={"email": "a@x.com"})
        ).status_code == 200
    r = await unauthenticated_async_client.post("/api/v1/launch/waitlist", json={"email": "a@x.com"})
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


async def test_join_waitlist_closed_after_drop(configured, unauthenticated_async_client):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with patch("core.services.launch_phase.settings.LAUNCH_DROP_AT", past):
        r = await unauthenticated_async_client.post("/api/v1/launch/waitlist", json={"email": "a@x.com"})
    assert r.status_code == 409 and r.json()["detail"] == "waitlist_closed"


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
