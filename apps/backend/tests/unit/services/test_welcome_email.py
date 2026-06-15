"""send_welcome_email renders the template and calls the email seam."""

from unittest.mock import AsyncMock, patch

import pytest

from core.config import settings
from core.services import welcome_email


@pytest.mark.asyncio
async def test_send_welcome_calls_seam_with_expected_fields(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.co")

    with patch(
        "core.services.welcome_email.email_service.send_email",
        new=AsyncMock(return_value="email_xyz"),
    ) as mock_send:
        result = await welcome_email.send_welcome_email(
            to_email="newuser@example.com", user_id="u_42", first_name="Ada"
        )

    assert result == "email_xyz"
    mock_send.assert_awaited_once()
    kwargs = mock_send.await_args.kwargs
    assert kwargs["to"] == "newuser@example.com"
    assert kwargs["from_addr"] == settings.EMAIL_FROM_WELCOME
    assert kwargs["reply_to"] == settings.EMAIL_SUPPORT_ADDRESS
    assert kwargs["idempotency_key"] == "welcome-user/u_42"
    assert kwargs["kind"] == "welcome"
    assert "Ada" in kwargs["html"]
    assert "https://app.example.co/chat" in kwargs["html"]


@pytest.mark.asyncio
async def test_send_welcome_without_first_name_uses_fallback(monkeypatch):
    with patch(
        "core.services.welcome_email.email_service.send_email",
        new=AsyncMock(return_value="email_xyz"),
    ) as mock_send:
        await welcome_email.send_welcome_email(to_email="newuser@example.com", user_id="u_42", first_name=None)

    html = mock_send.await_args.kwargs["html"]
    assert "there" in html  # "Hi there," fallback greeting
