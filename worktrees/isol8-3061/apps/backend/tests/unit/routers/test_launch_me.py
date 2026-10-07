from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

CLOSES = datetime.now(timezone.utc) + timedelta(days=10)


@pytest.fixture(autouse=True)
def _fresh_state():
    from routers import launch

    launch._RATE.clear()
    yield
    launch._RATE.clear()


@pytest.fixture
def configured(launch_schedule):
    with launch_schedule(CLOSES):
        yield


async def test_me_requires_auth(configured, unauthenticated_async_client):
    assert (await unauthenticated_async_client.get("/api/v1/launch/me")).status_code in (401, 403)


@patch("routers.launch.launch_service.me", AsyncMock(return_value={"joined": False, "onboarded": False}))
async def test_me_delegates(configured, async_client):
    r = await async_client.get("/api/v1/launch/me")
    assert r.status_code == 200 and r.json() == {"joined": False, "onboarded": False}


@patch("routers.launch.launch_waitlist_repo")
@patch("routers.launch.billing_repo")
@patch("routers.launch.launch_service.resolve_email", AsyncMock(return_value="me@x.com"))
async def test_referral_attributes_when_no_subscription(mock_billing, mock_wl, configured, async_client):
    mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
    mock_wl.join = AsyncMock(return_value={"email": "me@x.com"})
    mock_wl.attribute = AsyncMock(return_value=True)
    r = await async_client.post("/api/v1/launch/referral", json={"ref": "k7qx2mpp"})
    assert r.status_code == 200 and r.json() == {"attributed": True}
    mock_wl.attribute.assert_awaited_once_with("me@x.com", "K7QX2MPP")


@patch("routers.launch.billing_repo")
@patch("routers.launch.launch_service.resolve_email", AsyncMock(return_value="me@x.com"))
async def test_referral_refused_once_subscribed(mock_billing, configured, async_client):
    mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
    r = await async_client.post("/api/v1/launch/referral", json={"ref": "K7QX2MPP"})
    assert r.status_code == 200 and r.json() == {"attributed": False}


@patch("routers.launch.launch_waitlist_repo")
@patch("routers.launch.billing_repo")
@patch("routers.launch.launch_service.resolve_email", AsyncMock(return_value="me@x.com"))
async def test_referral_is_rate_limited_per_ip(mock_billing, mock_wl, configured, async_client):
    # It writes to the sheet table with a caller-supplied code, and the
    # {attributed} response confirms code existence — enumeration risk
    # without a limit. Mirrors test_join_is_rate_limited_per_ip.
    mock_billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
    mock_wl.join = AsyncMock(return_value={"email": "me@x.com"})
    mock_wl.attribute = AsyncMock(return_value=True)
    body = {"ref": "K7QX2MPP"}
    for _ in range(10):
        assert (await async_client.post("/api/v1/launch/referral", json=body)).status_code == 200
    r = await async_client.post("/api/v1/launch/referral", json=body)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
