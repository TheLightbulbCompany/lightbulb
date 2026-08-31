from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.services import launch_service

CLOSES = datetime.now(timezone.utc) + timedelta(days=10)


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
    with launch_schedule(CLOSES):
        yield


# --- POST /join -------------------------------------------------------------


@patch("routers.launch.launch_service.join_sheet", AsyncMock(return_value={"joined": True, "position": 7}))
async def test_join_ok(configured, async_client):
    r = await async_client.post("/api/v1/launch/join", json={"display_name": "Ada", "public": True})
    assert r.status_code == 200 and r.json()["joined"] is True


@patch("routers.launch.launch_service.join_sheet", AsyncMock(side_effect=launch_service.LaunchRefused("not_onboarded")))
async def test_join_not_onboarded_is_409_with_a_reason(configured, async_client):
    r = await async_client.post("/api/v1/launch/join", json={"display_name": "Ada", "public": True})
    assert r.status_code == 409 and r.json()["detail"] == {"reason": "not_onboarded"}


@patch("routers.launch.launch_service.join_sheet", AsyncMock(side_effect=launch_service.LaunchRefused("closed")))
async def test_join_closed_is_409_with_a_reason(configured, async_client):
    r = await async_client.post("/api/v1/launch/join", json={"display_name": "Ada", "public": True})
    assert r.status_code == 409 and r.json()["detail"] == {"reason": "closed"}


@patch("routers.launch.launch_service.join_sheet", AsyncMock(side_effect=launch_service.LaunchRefused("no_email")))
async def test_other_refusals_stay_400(configured, async_client):
    r = await async_client.post("/api/v1/launch/join", json={"display_name": "Ada", "public": True})
    assert r.status_code == 400 and r.json()["detail"] == "no_email"


async def test_join_404s_when_the_feature_is_off(launch_schedule, async_client):
    with launch_schedule():
        r = await async_client.post("/api/v1/launch/join", json={"display_name": "Ada", "public": True})
    assert r.status_code == 404


async def test_join_validates_body(configured, async_client):
    r = await async_client.post("/api/v1/launch/join", json={"display_name": "A"})
    assert r.status_code == 422


async def test_join_rejects_a_whitespace_only_display_name(configured, async_client):
    # Stripped BEFORE min_length, or "  " passes the check and lands as "".
    r = await async_client.post("/api/v1/launch/join", json={"display_name": "  ", "public": True})
    assert r.status_code == 422


@patch("routers.launch.launch_service.join_sheet", AsyncMock(return_value={"joined": True}))
async def test_join_is_rate_limited_per_ip(configured, async_client):
    body = {"display_name": "Ada", "public": True}
    for _ in range(10):
        assert (await async_client.post("/api/v1/launch/join", json=body)).status_code == 200
    r = await async_client.post("/api/v1/launch/join", json=body)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


async def test_join_refused_for_an_org_member(configured, async_client_org_member):
    r = await async_client_org_member.post("/api/v1/launch/join", json={"display_name": "Ada", "public": True})
    assert r.status_code == 403 and r.json()["detail"] == "org_admin_required"


@patch("routers.launch.launch_service.join_sheet", AsyncMock(return_value={"joined": True}))
async def test_join_allowed_for_an_org_admin(configured, async_client_org_admin):
    r = await async_client_org_admin.post("/api/v1/launch/join", json={"display_name": "Ada", "public": True})
    assert r.status_code == 200


async def test_enter_route_is_gone(configured, async_client):
    r = await async_client.post(
        "/api/v1/launch/enter", json={"display_name": "Ada", "stripe_key": "rk_test_" + "x" * 20, "public": True}
    )
    assert r.status_code in (404, 405)
