"""ses_email.send_ses_reply: gated, off-loop, best-effort SES send for agent replies."""

from email import message_from_bytes, policy
from unittest.mock import MagicMock

import pytest

from core.config import settings
from core.services import ses_email


@pytest.mark.asyncio
async def test_disabled_when_agent_email_domain_empty(monkeypatch):
    """No AGENT_EMAIL_DOMAIN configured -> None, client never touched."""
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "")
    fake_client = MagicMock()
    monkeypatch.setattr(ses_email, "_client", lambda: fake_client)

    result = await ses_email.send_ses_reply(
        from_addr="friday@acme.isol8.co",
        to="alice@x.com",
        subject="Re: hi",
        body_text="thanks",
    )

    assert result is None
    fake_client.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_happy_path_returns_message_id_and_builds_raw_mime(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    fake_client = MagicMock()
    fake_client.send_email.return_value = {"MessageId": "ses-123"}
    monkeypatch.setattr(ses_email, "_client", lambda: fake_client)

    result = await ses_email.send_ses_reply(
        from_addr="friday@acme.isol8.co",
        to="alice@x.com",
        subject="Re: Need help",
        body_text="thanks",
        body_html="<p>thanks</p>",
        headers={"In-Reply-To": "<m@x>", "References": "<m@x>"},
    )

    assert result == "ses-123"
    fake_client.send_email.assert_called_once()
    _, kwargs = fake_client.send_email.call_args
    raw_bytes = kwargs["Content"]["Raw"]["Data"]
    assert isinstance(raw_bytes, (bytes, bytearray))

    parsed = message_from_bytes(raw_bytes)
    assert parsed["From"] == "friday@acme.isol8.co"
    assert parsed["To"] == "alice@x.com"
    assert parsed["Subject"] == "Re: Need help"
    assert parsed["In-Reply-To"] == "<m@x>"
    assert parsed["References"] == "<m@x>"
    assert parsed.is_multipart()


@pytest.mark.asyncio
async def test_no_html_alternative_when_body_html_omitted(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    fake_client = MagicMock()
    fake_client.send_email.return_value = {"MessageId": "ses-456"}
    monkeypatch.setattr(ses_email, "_client", lambda: fake_client)

    await ses_email.send_ses_reply(
        from_addr="friday@acme.isol8.co",
        to="alice@x.com",
        subject="Re: hi",
        body_text="thanks",
    )

    _, kwargs = fake_client.send_email.call_args
    parsed = message_from_bytes(kwargs["Content"]["Raw"]["Data"], policy=policy.default)
    assert not parsed.is_multipart()
    assert parsed.get_content().strip() == "thanks"


@pytest.mark.asyncio
async def test_never_raises_when_client_send_errors(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    fake_client = MagicMock()
    fake_client.send_email.side_effect = RuntimeError("boom")
    monkeypatch.setattr(ses_email, "_client", lambda: fake_client)

    result = await ses_email.send_ses_reply(
        from_addr="friday@acme.isol8.co",
        to="alice@x.com",
        subject="Re: hi",
        body_text="thanks",
    )

    assert result is None


@pytest.mark.asyncio
async def test_never_raises_when_client_lookup_itself_errors(monkeypatch):
    """Even if boto3 client construction blows up, send_ses_reply swallows it."""
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "isol8.co")

    def _boom():
        raise RuntimeError("no creds")

    monkeypatch.setattr(ses_email, "_client", _boom)

    result = await ses_email.send_ses_reply(
        from_addr="friday@acme.isol8.co",
        to="alice@x.com",
        subject="Re: hi",
        body_text="thanks",
    )

    assert result is None
