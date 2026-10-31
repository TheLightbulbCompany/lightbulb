"""ses_email: gated, off-loop, best-effort SES send — the one outbound transport."""

import time
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

    fake_client.send_email.assert_called_once()
    _, kwargs = fake_client.send_email.call_args
    raw_bytes = kwargs["Content"]["Raw"]["Data"]
    assert isinstance(raw_bytes, (bytes, bytearray))

    parsed = message_from_bytes(raw_bytes)
    # The RFC Message-ID we minted, not the SES id: a reply's In-Reply-To names it.
    assert result == parsed["Message-ID"]
    assert result.startswith("<") and result.endswith("@isol8.co>")
    assert kwargs["FromEmailAddress"] == "friday@acme.isol8.co"
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


@pytest.mark.asyncio
async def test_html_only_body_is_a_single_text_html_part(monkeypatch):
    """The app-mail path passes body_html with no body_text. EmailMessage has
    no 'unset body' state, so this must go through set_content(subtype="html")
    — add_alternative on an empty message would leave a stray text/plain part
    and the recipient would see raw markup."""
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    fake_client = MagicMock()
    fake_client.send_email.return_value = {"MessageId": "ses-html"}
    monkeypatch.setattr(ses_email, "_client", lambda: fake_client)

    result = await ses_email.send_ses_email(
        from_addr="Isol8 <hello@isol8.co>",
        to="alice@x.com",
        subject="Welcome",
        body_html="<p>hi</p>",
        kind="welcome",
    )

    _, kwargs = fake_client.send_email.call_args
    parsed = message_from_bytes(kwargs["Content"]["Raw"]["Data"], policy=policy.default)
    assert result == parsed["Message-ID"]
    assert not parsed.is_multipart()
    assert parsed.get_content_type() == "text/html"
    assert parsed.get_content().strip() == "<p>hi</p>"


@pytest.mark.asyncio
async def test_list_recipients_are_joined(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    fake_client = MagicMock()
    fake_client.send_email.return_value = {"MessageId": "ses-multi"}
    monkeypatch.setattr(ses_email, "_client", lambda: fake_client)

    await ses_email.send_ses_email(
        from_addr="Isol8 <hello@isol8.co>",
        to=["a@x.com", "b@x.com"],
        subject="hi",
        body_html="<p>hi</p>",
    )

    _, kwargs = fake_client.send_email.call_args
    parsed = message_from_bytes(kwargs["Content"]["Raw"]["Data"])
    assert parsed["To"] == "a@x.com, b@x.com"


@pytest.mark.asyncio
async def test_stalled_send_respects_timeout(monkeypatch):
    """A send that blocks past the timeout returns None promptly instead of
    wedging the caller (an agent run, or the Clerk webhook). abandon_on_cancel
    lets fail_after return at the deadline rather than waiting for the thread."""
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    monkeypatch.setattr(ses_email, "_SEND_TIMEOUT_S", 0.1)

    def slow_send(**_kwargs):
        time.sleep(2.0)
        return {"MessageId": "too_late"}

    fake_client = MagicMock()
    fake_client.send_email.side_effect = slow_send
    monkeypatch.setattr(ses_email, "_client", lambda: fake_client)

    start = time.monotonic()
    result = await ses_email.send_ses_email(
        from_addr="Isol8 <hello@isol8.co>",
        to="a@x.com",
        subject="hi",
        body_html="<p>hi</p>",
    )
    elapsed = time.monotonic() - start

    assert result is None
    # Without abandon_on_cancel this blocks ~2s for the thread; the fix returns
    # at the ~0.1s deadline. Generous bound to stay non-flaky under CI load.
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_send_stamps_configuration_set_and_tenant(monkeypatch):
    from core.services import ses_email

    monkeypatch.setattr(ses_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    monkeypatch.setattr(ses_email.settings, "AGENT_MAIL_CONFIGURATION_SET", "isol8-dev-agent-mail")
    fake = MagicMock()
    fake.send_email.return_value = {"MessageId": "m"}
    monkeypatch.setattr(ses_email, "_client", lambda: fake)
    await ses_email.send_ses_reply(
        from_addr="f@a.isol8.co", to="t@x.y", subject="s", body_text="b", tenant_name="owner-user_1"
    )
    kwargs = fake.send_email.call_args.kwargs
    assert kwargs["ConfigurationSetName"] == "isol8-dev-agent-mail"
    assert kwargs["TenantName"] == "owner-user_1"


@pytest.mark.asyncio
async def test_send_omits_stamps_when_unset(monkeypatch):
    from core.services import ses_email

    monkeypatch.setattr(ses_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    monkeypatch.setattr(ses_email.settings, "AGENT_MAIL_CONFIGURATION_SET", "")
    fake = MagicMock()
    fake.send_email.return_value = {"MessageId": "m"}
    monkeypatch.setattr(ses_email, "_client", lambda: fake)
    await ses_email.send_ses_reply(from_addr="f@a.isol8.co", to="t@x.y", subject="s", body_text="b")
    kwargs = fake.send_email.call_args.kwargs
    assert "ConfigurationSetName" not in kwargs and "TenantName" not in kwargs


@pytest.mark.asyncio
async def test_a_display_name_from_sends_the_bare_envelope_address(monkeypatch):
    """SES reports FromEmailAddress as a complaint's mail.source, which the complaint handler looks up."""
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    fake_client = MagicMock()
    fake_client.send_email.return_value = {"MessageId": "ses-1"}
    monkeypatch.setattr(ses_email, "_client", lambda: fake_client)

    await ses_email.send_ses_reply(
        from_addr='"Friday · Acme agents" <agents@acme.isol8.co>', to="t@x.y", subject="s", body_text="b"
    )

    assert fake_client.send_email.call_args.kwargs["FromEmailAddress"] == "agents@acme.isol8.co"


@pytest.mark.asyncio
async def test_calendar_reply_is_a_text_calendar_alternative(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    fake_client = MagicMock()
    fake_client.send_email.return_value = {"MessageId": "ses-789"}
    monkeypatch.setattr(ses_email, "_client", lambda: fake_client)

    ics = "BEGIN:VCALENDAR\r\nMETHOD:REPLY\r\nEND:VCALENDAR\r\n"
    await ses_email.send_ses_email(
        from_addr="agents@acme.isol8.co", to="o@x.com", subject="Accepted: x", body_text="ok", calendar=ics
    )

    parsed = message_from_bytes(
        fake_client.send_email.call_args.kwargs["Content"]["Raw"]["Data"], policy=policy.default
    )
    assert parsed.get_content_type() == "multipart/alternative"
    part = next(p for p in parsed.walk() if p.get_content_type() == "text/calendar")
    assert part.get_param("method") == "REPLY" and part.get_content_charset() == "utf-8"
    assert "METHOD:REPLY" in part.get_content()


@pytest.mark.asyncio
async def test_an_app_invite_never_logs_the_full_address(monkeypatch, caplog):
    """R49: an invitee is an outsider; the three log lines mask an apps_invite recipient."""
    caplog.set_level("INFO", logger=ses_email.logger.name)
    fake_client = MagicMock()
    fake_client.send_email.return_value = {"MessageId": "ses-1"}
    monkeypatch.setattr(ses_email, "_client", lambda: fake_client)
    for domain, side_effect in (("", None), ("isol8.co", None), ("isol8.co", RuntimeError("boom"))):
        monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", domain)
        fake_client.send_email.side_effect = side_effect
        await ses_email.send_ses_email(
            from_addr="hello@isol8.co", to="bo.secret@x.co", subject="s", body_text="t", kind="apps_invite"
        )
    assert len(caplog.records) == 3
    assert "bo.secret" not in caplog.text and "b***@x.co" in caplog.text

    caplog.clear()  # other kinds are unchanged
    await ses_email.send_ses_email(from_addr="hello@isol8.co", to="al@x.co", subject="s", body_text="t")
    assert "al@x.co" in caplog.text
