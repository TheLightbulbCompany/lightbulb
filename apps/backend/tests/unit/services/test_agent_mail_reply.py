"""agent_mail_reply: the team address under the agent's name, thread headers, and send a reply."""

import os
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_mail_reply  # noqa: E402


@pytest.fixture(autouse=True)
def _team_mailbox(monkeypatch):
    """From is the bare team address unless a test says otherwise; sent mail is recorded."""
    monkeypatch.setattr(agent_mail_reply, "from_header", AsyncMock(side_effect=lambda o, a, address: address))
    monkeypatch.setattr(agent_mail_reply.agent_mail_inbox_repo, "put_sent", AsyncMock())


class _ReplyHarness:
    def __init__(self):
        self.subscription_status = "trial_local"
        self.sent_body: str | None = None


@pytest.fixture
def reply_harness(monkeypatch):
    """Wires send_reply's full happy path to a mutable harness so a test can flip
    ``subscription_status`` after the fixture runs and ``send()`` will read the
    current value at call time."""
    harness = _ReplyHarness()
    monkeypatch.setattr(agent_mail_reply.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(agent_mail_reply, "team_address", AsyncMock(return_value="friday@agents.isol8.co"))

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


def test_reply_subject_plain():
    assert agent_mail_reply.reply_subject("hello") == "Re: hello"


def test_reply_subject_already_prefixed_titlecase():
    assert agent_mail_reply.reply_subject("Re: hello") == "Re: hello"


def test_reply_subject_already_prefixed_lowercase():
    assert agent_mail_reply.reply_subject("re: hi") == "re: hi"


@pytest.mark.asyncio
async def test_send_reply_happy_path(monkeypatch):
    monkeypatch.setattr(agent_mail_reply.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(agent_mail_reply, "team_address", AsyncMock(return_value="acme@agents.isol8.co"))
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
    assert kwargs["headers"] == {
        "Auto-Submitted": "auto-generated",
        "In-Reply-To": "<m@x>",
        "References": "<m@x>",
    }
    assert kwargs["body_text"] == "thanks"


# --- References chain (RFC 5322 3.6.4) -------------------------------------
#
# The reply's References must be the PARENT'S References plus the parent's
# Message-ID. Emitting only the parent truncates the chain on every agent
# reply and lets the thread ROOT drift: Friday sends A, owner replies
# (refs "A"), Friday replies (refs "B" -- A gone), owner replies (refs "B C").
# Any thread identity read off References[0] then splits one conversation
# into several, and strict mail clients fragment the thread.


def test_references_chain_appends_parent_to_the_inherited_chain():
    assert agent_mail_reply.build_references("<a@x> <b@x>", "<c@x>") == "<a@x> <b@x> <c@x>"


def test_references_chain_is_just_the_parent_when_there_is_no_history():
    assert agent_mail_reply.build_references("", "<a@x>") == "<a@x>"


def test_references_chain_dedupes_a_parent_already_present():
    """Some clients append the parent's Message-ID to References themselves;
    repeating it would grow the header without adding ancestry."""
    assert agent_mail_reply.build_references("<a@x> <b@x>", "<b@x>") == "<a@x> <b@x>"


def test_references_truncation_keeps_the_ROOT_and_the_recent_tail():
    """RFC 5322 permits dropping ids from an over-long chain, and the root is
    the one id that must survive -- it is the thread's identity. Dropping the
    head instead would move the root and split the conversation."""
    ids = [f"<m{i}@x>" for i in range(40)]
    out = agent_mail_reply.build_references(" ".join(ids[:-1]), ids[-1]).split()

    assert len(out) == agent_mail_reply._MAX_REFERENCES
    assert out[0] == "<m0@x>", "root must be preserved"
    assert out[-1] == "<m39@x>", "most recent ancestor must be preserved"


@pytest.mark.asyncio
async def test_send_reply_emits_the_full_inherited_chain(monkeypatch):
    monkeypatch.setattr(agent_mail_reply.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(agent_mail_reply, "team_address", AsyncMock(return_value="acme@agents.isol8.co"))
    send_mock = AsyncMock(return_value="ses-123")
    monkeypatch.setattr(agent_mail_reply.ses_email, "send_ses_reply", send_mock)

    ctx = {
        "owner_id": "o",
        "agent_id": "a1",
        "reply_to": "alice@x.com",
        "in_reply_to": "<b@x>",
        "references": "<a@x>",
        "subject": "Need help",
    }
    assert await agent_mail_reply.send_reply(ctx=ctx, body_text="thanks") == "sent"

    _, kwargs = send_mock.call_args
    assert kwargs["headers"]["In-Reply-To"] == "<b@x>"
    assert kwargs["headers"]["References"] == "<a@x> <b@x>", "root <a@x> must survive"


@pytest.mark.asyncio
async def test_send_reply_first_contact_uses_subject_verbatim_no_re_prefix(monkeypatch):
    """A first contact (empty in_reply_to) is a brand-new email, not a reply --
    it must not get the "Re: " prefix reply_subject would otherwise add."""
    monkeypatch.setattr(agent_mail_reply.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(agent_mail_reply, "team_address", AsyncMock(return_value="friday@agents.isol8.co"))
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
    # Even a first contact (nothing to thread on) is marked machine-generated.
    assert kwargs["headers"] == {"Auto-Submitted": "auto-generated"}


@pytest.mark.asyncio
async def test_send_reply_with_in_reply_to_still_gets_re_prefix(monkeypatch):
    monkeypatch.setattr(agent_mail_reply.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(agent_mail_reply, "team_address", AsyncMock(return_value="friday@agents.isol8.co"))
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
    monkeypatch.setattr(agent_mail_reply, "team_address", AsyncMock(return_value="acme@agents.isol8.co"))
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
    monkeypatch.setattr(agent_mail_reply, "team_address", AsyncMock(return_value=None))
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
    monkeypatch.setattr(agent_mail_reply, "team_address", query_mock)

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
    monkeypatch.setattr(agent_mail_reply, "team_address", AsyncMock(return_value="acme@agents.isol8.co"))
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
    monkeypatch.setattr(agent_mail_reply, "team_address", AsyncMock(return_value="acme@agents.isol8.co"))
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


@pytest.mark.asyncio
async def test_send_reply_is_the_agent_on_the_team_address_and_records_the_send(monkeypatch):
    monkeypatch.setattr(agent_mail_reply.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    monkeypatch.setattr(agent_mail_reply, "team_address", AsyncMock(return_value="agents@acme.isol8.co"))
    monkeypatch.setattr(
        agent_mail_reply, "from_header", AsyncMock(return_value='"Friday · Acme agents" <agents@acme.isol8.co>')
    )
    send_mock = AsyncMock(return_value="<r1@isol8.co>")
    monkeypatch.setattr(agent_mail_reply.ses_email, "send_ses_reply", send_mock)
    ctx = {"owner_id": "o", "agent_id": "friday", "reply_to": "alice@x.com", "in_reply_to": "<m@x>", "subject": "Hi"}

    assert await agent_mail_reply.send_reply(ctx=ctx, body_text="thanks") == "sent"

    assert send_mock.await_args.kwargs["from_addr"] == '"Friday · Acme agents" <agents@acme.isol8.co>'
    agent_mail_reply.from_header.assert_awaited_once_with("o", "friday", "agents@acme.isol8.co")
    kw = agent_mail_reply.agent_mail_inbox_repo.put_sent.await_args.kwargs
    assert (kw["address"], kw["message_id"], kw["agent_id"]) == ("agents@acme.isol8.co", "<r1@isol8.co>", "friday")
    assert kw["text"] == "thanks"
