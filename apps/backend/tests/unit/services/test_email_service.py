"""The email_service.send_email seam: gated, off-loop, best-effort."""

import time
from unittest.mock import patch

import pytest

from core.config import settings
from core.services import email_service


@pytest.mark.asyncio
async def test_placeholder_key_is_noop(monkeypatch):
    """A non-re_ placeholder key (the CDK default) -> None, never calls Resend."""
    monkeypatch.setattr(settings, "RESEND_API_KEY", "PLACEHOLDERnotARealKey")

    with patch("resend.Emails.send") as mock_send:
        result = await email_service.send_email(to="a@b.com", subject="hi", html="<p>hi</p>", from_addr="x@y.com")

    assert result is None
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_missing_key_is_noop(monkeypatch):
    """No key -> returns None and never calls Resend."""
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")

    with patch("resend.Emails.send") as mock_send:
        result = await email_service.send_email(to="a@b.com", subject="hi", html="<p>hi</p>", from_addr="x@y.com")

    assert result is None
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_sends_with_params_and_idempotency(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")

    with patch("resend.Emails.send", return_value={"id": "email_abc"}) as mock_send:
        result = await email_service.send_email(
            to="a@b.com",
            subject="Welcome",
            html="<p>hi</p>",
            from_addr="Isol8 <hello@isol8.co>",
            reply_to="support@isol8.co",
            idempotency_key="welcome-user/u_1",
            kind="welcome",
        )

    assert result == "email_abc"
    mock_send.assert_called_once()
    params, options = mock_send.call_args.args
    assert params["from"] == "Isol8 <hello@isol8.co>"
    assert params["to"] == "a@b.com"
    assert params["subject"] == "Welcome"
    assert params["html"] == "<p>hi</p>"
    assert params["reply_to"] == "support@isol8.co"
    assert options == {"idempotency_key": "welcome-user/u_1"}


@pytest.mark.asyncio
async def test_sends_with_custom_headers(monkeypatch):
    """headers is passed through verbatim for reply threading (In-Reply-To/References)."""
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")

    with patch("resend.Emails.send", return_value={"id": "email_abc"}) as mock_send:
        await email_service.send_email(
            to="a@b.com",
            subject="Re: Welcome",
            html="<p>hi</p>",
            from_addr="Isol8 <hello@isol8.co>",
            headers={"In-Reply-To": "<m@x>", "References": "<m@x>"},
        )

    params, _ = mock_send.call_args.args
    assert params["headers"] == {"In-Reply-To": "<m@x>", "References": "<m@x>"}


@pytest.mark.asyncio
async def test_no_headers_key_when_omitted(monkeypatch):
    """No regression: omitting headers must not add a headers key at all."""
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")

    with patch("resend.Emails.send", return_value={"id": "email_abc"}) as mock_send:
        await email_service.send_email(to="a@b.com", subject="hi", html="<p>hi</p>", from_addr="x@y.com")

    params, _ = mock_send.call_args.args
    assert "headers" not in params


@pytest.mark.asyncio
async def test_non_prod_env_prefixes_subject(monkeypatch):
    """Dev/staging sends get a [ENV] subject tag so test mail is obvious."""
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")

    with patch("resend.Emails.send", return_value={"id": "x"}) as mock_send:
        await email_service.send_email(to="a@b.com", subject="Welcome", html="<p>hi</p>", from_addr="x@y.com")

    params, _ = mock_send.call_args.args
    assert params["subject"] == "[DEV] Welcome"


@pytest.mark.asyncio
async def test_prod_env_has_no_prefix(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")

    with patch("resend.Emails.send", return_value={"id": "x"}) as mock_send:
        await email_service.send_email(to="a@b.com", subject="Welcome", html="<p>hi</p>", from_addr="x@y.com")

    params, _ = mock_send.call_args.args
    assert params["subject"] == "Welcome"


@pytest.mark.asyncio
async def test_failure_is_swallowed(monkeypatch):
    """Resend raising -> returns None, does not raise."""
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")

    with patch("resend.Emails.send", side_effect=RuntimeError("boom")):
        result = await email_service.send_email(to="a@b.com", subject="hi", html="<p>hi</p>", from_addr="x@y.com")

    assert result is None


@pytest.mark.asyncio
async def test_stalled_send_respects_timeout(monkeypatch):
    """A send that blocks past the timeout returns None promptly instead of
    wedging the caller. abandon_on_cancel lets fail_after return at the
    deadline rather than waiting for the stalled worker thread."""
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(email_service, "_SEND_TIMEOUT_S", 0.1)

    def slow_send(*_args, **_kwargs):
        time.sleep(2.0)
        return {"id": "too_late"}

    start = time.monotonic()
    with patch("resend.Emails.send", side_effect=slow_send):
        result = await email_service.send_email(to="a@b.com", subject="hi", html="<p>hi</p>", from_addr="x@y.com")
    elapsed = time.monotonic() - start

    assert result is None
    # Without abandon_on_cancel this blocks ~2s for the thread; the fix returns
    # at the ~0.1s deadline. Generous bound to stay non-flaky under CI load.
    assert elapsed < 1.0
