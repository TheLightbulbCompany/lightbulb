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
    send_mock = AsyncMock(return_value="ses-123")
    monkeypatch.setattr(agent_mail_reply.ses_email, "send_ses_reply", send_mock)

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
    assert kwargs["body_text"] == "thanks"


@pytest.mark.asyncio
async def test_send_reply_first_contact_uses_subject_verbatim_no_re_prefix(monkeypatch):
    """A first contact (empty in_reply_to) is a brand-new email, not a reply --
    it must not get the "Re: " prefix reply_subject would otherwise add."""
    monkeypatch.setattr(agent_mail_reply.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(
        agent_mail_reply.agent_email_repo,
        "query_by_owner",
        AsyncMock(
            return_value=[
                {"address": "friday@agents.isol8.co", "agent_id": "a1", "status": "active"},
            ]
        ),
    )
    send_mock = AsyncMock(return_value="ses-123")
    monkeypatch.setattr(agent_mail_reply.ses_email, "send_ses_reply", send_mock)

    ctx = {
        "owner_id": "o",
        "agent_id": "a1",
        "reply_to": "owner@x.com",
        "in_reply_to": "",
        "subject": "Getting started with Acme",
    }
    result = await agent_mail_reply.send_reply(ctx=ctx, body_text="hi there")

    assert result == "sent"
    _, kwargs = send_mock.call_args
    assert kwargs["subject"] == "Getting started with Acme"
    assert kwargs["headers"] is None


@pytest.mark.asyncio
async def test_send_reply_with_in_reply_to_still_gets_re_prefix(monkeypatch):
    monkeypatch.setattr(agent_mail_reply.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(
        agent_mail_reply.agent_email_repo,
        "query_by_owner",
        AsyncMock(
            return_value=[
                {"address": "friday@agents.isol8.co", "agent_id": "a1", "status": "active"},
            ]
        ),
    )
    send_mock = AsyncMock(return_value="ses-123")
    monkeypatch.setattr(agent_mail_reply.ses_email, "send_ses_reply", send_mock)

    ctx = {
        "owner_id": "o",
        "agent_id": "a1",
        "reply_to": "owner@x.com",
        "in_reply_to": "<m@x>",
        "subject": "Getting started with Acme",
    }
    result = await agent_mail_reply.send_reply(ctx=ctx, body_text="hi there")

    assert result == "sent"
    _, kwargs = send_mock.call_args
    assert kwargs["subject"] == "Re: Getting started with Acme"


@pytest.mark.asyncio
async def test_send_reply_returns_not_sent_when_send_ses_reply_returns_none(monkeypatch):
    """send_ses_reply returns None (never raises) when SES send fails or agent
    email is disabled. That must not be reported as "sent" — the agent would
    tell the user it replied when nothing actually went out."""
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
    send_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(agent_mail_reply.ses_email, "send_ses_reply", send_mock)

    ctx = {
        "owner_id": "o",
        "agent_id": "a1",
        "reply_to": "alice@x.com",
        "in_reply_to": "<m@x>",
        "subject": "Need help",
    }
    result = await agent_mail_reply.send_reply(ctx=ctx, body_text="thanks")

    assert result == "not_sent"
    send_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_reply_no_address(monkeypatch):
    monkeypatch.setattr(agent_mail_reply.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(
        agent_mail_reply.agent_email_repo,
        "query_by_owner",
        AsyncMock(return_value=[]),
    )
    send_mock = AsyncMock()
    monkeypatch.setattr(agent_mail_reply.ses_email, "send_ses_reply", send_mock)

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
    monkeypatch.setattr(agent_mail_reply.ses_email, "send_ses_reply", send_mock)
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


def test_header_safe_strips_crlf_injection():
    result = agent_mail_reply._header_safe("<x>\r\nBcc: evil@x.com")
    assert "\r" not in result
    assert "\n" not in result


def test_header_safe_leaves_normal_message_id_untouched():
    assert agent_mail_reply._header_safe("<normal@id>") == "<normal@id>"


def test_header_safe_strips_surrounding_whitespace():
    assert agent_mail_reply._header_safe("  <id>  ") == "<id>"


@pytest.mark.asyncio
async def test_send_reply_strips_crlf_injection_from_threading_headers(monkeypatch):
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
    send_mock = AsyncMock(return_value="ses-123")
    monkeypatch.setattr(agent_mail_reply.ses_email, "send_ses_reply", send_mock)

    ctx = {
        "owner_id": "o",
        "agent_id": "a1",
        "reply_to": "alice@x.com",
        "in_reply_to": "<m@x>\r\nBcc: evil@x.com",
        "subject": "Need help",
    }
    result = await agent_mail_reply.send_reply(ctx=ctx, body_text="thanks")

    assert result == "sent"
    send_mock.assert_awaited_once()
    _, kwargs = send_mock.call_args
    headers = kwargs["headers"]
    assert "\r" not in headers["In-Reply-To"]
    assert "\n" not in headers["In-Reply-To"]
    assert "\r" not in headers["References"]
    assert "\n" not in headers["References"]


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
        agent_mail_reply.ses_email,
        "send_ses_reply",
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
