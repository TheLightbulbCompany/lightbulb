"""support_ticket.submit_ticket enriches + delivers a ticket."""

import html
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.config import settings
from core.services import support_ticket
from schemas.support import SupportTicketRequest


@pytest.mark.asyncio
async def test_submit_ticket_enriches_and_sends(monkeypatch):
    monkeypatch.setattr(settings, "POSTHOG_PROJECT_ID", "380894")
    auth = AuthContext(user_id="user_123", email="ada@example.com")
    req = SupportTicketRequest(message="it broke", category="Bug report", route="/chat")

    with (
        patch(
            "core.services.support_ticket.container_repo.get_by_owner_id",
            new=AsyncMock(return_value={"status": "running", "service_name": "openclaw-user_123-abc"}),
        ),
        patch(
            "core.services.support_ticket.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"subscription_status": "active", "provider_choice": "bedrock_claude"}),
        ),
        patch(
            "core.services.support_ticket.email_service.send_email",
            new=AsyncMock(return_value="email_1"),
        ) as mock_send,
    ):
        result = await support_ticket.submit_ticket(auth, req)

    assert result == "email_1"
    kwargs = mock_send.await_args.kwargs
    assert kwargs["from_addr"] == settings.EMAIL_FROM_TICKETS
    assert kwargs["to"] == settings.EMAIL_SUPPORT_ADDRESS
    assert kwargs["reply_to"] == "ada@example.com"
    assert kwargs["kind"] == "support_ticket"
    body = kwargs["html"]
    assert "Bug report" in body
    assert "```json" in body
    assert "user_123" in body
    assert "running" in body  # container status
    assert "bedrock_claude" in body  # provider
    assert "380894" in body  # posthog person link


@pytest.mark.asyncio
async def test_submit_ticket_escapes_user_message(monkeypatch):
    auth = AuthContext(user_id="user_1", email="x@y.com")
    req = SupportTicketRequest(message="<script>alert(1)</script>")

    with (
        patch("core.services.support_ticket.container_repo.get_by_owner_id", new=AsyncMock(return_value=None)),
        patch("core.services.support_ticket.billing_repo.get_by_owner_id", new=AsyncMock(return_value=None)),
        patch(
            "core.services.support_ticket.email_service.send_email",
            new=AsyncMock(return_value="id"),
        ) as mock_send,
    ):
        await support_ticket.submit_ticket(auth, req)

    body = mock_send.await_args.kwargs["html"]
    assert "<script>" not in body
    assert html.escape("<script>alert(1)</script>") in body
