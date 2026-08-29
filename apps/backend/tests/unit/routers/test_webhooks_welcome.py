"""user.created fires a best-effort welcome email; failures never 500."""

from unittest.mock import AsyncMock, patch

import pytest

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
    """No launch-schedule.json drop configured for this env -> no join attempt."""
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
