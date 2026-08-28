"""email_service.send_email: the app-mail concerns layered over the SES transport.

The transport itself (gating, off-loop, best-effort) is covered by
test_ses_email.py — these assert only what this seam adds: [ENV] subject
tagging and Reply-To routing, plus the end-to-end shape of the message a
signup actually receives.
"""

from email import message_from_bytes, policy
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config import settings
from core.services import email_service, ses_email


@pytest.fixture
def sent(monkeypatch):
    """Capture the kwargs email_service hands the SES transport."""
    send_mock = AsyncMock(return_value="ses-123")
    monkeypatch.setattr(ses_email, "send_ses_email", send_mock)
    return send_mock


@pytest.mark.asyncio
async def test_non_prod_env_prefixes_subject(monkeypatch, sent):
    """Dev/staging sends get an [ENV] subject tag so test mail is obvious."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")

    await email_service.send_email(to="a@b.com", subject="Welcome", html="<p>hi</p>", from_addr="x@y.com")

    assert sent.call_args.kwargs["subject"] == "[DEV] Welcome"


@pytest.mark.asyncio
async def test_prod_env_has_no_prefix(monkeypatch, sent):
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")

    await email_service.send_email(to="a@b.com", subject="Welcome", html="<p>hi</p>", from_addr="x@y.com")

    assert sent.call_args.kwargs["subject"] == "Welcome"


@pytest.mark.asyncio
async def test_reply_to_becomes_a_header(monkeypatch, sent):
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")

    await email_service.send_email(
        to="a@b.com",
        subject="Welcome",
        html="<p>hi</p>",
        from_addr="Isol8 <hello@isol8.co>",
        reply_to="support@isol8.co",
        kind="welcome",
    )

    kwargs = sent.call_args.kwargs
    assert kwargs["headers"] == {"Reply-To": "support@isol8.co"}
    assert kwargs["from_addr"] == "Isol8 <hello@isol8.co>"
    assert kwargs["body_html"] == "<p>hi</p>"
    assert kwargs["kind"] == "welcome"
    assert kwargs["body_text"] is None


@pytest.mark.asyncio
async def test_no_headers_when_reply_to_omitted(monkeypatch, sent):
    """Omitting reply_to must not attach an empty headers dict."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")

    await email_service.send_email(to="a@b.com", subject="hi", html="<p>hi</p>", from_addr="x@y.com")

    assert sent.call_args.kwargs["headers"] is None


@pytest.mark.asyncio
async def test_list_reply_to_is_joined(monkeypatch, sent):
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")

    await email_service.send_email(
        to="a@b.com",
        subject="hi",
        html="<p>hi</p>",
        from_addr="x@y.com",
        reply_to=["a@isol8.co", "b@isol8.co"],
    )

    assert sent.call_args.kwargs["headers"] == {"Reply-To": "a@isol8.co, b@isol8.co"}


@pytest.mark.asyncio
async def test_welcome_email_renders_as_html_not_plaintext(monkeypatch):
    """The signup email must arrive as text/html — an HTML body accidentally
    sent as text/plain would show the user raw markup."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    fake_client = MagicMock()
    fake_client.send_email.return_value = {"MessageId": "ses-1"}
    monkeypatch.setattr(ses_email, "_client", lambda: fake_client)

    result = await email_service.send_email(
        to="a@b.com",
        subject="Welcome to Lightbulb",
        html="<p>hi</p>",
        from_addr="Isol8 <hello@isol8.co>",
        reply_to="support@isol8.co",
        kind="welcome",
    )

    assert result == "ses-1"
    _, kwargs = fake_client.send_email.call_args
    parsed = message_from_bytes(kwargs["Content"]["Raw"]["Data"], policy=policy.default)
    assert parsed.get_content_type() == "text/html"
    assert not parsed.is_multipart()
    assert parsed["Reply-To"] == "support@isol8.co"
    assert parsed["Subject"] == "Welcome to Lightbulb"
    assert parsed.get_content().strip() == "<p>hi</p>"


@pytest.mark.asyncio
async def test_gate_is_inherited_from_transport(monkeypatch):
    """No AGENT_EMAIL_DOMAIN -> no send, no raise. App mail inherits the same
    prod-only gate as agent replies."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "")
    fake_client = MagicMock()
    monkeypatch.setattr(ses_email, "_client", lambda: fake_client)

    result = await email_service.send_email(to="a@b.com", subject="hi", html="<p>hi</p>", from_addr="x@y.com")

    assert result is None
    fake_client.send_email.assert_not_called()
