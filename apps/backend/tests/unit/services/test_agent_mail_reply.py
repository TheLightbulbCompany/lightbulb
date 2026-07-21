"""agent_mail_reply: derive from-address, thread headers, and send a reply."""

import os
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_mail_reply  # noqa: E402


def test_reply_subject_plain():
    assert agent_mail_reply.reply_subject("hello") == "Re: hello"


def test_reply_subject_already_prefixed_titlecase():
    assert agent_mail_reply.reply_subject("Re: hello") == "Re: hello"


def test_reply_subject_already_prefixed_lowercase():
    assert agent_mail_reply.reply_subject("re: hi") == "re: hi"


@pytest.mark.asyncio
async def test_resolve_from_address_picks_active_over_alias(monkeypatch):
    monkeypatch.setattr(
        agent_mail_reply.agent_email_repo,
        "query_by_owner",
        AsyncMock(
            return_value=[
                {"address": "acme@d", "agent_id": "a1", "status": "active"},
                {"address": "old@d", "agent_id": "a1", "status": "alias"},
            ]
        ),
    )
    assert await agent_mail_reply.resolve_from_address("o", "a1") == "acme@d"


@pytest.mark.asyncio
async def test_resolve_from_address_no_match_returns_none(monkeypatch):
    monkeypatch.setattr(
        agent_mail_reply.agent_email_repo,
        "query_by_owner",
        AsyncMock(
            return_value=[
                {"address": "acme@d", "agent_id": "a1", "status": "active"},
            ]
        ),
    )
    assert await agent_mail_reply.resolve_from_address("o", "no_such_agent") is None


@pytest.mark.asyncio
async def test_send_reply_happy_path(monkeypatch):
    monkeypatch.setattr(agent_mail_reply.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(
        agent_mail_reply.agent_email_repo,
        "query_by_owner",
        AsyncMock(
            return_value=[
                {"address": "acme@agents.isol8.co", "agent_id": "a1", "status": "active"},
            ]
        ),
    )
    send_mock = AsyncMock(return_value="email_id")
    monkeypatch.setattr(agent_mail_reply.email_service, "send_email", send_mock)

    ctx = {
        "owner_id": "o",
        "agent_id": "a1",
        "reply_to": "alice@x.com",
        "in_reply_to": "<m@x>",
        "subject": "Need help",
    }
    result = await agent_mail_reply.send_reply(ctx=ctx, body_text="thanks")

    assert result == "sent"
    send_mock.assert_awaited_once()
    _, kwargs = send_mock.call_args
    assert kwargs["to"] == "alice@x.com"
    assert kwargs["from_addr"] == "acme@agents.isol8.co"
    assert kwargs["subject"] == "Re: Need help"
    assert kwargs["headers"] == {"In-Reply-To": "<m@x>", "References": "<m@x>"}
    assert kwargs["text"] == "thanks"
    assert kwargs["kind"] == "agent_reply"


@pytest.mark.asyncio
async def test_send_reply_no_address(monkeypatch):
    monkeypatch.setattr(agent_mail_reply.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(
        agent_mail_reply.agent_email_repo,
        "query_by_owner",
        AsyncMock(return_value=[]),
    )
    send_mock = AsyncMock()
    monkeypatch.setattr(agent_mail_reply.email_service, "send_email", send_mock)

    ctx = {
        "owner_id": "o",
        "agent_id": "a1",
        "reply_to": "alice@x.com",
        "in_reply_to": "<m@x>",
        "subject": "Need help",
    }
    result = await agent_mail_reply.send_reply(ctx=ctx, body_text="thanks")

    assert result == "no_address"
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_reply_disabled_when_domain_empty(monkeypatch):
    monkeypatch.setattr(agent_mail_reply.settings, "AGENT_EMAIL_DOMAIN", "")
    send_mock = AsyncMock()
    monkeypatch.setattr(agent_mail_reply.email_service, "send_email", send_mock)
    query_mock = AsyncMock()
    monkeypatch.setattr(agent_mail_reply.agent_email_repo, "query_by_owner", query_mock)

    ctx = {
        "owner_id": "o",
        "agent_id": "a1",
        "reply_to": "alice@x.com",
        "in_reply_to": "<m@x>",
        "subject": "Need help",
    }
    result = await agent_mail_reply.send_reply(ctx=ctx, body_text="thanks")

    assert result == "disabled"
    send_mock.assert_not_awaited()
    query_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_reply_never_raises_on_send_error(monkeypatch):
    monkeypatch.setattr(agent_mail_reply.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(
        agent_mail_reply.agent_email_repo,
        "query_by_owner",
        AsyncMock(
            return_value=[
                {"address": "acme@agents.isol8.co", "agent_id": "a1", "status": "active"},
            ]
        ),
    )
    monkeypatch.setattr(
        agent_mail_reply.email_service,
        "send_email",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    ctx = {
        "owner_id": "o",
        "agent_id": "a1",
        "reply_to": "alice@x.com",
        "in_reply_to": "<m@x>",
        "subject": "Need help",
    }
    result = await agent_mail_reply.send_reply(ctx=ctx, body_text="thanks")

    assert result == "error"
