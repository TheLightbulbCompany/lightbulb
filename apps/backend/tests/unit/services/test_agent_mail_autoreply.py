from unittest.mock import AsyncMock

import pytest

from core.services import agent_mail_autoreply as mod
from core.services.trial_cap import DAILY_CAP, INCLUDED_CAP, TRIAL_EXPIRED


@pytest.fixture
def sent(monkeypatch):
    box = []

    async def _send(*, from_addr, to, subject, body_text, body_html, headers):
        box.append({"from": from_addr, "to": to, "subject": subject, "body": body_text, "headers": headers})
        return "msg-1"

    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    monkeypatch.setattr(mod.ses_email, "send_ses_reply", _send)
    monkeypatch.setattr(mod.container_repo, "try_consume_daily_email_send", AsyncMock(return_value=True))
    monkeypatch.setattr(
        mod, "from_header", AsyncMock(side_effect=lambda o, a, address: f'"{a} · Acme agents" <{address}>')
    )
    monkeypatch.setattr(mod.agent_mail_inbox_repo, "put_sent", AsyncMock())
    return box


async def _call(reason, **overrides):
    kwargs = {
        "owner_id": "owner_1",
        "agent_id": "Friday",
        "reason": reason,
        "from_addr": "agents@acme.isol8.co",
        "to": "owner@example.com",
        "in_reply_to": "<m1@mail>",
        "subject": "Re: hello",
    }
    kwargs.update(overrides)
    return await mod.send_cap_autoreply(**kwargs)


@pytest.mark.asyncio
async def test_daily_cap_says_it_resets_and_does_NOT_demand_payment(sent):
    assert await _call(DAILY_CAP) is True
    body = sent[0]["body"].lower()
    assert "resets" in body
    # Waiting must be offered FIRST and upgrading must not be the only way out.
    assert "send this again" in body
    assert "must upgrade" not in body
    assert "your free plan has ended" not in body


@pytest.mark.asyncio
async def test_trial_expired_does_ask_for_payment(sent):
    assert await _call(TRIAL_EXPIRED) is True
    body = sent[0]["body"].lower()
    assert "ended" in body
    assert "upgrade" in body
    assert "resets" not in body


@pytest.mark.asyncio
async def test_threads_with_the_conversation(sent):
    await _call(DAILY_CAP)
    assert sent[0]["from"] == '"Friday · Acme agents" <agents@acme.isol8.co>'
    kw = mod.agent_mail_inbox_repo.put_sent.await_args.kwargs
    assert (kw["address"], kw["message_id"], kw["agent_id"]) == ("agents@acme.isol8.co", "msg-1", "Friday")
    assert kw["text"] == sent[0]["body"]
    assert sent[0]["to"] == "owner@example.com"
    assert sent[0]["headers"]["In-Reply-To"] == "<m1@mail>"


@pytest.mark.asyncio
async def test_marks_itself_as_machine_mail(sent):
    """RFC 3834. Without it a correspondent's own autoresponder can bounce
    back and forth with us forever -- each bounce carries a fresh Message-ID,
    so dedup never sees the loop and the owner is still over cap."""
    await _call(DAILY_CAP)
    assert sent[0]["headers"]["Auto-Submitted"] == "auto-replied"


@pytest.mark.asyncio
async def test_stops_once_the_daily_send_cap_is_exhausted(monkeypatch, sent):
    """The header above is advisory; this counter is the fence that holds when
    the other side ignores it."""
    monkeypatch.setattr(mod.container_repo, "try_consume_daily_email_send", AsyncMock(return_value=False))
    assert await _call(DAILY_CAP) is False
    assert sent == []


@pytest.mark.asyncio
async def test_sanitizes_sender_controlled_headers(sent):
    """`in_reply_to` and `subject` come straight off the sender's mail."""
    await _call(DAILY_CAP, in_reply_to="<m1@mail>\r\nBcc: evil@x.com", subject="Re: hi\r\nBcc: evil@x.com")
    assert sent[0]["headers"]["In-Reply-To"] == "<m1@mail>Bcc: evil@x.com"
    assert "\r" not in sent[0]["subject"] and "\n" not in sent[0]["subject"]


@pytest.mark.asyncio
async def test_unknown_reason_sends_nothing(sent):
    assert await _call("something_else") is False
    assert sent == []


@pytest.mark.asyncio
async def test_disabled_when_domain_unset(monkeypatch, sent):
    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "")
    assert await _call(DAILY_CAP) is False
    assert sent == []


@pytest.mark.asyncio
async def test_ses_failure_returns_false_and_does_not_raise(monkeypatch, sent):
    async def _boom(**kwargs):
        raise RuntimeError("ses down")

    monkeypatch.setattr(mod.ses_email, "send_ses_reply", _boom)
    assert await _call(DAILY_CAP) is False


@pytest.mark.asyncio
async def test_included_cap_reply_is_for_a_paying_owner(sent):
    """A paying Base/paid-default owner at the cap: wait for the reset, never free-trial copy."""
    assert await _call(INCLUDED_CAP) is True
    body = sent[0]["body"].lower()
    assert "resets" in body and "included usage" in body
    assert "free" not in body and "trial" not in body
