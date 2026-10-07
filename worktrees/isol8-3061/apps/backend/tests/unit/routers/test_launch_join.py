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


@patch("routers.launch.launch_service.join_sheet", new_callable=AsyncMock)
async def test_join_rejects_active_workspace(mock_join, configured, async_client):
    r = await async_client.post("/api/v1/launch/join", json={"display_name": "Ada", "public": True})
    assert r.status_code == 409 and r.json()["detail"] == {"reason": "org_not_supported"}
    mock_join.assert_not_awaited()


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
        assert (await async_client.post("/api/v1/launch/join", json=body)).status_code == 409
    r = await async_client.post("/api/v1/launch/join", json=body)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


@patch("routers.launch.launch_service.join_sheet", AsyncMock(return_value={"joined": True}))
async def test_join_refused_for_every_org_actor(configured, async_client_org_member, async_client_org_admin):
    """Org contexts can't join at all — admin or member. The sheet row keys
    to the personal email/user_id while org billing keys by org_id, so every
    money leg would silently miss an org actor."""
    for client in (async_client_org_member, async_client_org_admin):
        r = await client.post("/api/v1/launch/join", json={"display_name": "Ada", "public": True})
        assert r.status_code == 409 and r.json()["detail"] == {"reason": "org_not_supported"}


async def test_enter_route_is_gone(configured, async_client):
    r = await async_client.post(
        "/api/v1/launch/enter", json={"display_name": "Ada", "stripe_key": "rk_test_" + "x" * 20, "public": True}
    )
    assert r.status_code in (404, 405)


# --- PATCH /display-name ----------------------------------------------------


@patch("routers.launch.launch_service.rename_display_name", AsyncMock(return_value={"display_name": "New Co"}))
async def test_rename_ok(configured, async_client):
    r = await async_client.patch("/api/v1/launch/display-name", json={"display_name": "New Co"})
    assert r.status_code == 200 and r.json() == {"display_name": "New Co"}


@pytest.mark.parametrize("name", ["", " ", "a", "   x   "])
async def test_rename_rejects_names_join_would_have_rejected(configured, async_client, name):
    """Same bounds as JoinRequest, strip-before-validate — otherwise a rename
    is a way to put a name on the public board that joining could not."""
    r = await async_client.patch("/api/v1/launch/display-name", json={"display_name": name})
    assert r.status_code == 422


@patch(
    "routers.launch.launch_service.rename_display_name",
    AsyncMock(side_effect=launch_service.LaunchRefused("not_joined")),
)
async def test_rename_refusal_is_a_400_with_the_reason(configured, async_client):
    r = await async_client.patch("/api/v1/launch/display-name", json={"display_name": "New Co"})
    assert r.status_code == 400 and r.json()["detail"] == "not_joined"
