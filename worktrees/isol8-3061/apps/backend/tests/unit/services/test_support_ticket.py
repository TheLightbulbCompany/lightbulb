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
    auth = AuthContext(user_id="human_123", org_id="user_123", org_role="org:admin", email="ada@example.com")
    req = SupportTicketRequest(message="it broke", category="Bug report", route="/chat")

    with (
        patch(
            "core.services.support_ticket.container_repo.get_by_owner_id",
            new=AsyncMock(
                return_value={
                    "status": "running",
                    "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1",
                }
            ),
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
    # The runtime ARN, not the retired ECS service name -- it is the one identifier an operator
    # pastes straight into ``aws bedrock-agentcore get-agent-runtime`` from the ticket. A field
    # the row no longer carries would render as null on every ticket, silently.
    assert "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1" in body
    assert "service_name" not in body
    assert "running" in body  # container status
    assert "bedrock_claude" in body  # provider
    assert "380894" in body  # posthog person link


@pytest.mark.asyncio
async def test_submit_ticket_escapes_user_message(monkeypatch):
    auth = AuthContext(user_id="human_1", org_id="user_1", org_role="org:admin", email="x@y.com")
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


@pytest.mark.asyncio
async def test_submit_ticket_survives_enrichment_failure():
    """A repo/DynamoDB failure during enrichment must not break delivery —
    the ticket still sends, just without that metadata (no 500)."""
    auth = AuthContext(user_id="human_1", org_id="user_1", org_role="org:admin", email="x@y.com")
    req = SupportTicketRequest(message="help")

    with (
        patch(
            "core.services.support_ticket.container_repo.get_by_owner_id",
            new=AsyncMock(side_effect=RuntimeError("ddb down")),
        ),
        patch(
            "core.services.support_ticket.billing_repo.get_by_owner_id",
            new=AsyncMock(side_effect=RuntimeError("ddb down")),
        ),
        patch(
            "core.services.support_ticket.email_service.send_email",
            new=AsyncMock(return_value="email_1"),
        ) as mock_send,
    ):
        result = await support_ticket.submit_ticket(auth, req)

    assert result == "email_1"
    mock_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_undelivered_ticket_is_logged_with_its_content(caplog):
    """The email is the ONLY capture path — no ticket table exists — so a failed
    send must leave support something actionable, not just a user id."""
    auth = AuthContext(user_id="human_9", org_id="user_9", org_role="org:admin", email="ada@example.com")
    req = SupportTicketRequest(message="I'd like to use Instagram.", category="Integration request")

    with (
        patch("core.services.support_ticket.container_repo.get_by_owner_id", new=AsyncMock(return_value=None)),
        patch("core.services.support_ticket.billing_repo.get_by_owner_id", new=AsyncMock(return_value=None)),
        patch("core.services.support_ticket.email_service.send_email", new=AsyncMock(return_value=None)),
        caplog.at_level("WARNING", logger="core.services.support_ticket"),
    ):
        result = await support_ticket.submit_ticket(auth, req)

    assert result is None
    undelivered = [r.getMessage() for r in caplog.records if "UNDELIVERED" in r.getMessage()]
    assert len(undelivered) == 1
    line = undelivered[0]
    assert "I'd like to use Instagram." in line  # the ask itself
    assert "Integration request" in line  # what it is about
    assert "ada@example.com" in line  # how to answer without the email


@pytest.mark.asyncio
async def test_undelivered_log_bounds_the_message(caplog):
    """A pasted stack trace can't flood the log group: 8000-char schema max,
    2000-char log bound."""
    auth = AuthContext(user_id="human_9", org_id="user_9", org_role="org:admin", email="x@y.com")
    req = SupportTicketRequest(message="A" * 8000, category="Other")

    with (
        patch("core.services.support_ticket.container_repo.get_by_owner_id", new=AsyncMock(return_value=None)),
        patch("core.services.support_ticket.billing_repo.get_by_owner_id", new=AsyncMock(return_value=None)),
        patch("core.services.support_ticket.email_service.send_email", new=AsyncMock(return_value=None)),
        caplog.at_level("WARNING", logger="core.services.support_ticket"),
    ):
        await support_ticket.submit_ticket(auth, req)

    line = next(r.getMessage() for r in caplog.records if "UNDELIVERED" in r.getMessage())
    assert "A" * 2000 in line
    assert "A" * 2001 not in line


@pytest.mark.parametrize(
    "frontend_url, expected",
    [
        ("https://isol8.co", "https://admin.isol8.co/admin/users/user_123"),
        ("https://dev.isol8.co", "https://admin.dev.isol8.co/admin/users/user_123"),
    ],
)
def test_admin_user_url_tracks_the_frontend_host(monkeypatch, frontend_url, expected):
    """The deep link in every ticket is `admin.` in front of the frontend host —
    the same allowlist apps/frontend/src/middleware.ts gates /admin on."""
    monkeypatch.setattr(settings, "FRONTEND_URL", frontend_url)
    assert support_ticket._admin_user_url("user_123") == expected
