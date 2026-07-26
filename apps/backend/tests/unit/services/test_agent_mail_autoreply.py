import pytest

from core.services import agent_mail_autoreply as mod
from core.services.trial_cap import DAILY_CAP, TRIAL_EXPIRED


@pytest.fixture
def sent(monkeypatch):
    box = []

    async def _send(*, from_addr, to, subject, body_text, body_html, headers):
        box.append({"from": from_addr, "to": to, "subject": subject, "body": body_text, "headers": headers})
        return "msg-1"

    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    monkeypatch.setattr(mod.ses_email, "send_ses_reply", _send)
    return box


async def _call(reason):
    return await mod.send_cap_autoreply(
        reason=reason,
        from_addr="friday@acme.isol8.co",
        to="owner@example.com",
        in_reply_to="<m1@mail>",
        subject="Re: hello",
    )


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
    assert sent[0]["from"] == "friday@acme.isol8.co"
    assert sent[0]["to"] == "owner@example.com"
    assert sent[0]["headers"]["In-Reply-To"] == "<m1@mail>"


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
