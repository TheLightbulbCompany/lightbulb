"""agent_mail_reply: derive from-address, thread headers, and send a reply."""

import os
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_mail_reply  # noqa: E402


FOOTER_MARK = "I was asleep when this arrived"


class _ReplyHarness:
    def __init__(self):
        self.subscription_status = "trial_local"
        self.cold_wake = False
        self.sent_body: str | None = None


@pytest.fixture
def reply_harness(monkeypatch):
    """Wires send_reply's full happy path plus the two cold-wake-footer
    dependencies (container_repo.consume_email_cold_wake, billing_repo.
    get_by_owner_id) to a mutable harness so a test can flip
    ``subscription_status`` / ``cold_wake`` after the fixture runs and
    ``send()`` will read the current values at call time."""
    harness = _ReplyHarness()
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

    async def _consume_email_cold_wake(owner_id, **kwargs):
        return harness.cold_wake

    monkeypatch.setattr(agent_mail_reply.container_repo, "consume_email_cold_wake", _consume_email_cold_wake)

    async def _get_by_owner_id(owner_id, **kwargs):
        return {"subscription_status": harness.subscription_status}

    monkeypatch.setattr(agent_mail_reply.billing_repo, "get_by_owner_id", _get_by_owner_id)

    async def _send_ses_reply(**kwargs):
        harness.sent_body = kwargs["body_text"]
        return "ses-123"

    monkeypatch.setattr(agent_mail_reply.ses_email, "send_ses_reply", _send_ses_reply)

    return harness


async def send():
    ctx = {
        "owner_id": "user_a",
        "agent_id": "a1",
        "reply_to": "alice@x.com",
        "in_reply_to": "<m@x>",
        "subject": "Need help",
    }
    return await agent_mail_reply.send_reply(ctx=ctx, body_text="agent words")


@pytest.mark.asyncio
async def test_footer_when_free_tier_and_recent_cold_wake(reply_harness):
    reply_harness.subscription_status = "trial_local"
    reply_harness.cold_wake = True
    await send()
    assert FOOTER_MARK in reply_harness.sent_body
    # The agent's own words must be untouched, above the separator.
    assert reply_harness.sent_body.index("agent words") < reply_harness.sent_body.index(FOOTER_MARK)


@pytest.mark.asyncio
async def test_no_footer_without_a_cold_wake(reply_harness):
    reply_harness.subscription_status = "trial_local"
    reply_harness.cold_wake = False
    await send()
    assert FOOTER_MARK not in reply_harness.sent_body


@pytest.mark.asyncio
async def test_no_footer_for_a_paid_owner(reply_harness):
    """A paid container never sleeps, so the note would be a lie."""
    reply_harness.subscription_status = "active"
    reply_harness.cold_wake = True
    await send()
    assert FOOTER_MARK not in reply_harness.sent_body


@pytest.mark.asyncio
async def test_footer_appears_exactly_once(reply_harness):
    reply_harness.subscription_status = "trial_local"
    reply_harness.cold_wake = True
    await send()
    assert reply_harness.sent_body.count(FOOTER_MARK) == 1


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
