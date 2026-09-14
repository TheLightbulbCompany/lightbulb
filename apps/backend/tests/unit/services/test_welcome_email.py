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
        result = await welcome_email.send_welcome_email(to_email="newuser@example.com", first_name="Ada")

    assert result == "email_xyz"
    mock_send.assert_awaited_once()
    kwargs = mock_send.await_args.kwargs
    assert kwargs["to"] == "newuser@example.com"
    assert kwargs["from_addr"] == settings.EMAIL_FROM_WELCOME
    assert kwargs["reply_to"] == settings.EMAIL_SUPPORT_ADDRESS
    assert kwargs["kind"] == "welcome"
    assert "Ada" in kwargs["html"]
    assert "https://app.example.co/chat" in kwargs["html"]


@pytest.mark.asyncio
async def test_send_welcome_without_first_name_uses_fallback(monkeypatch):
    with patch(
        "core.services.welcome_email.email_service.send_email",
        new=AsyncMock(return_value="email_xyz"),
    ) as mock_send:
        await welcome_email.send_welcome_email(to_email="newuser@example.com", first_name=None)

    html = mock_send.await_args.kwargs["html"]
    assert "there" in html  # "Hi there," fallback greeting


@pytest.mark.asyncio
async def test_send_welcome_escapes_html_in_first_name():
    """A first_name with markup must be escaped, not injected into the email."""
    with patch(
        "core.services.welcome_email.email_service.send_email",
        new=AsyncMock(return_value="email_xyz"),
    ) as mock_send:
        await welcome_email.send_welcome_email(to_email="x@y.com", first_name="<script>alert(1)</script>")

    body = mock_send.await_args.kwargs["html"]
    assert "<script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


@pytest.mark.asyncio
async def test_send_welcome_includes_plaintext_alternative(monkeypatch):
    """HTML-only mail is a deliverability penalty and renders badly in some
    clients, so the welcome email must carry a text/plain part carrying the
    same links — not an empty string, which would produce a blank part."""
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.co")

    with patch(
        "core.services.welcome_email.email_service.send_email",
        new=AsyncMock(return_value="email_xyz"),
    ) as mock_send:
        await welcome_email.send_welcome_email(to_email="x@y.com", first_name="Ada")

    text = mock_send.await_args.kwargs["text"]
    assert text and text.strip()
    assert "Ada" in text
    assert "<" not in text, "the plain-text part must not contain markup"
    for link in (
        "https://app.example.co/chat",
        "https://app.example.co/marketplace",
        "https://app.example.co/blog",
        "https://app.example.co/docs",
    ):
        assert link in text
        assert link in mock_send.await_args.kwargs["html"]


@pytest.mark.asyncio
async def test_send_welcome_does_not_double_slash_urls(monkeypatch):
    """FRONTEND_URL with a trailing slash must not yield //chat."""
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.co/")

    with patch(
        "core.services.welcome_email.email_service.send_email",
        new=AsyncMock(return_value="email_xyz"),
    ) as mock_send:
        await welcome_email.send_welcome_email(to_email="x@y.com")

    kwargs = mock_send.await_args.kwargs
    assert "https://app.example.co/chat" in kwargs["html"]
    assert "//chat" not in kwargs["html"].replace("https://", "")
    assert "//chat" not in kwargs["text"].replace("https://", "")
