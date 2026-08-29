from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.services import launch_service

DROP = datetime.now(timezone.utc) - timedelta(days=1)


@pytest.fixture(autouse=True)
def _fresh_state():
    from routers import launch

    launch._RATE.clear()
    launch._RESP_CACHE.clear()
    yield
    launch._RATE.clear()
    launch._RESP_CACHE.clear()


@pytest.fixture
def configured(launch_schedule):
    with launch_schedule(DROP):
        yield


# --- POST /join -------------------------------------------------------------


@patch("routers.launch.launch_service.join", AsyncMock(return_value={"display_name": "Ada", "has_key": False}))
async def test_join_ok(configured, async_client):
    r = await async_client.post("/api/v1/launch/join", json={"display_name": "Ada", "public": True})
    assert r.status_code == 200 and r.json()["display_name"] == "Ada"


@patch("routers.launch.launch_service.join", AsyncMock(side_effect=launch_service.LaunchRefused("not_activated")))
async def test_join_refused_maps_to_400_reason(configured, async_client):
    r = await async_client.post("/api/v1/launch/join", json={"display_name": "Ada", "public": True})
    assert r.status_code == 400 and r.json()["detail"] == "not_activated"


async def test_join_validates_body(configured, async_client):
    r = await async_client.post("/api/v1/launch/join", json={"display_name": "A"})
    assert r.status_code == 422


@patch("routers.launch.launch_service.join", AsyncMock(return_value={"display_name": "Ada"}))
async def test_join_is_rate_limited_per_ip(configured, async_client):
    body = {"display_name": "Ada", "public": True}
    for _ in range(10):
        assert (await async_client.post("/api/v1/launch/join", json=body)).status_code == 200
    r = await async_client.post("/api/v1/launch/join", json=body)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


async def test_join_refused_for_an_org_member(configured, async_client_org_member):
    r = await async_client_org_member.post("/api/v1/launch/join", json={"display_name": "Ada", "public": True})
    assert r.status_code == 403 and r.json()["detail"] == "org_admin_required"


@patch("routers.launch.launch_service.join", AsyncMock(return_value={"display_name": "Ada"}))
async def test_join_allowed_for_an_org_admin(configured, async_client_org_admin):
    r = await async_client_org_admin.post("/api/v1/launch/join", json={"display_name": "Ada", "public": True})
    assert r.status_code == 200


async def test_enter_route_is_gone(configured, async_client):
    r = await async_client.post(
        "/api/v1/launch/enter", json={"display_name": "Ada", "stripe_key": "rk_test_" + "x" * 20, "public": True}
    )
    assert r.status_code in (404, 405)


# --- POST /connect-stripe ----------------------------------------------------


@patch("routers.launch.launch_service.connect_stripe", AsyncMock(return_value={"has_key": True}))
async def test_connect_stripe_ok(configured, async_client):
    r = await async_client.post("/api/v1/launch/connect-stripe", json={"stripe_key": "rk_test_" + "x" * 20})
    assert r.status_code == 200 and r.json()["has_key"] is True


@patch("routers.launch.launch_service.connect_stripe", AsyncMock(side_effect=launch_service.LaunchRefused("no_entry")))
async def test_connect_stripe_refused_maps_to_400_and_never_echoes_key(configured, async_client):
    key = "rk_test_" + "s3cr3t" * 4
    r = await async_client.post("/api/v1/launch/connect-stripe", json={"stripe_key": key})
    assert r.status_code == 400 and r.json()["detail"] == "no_entry"
    assert key not in r.text


async def test_connect_stripe_validates_body(configured, async_client):
    r = await async_client.post("/api/v1/launch/connect-stripe", json={"stripe_key": "short"})
    assert r.status_code == 422


@patch("routers.launch.launch_service.connect_stripe", AsyncMock(return_value={"has_key": True}))
async def test_connect_stripe_is_rate_limited_per_ip(configured, async_client):
    body = {"stripe_key": "rk_test_" + "x" * 20}
    for _ in range(10):
        assert (await async_client.post("/api/v1/launch/connect-stripe", json=body)).status_code == 200
    r = await async_client.post("/api/v1/launch/connect-stripe", json=body)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


async def test_connect_stripe_refused_for_an_org_member(configured, async_client_org_member):
    r = await async_client_org_member.post("/api/v1/launch/connect-stripe", json={"stripe_key": "rk_test_" + "x" * 20})
    assert r.status_code == 403 and r.json()["detail"] == "org_admin_required"


@patch("routers.launch.launch_service.connect_stripe", AsyncMock(return_value={"has_key": True}))
async def test_connect_stripe_allowed_for_an_org_admin(configured, async_client_org_admin):
    r = await async_client_org_admin.post("/api/v1/launch/connect-stripe", json={"stripe_key": "rk_test_" + "x" * 20})
    assert r.status_code == 200


# --- leaderboard / me (unaffected by the split, still exercised here) -------


@patch(
    "routers.launch.launch_service.leaderboard",
    AsyncMock(
        return_value={
            "competing": 2,
            "on_board": 1,
            "rows": [{"rank": 1, "display_name": "A", "delta_cents": 5, "pct": None, "currency": "usd"}],
        }
    ),
)
async def test_leaderboard_public_cached(configured, unauthenticated_async_client):
    r = await unauthenticated_async_client.get("/api/v1/launch/leaderboard")
    assert r.status_code == 200 and r.json()["rows"][0]["rank"] == 1
    assert r.json()["competing"] == 2 and r.json()["on_board"] == 1
    assert r.headers["cache-control"] == "public, max-age=60"


@patch("routers.launch.launch_revenue.maybe_revalidate")
@patch("routers.launch.launch_service.leaderboard", AsyncMock(return_value={"competing": 0, "on_board": 0, "rows": []}))
async def test_leaderboard_read_triggers_revalidate(reval, configured, unauthenticated_async_client):
    await unauthenticated_async_client.get("/api/v1/launch/leaderboard")
    reval.assert_called_once()


@patch("routers.launch.launch_revenue.maybe_revalidate")
@patch("routers.launch.launch_service.me", AsyncMock(return_value={"waitlist": None}))
async def test_me_read_triggers_revalidate(reval, configured, async_client):
    await async_client.get("/api/v1/launch/me")
    reval.assert_called_once()
