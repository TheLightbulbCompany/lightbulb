"""user.created fires a best-effort welcome email; failures never 500."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

_NOW = datetime.now(timezone.utc)
_WAITLIST_ROW = {
    "email": "ada@example.com",
    "position": 52,
    "referral_code": "ABCD1234",
    "referrals_signed_up": 1,
}

_PAYLOAD = {
    "type": "user.created",
    "data": {
        "id": "u_new",
        "first_name": "Ada",
        "email_addresses": [{"id": "ea_1", "email_address": "ada@example.com"}],
        "primary_email_address_id": "ea_1",
    },
}

_HEADERS = {"svix-id": "msg_welcome", "svix-timestamp": "1", "svix-signature": "ignored"}


@pytest.mark.asyncio
async def test_user_created_sends_welcome(async_client, monkeypatch):
    monkeypatch.setattr("routers.webhooks._verify_svix_signature", lambda body, headers: None)

    with (
        patch("core.repositories.user_repo.put", new=AsyncMock(return_value=None)),
        patch(
            "core.services.welcome_email.send_welcome_email",
            new=AsyncMock(return_value="email_1"),
        ) as mock_welcome,
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", json=_PAYLOAD, headers=_HEADERS)

    assert resp.status_code == 200
    mock_welcome.assert_awaited_once_with(to_email="ada@example.com", first_name="Ada")


@pytest.mark.asyncio
async def test_user_created_welcome_failure_is_non_fatal(async_client, monkeypatch):
    monkeypatch.setattr("routers.webhooks._verify_svix_signature", lambda body, headers: None)

    with (
        patch("core.repositories.user_repo.put", new=AsyncMock(return_value=None)),
        patch(
            "core.services.welcome_email.send_welcome_email",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", json=_PAYLOAD, headers=_HEADERS)

    # A welcome-send blowup must not 500 the webhook (Clerk would retry).
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_user_created_joins_launch_waitlist_when_open(async_client, monkeypatch):
    """Sign-up IS the waitlist now: user.created best-effort-joins the caller's
    email, with the extracted primary email and source="signup"."""
    monkeypatch.setattr("routers.webhooks._verify_svix_signature", lambda body, headers: None)
    headers = {**_HEADERS, "svix-id": "msg_launch_join"}

    with (
        patch("core.repositories.user_repo.put", new=AsyncMock(return_value=None)),
        patch("core.services.welcome_email.send_welcome_email", new=AsyncMock(return_value="email_1")),
        patch("core.services.launch_phase.schedule", return_value=object()),
        patch("core.repositories.launch_waitlist_repo.join", new=AsyncMock(return_value={})) as mock_join,
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", json=_PAYLOAD, headers=headers)

    assert resp.status_code == 200
    mock_join.assert_awaited_once_with("ada@example.com", referred_by=None, source="signup")


@pytest.mark.asyncio
async def test_user_created_skips_launch_waitlist_join_when_feature_off(async_client, monkeypatch):
    """No launch settings drop configured for this env -> no join attempt."""
    monkeypatch.setattr("routers.webhooks._verify_svix_signature", lambda body, headers: None)
    headers = {**_HEADERS, "svix-id": "msg_launch_off"}

    with (
        patch("core.repositories.user_repo.put", new=AsyncMock(return_value=None)),
        patch("core.services.welcome_email.send_welcome_email", new=AsyncMock(return_value="email_1")),
        patch("core.services.launch_phase.schedule", return_value=None),
        patch("core.repositories.launch_waitlist_repo.join", new=AsyncMock()) as mock_join,
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", json=_PAYLOAD, headers=headers)

    assert resp.status_code == 200
    mock_join.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_created_sends_waitlist_email_in_waitlist_phase(async_client, monkeypatch, launch_schedule):
    """Sign-up during the waitlist phase gets the "You're #N" confirmation
    instead of the generic welcome — one email, the right one."""
    from core.services import launch_service

    monkeypatch.setattr("routers.webhooks._verify_svix_signature", lambda body, headers: None)
    headers = {**_HEADERS, "svix-id": "msg_waitlist_email"}

    with (
        launch_schedule(_NOW + timedelta(days=3)),
        patch("core.repositories.user_repo.put", new=AsyncMock(return_value=None)),
        patch("core.repositories.launch_waitlist_repo.join", new=AsyncMock(return_value=_WAITLIST_ROW)),
        patch(
            "core.services.launch_waitlist_email.send_waitlist_email", new=AsyncMock(return_value="email_2")
        ) as mock_waitlist,
        patch("core.services.welcome_email.send_welcome_email", new=AsyncMock(return_value="email_1")) as mock_welcome,
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", json=_PAYLOAD, headers=headers)

    assert resp.status_code == 200
    mock_welcome.assert_not_awaited()
    call = mock_waitlist.call_args.kwargs
    assert call["to_email"] == "ada@example.com"
    assert call["first_name"] == "Ada"
    assert call["effective_position"] == 42  # position 52 - referral_jump 10 * 1 referral
    assert call["referral_jump"] == 10
    assert call["comp_slots"] == 220
    assert call["referral_link"] == launch_service.referral_link("ABCD1234")
    assert call["drop_at"] is not None


@pytest.mark.asyncio
async def test_user_created_sends_welcome_when_feature_off(async_client, monkeypatch, launch_schedule):
    """No launch settings drop configured for this env -> the welcome email
    is unchanged, and neither the join nor the waitlist email are attempted."""
    monkeypatch.setattr("routers.webhooks._verify_svix_signature", lambda body, headers: None)
    headers = {**_HEADERS, "svix-id": "msg_feature_off_email"}

    with (
        launch_schedule(),
        patch("core.repositories.user_repo.put", new=AsyncMock(return_value=None)),
        patch("core.repositories.launch_waitlist_repo.join", new=AsyncMock()) as mock_join,
        patch("core.services.launch_waitlist_email.send_waitlist_email", new=AsyncMock()) as mock_waitlist,
        patch("core.services.welcome_email.send_welcome_email", new=AsyncMock(return_value="email_1")) as mock_welcome,
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", json=_PAYLOAD, headers=headers)

    assert resp.status_code == 200
    mock_join.assert_not_awaited()
    mock_waitlist.assert_not_awaited()
    mock_welcome.assert_awaited_once_with(to_email="ada@example.com", first_name="Ada")


@pytest.mark.asyncio
async def test_user_created_sends_welcome_after_the_drop(async_client, monkeypatch, launch_schedule):
    """Once the competition has dropped (activation phase or later), sign-up
    goes back to the generic welcome email -- not the waitlist confirmation."""
    monkeypatch.setattr("routers.webhooks._verify_svix_signature", lambda body, headers: None)
    headers = {**_HEADERS, "svix-id": "msg_after_drop"}

    with (
        launch_schedule(_NOW - timedelta(hours=1)),
        patch("core.repositories.user_repo.put", new=AsyncMock(return_value=None)),
        patch("core.repositories.launch_waitlist_repo.join", new=AsyncMock(return_value=_WAITLIST_ROW)),
        patch("core.services.launch_waitlist_email.send_waitlist_email", new=AsyncMock()) as mock_waitlist,
        patch("core.services.welcome_email.send_welcome_email", new=AsyncMock(return_value="email_1")) as mock_welcome,
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", json=_PAYLOAD, headers=headers)

    assert resp.status_code == 200
    mock_waitlist.assert_not_awaited()
    mock_welcome.assert_awaited_once_with(to_email="ada@example.com", first_name="Ada")


@pytest.mark.asyncio
async def test_user_created_waitlist_email_failure_is_non_fatal(async_client, monkeypatch, launch_schedule):
    monkeypatch.setattr("routers.webhooks._verify_svix_signature", lambda body, headers: None)
    headers = {**_HEADERS, "svix-id": "msg_waitlist_boom"}

    with (
        launch_schedule(_NOW + timedelta(days=3)),
        patch("core.repositories.user_repo.put", new=AsyncMock(return_value=None)),
        patch("core.repositories.launch_waitlist_repo.join", new=AsyncMock(return_value=_WAITLIST_ROW)),
        patch(
            "core.services.launch_waitlist_email.send_waitlist_email",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch("core.services.welcome_email.send_welcome_email", new=AsyncMock(return_value="email_1")) as mock_welcome,
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", json=_PAYLOAD, headers=headers)

    # A waitlist-send blowup must not 500 the webhook (Clerk would retry).
    # It also does not fall back to the welcome email -- the choice of which
    # sign-up email to send is made before the send is attempted.
    assert resp.status_code == 200
    mock_welcome.assert_not_awaited()
