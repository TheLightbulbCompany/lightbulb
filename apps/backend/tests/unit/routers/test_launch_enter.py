from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.services import launch_service

DROP = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


@pytest.fixture(autouse=True)
def _fresh_state():
    from routers import launch

    launch._RATE.clear()
    launch._RESP_CACHE.clear()
    yield
    launch._RATE.clear()
    launch._RESP_CACHE.clear()


@pytest.fixture
def configured():
    with patch("core.services.launch_phase.settings.LAUNCH_DROP_AT", DROP):
        yield


@patch("routers.launch.launch_service.enter", AsyncMock(return_value={"display_name": "Ada"}))
async def test_enter_ok(configured, async_client):
    r = await async_client.post(
        "/api/v1/launch/enter", json={"display_name": "Ada", "stripe_key": "rk_test_" + "x" * 20, "public": True}
    )
    assert r.status_code == 200 and r.json()["display_name"] == "Ada"


@patch("routers.launch.launch_service.enter", AsyncMock(side_effect=launch_service.EnterRefused("stripe_auth")))
async def test_enter_refused_maps_to_400_reason_and_never_echoes_key(configured, async_client):
    key = "rk_test_" + "s3cr3t" * 4
    r = await async_client.post("/api/v1/launch/enter", json={"display_name": "Ada", "stripe_key": key})
    assert r.status_code == 400 and r.json()["detail"] == "stripe_auth"
    assert key not in r.text


async def test_enter_validates_body(configured, async_client):
    r = await async_client.post("/api/v1/launch/enter", json={"display_name": "A", "stripe_key": "short"})
    assert r.status_code == 422


@patch(
    "routers.launch.launch_service.leaderboard",
    AsyncMock(
        return_value={
            "entrants": 1,
            "rows": [{"rank": 1, "display_name": "A", "delta_cents": 5, "pct": None, "currency": "usd"}],
        }
    ),
)
async def test_leaderboard_public_cached(configured, unauthenticated_async_client):
    r = await unauthenticated_async_client.get("/api/v1/launch/leaderboard")
    assert r.status_code == 200 and r.json()["rows"][0]["rank"] == 1
    assert r.headers["cache-control"] == "public, max-age=60"


@patch("routers.launch.launch_revenue.maybe_revalidate")
@patch("routers.launch.launch_service.leaderboard", AsyncMock(return_value={"entrants": 0, "rows": []}))
async def test_leaderboard_read_triggers_revalidate(reval, configured, unauthenticated_async_client):
    await unauthenticated_async_client.get("/api/v1/launch/leaderboard")
    reval.assert_called_once()


@patch("routers.launch.launch_revenue.maybe_revalidate")
@patch("routers.launch.launch_service.me", AsyncMock(return_value={"waitlist": None}))
async def test_me_read_triggers_revalidate(reval, configured, async_client):
    await async_client.get("/api/v1/launch/me")
    reval.assert_called_once()


@patch("routers.launch.launch_service.enter", AsyncMock(return_value={"display_name": "Ada"}))
async def test_enter_is_rate_limited_per_ip(configured, async_client):
    body = {"display_name": "Ada", "stripe_key": "rk_test_" + "x" * 20, "public": True}
    for _ in range(10):
        assert (await async_client.post("/api/v1/launch/enter", json=body)).status_code == 200
    r = await async_client.post("/api/v1/launch/enter", json=body)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
