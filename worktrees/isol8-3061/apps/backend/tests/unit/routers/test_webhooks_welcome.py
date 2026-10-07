"""user.created fires a best-effort welcome email, unconditionally; failures
never 500. Sign-up joins NOTHING — competition-sheet membership is the
explicit POST /launch/join only."""

from datetime import datetime, timedelta, timezone
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
async def test_user_created_sends_welcome_even_while_the_sheet_is_open(async_client, monkeypatch, launch_schedule):
    """The launch feature being live changes NOTHING about signup: still the
    generic welcome email, still no sheet row minted."""
    monkeypatch.setattr("routers.webhooks._verify_svix_signature", lambda body, headers: None)
    headers = {**_HEADERS, "svix-id": "msg_sheet_open"}
    closes = datetime.now(timezone.utc) + timedelta(days=10)

    with (
        launch_schedule(closes),
        patch("core.repositories.user_repo.put", new=AsyncMock(return_value=None)),
        patch("core.services.welcome_email.send_welcome_email", new=AsyncMock(return_value="email_1")) as welcome,
        patch("core.repositories.launch_waitlist_repo.join", new=AsyncMock()) as mock_join,
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", json=_PAYLOAD, headers=headers)

    assert resp.status_code == 200
    welcome.assert_awaited_once()
    mock_join.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_created_without_email_sends_nothing(async_client, monkeypatch):
    monkeypatch.setattr("routers.webhooks._verify_svix_signature", lambda body, headers: None)
    payload = {"type": "user.created", "data": {"id": "u_new", "email_addresses": []}}
    headers = {**_HEADERS, "svix-id": "msg_no_email"}

    with (
        patch("core.repositories.user_repo.put", new=AsyncMock(return_value=None)),
        patch("core.services.welcome_email.send_welcome_email", new=AsyncMock()) as welcome,
    ):
        resp = await async_client.post("/api/v1/webhooks/clerk", json=payload, headers=headers)

    assert resp.status_code == 200
    welcome.assert_not_awaited()
