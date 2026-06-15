"""The email_service.send_email seam: gated, off-loop, best-effort."""

import time
from unittest.mock import patch

import pytest

from core.config import settings
from core.services import email_service


@pytest.mark.asyncio
async def test_disabled_is_noop(monkeypatch):
    """EMAIL_ENABLED False -> returns None and never calls Resend."""
    monkeypatch.setattr(settings, "EMAIL_ENABLED", False)
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")

    with patch("resend.Emails.send") as mock_send:
        result = await email_service.send_email(to="a@b.com", subject="hi", html="<p>hi</p>", from_addr="x@y.com")

    assert result is None
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_missing_key_is_noop(monkeypatch):
    """Enabled but no key -> returns None and never calls Resend."""
    monkeypatch.setattr(settings, "EMAIL_ENABLED", True)
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")

    with patch("resend.Emails.send") as mock_send:
        result = await email_service.send_email(to="a@b.com", subject="hi", html="<p>hi</p>", from_addr="x@y.com")

    assert result is None
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_sends_with_params_and_idempotency(monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_ENABLED", True)
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
async def test_failure_is_swallowed(monkeypatch):
    """Resend raising -> returns None, does not raise."""
    monkeypatch.setattr(settings, "EMAIL_ENABLED", True)
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")

    with patch("resend.Emails.send", side_effect=RuntimeError("boom")):
        result = await email_service.send_email(to="a@b.com", subject="hi", html="<p>hi</p>", from_addr="x@y.com")

    assert result is None


@pytest.mark.asyncio
async def test_stalled_send_respects_timeout(monkeypatch):
    """A send that blocks past the timeout returns None promptly instead of
    wedging the caller. abandon_on_cancel lets fail_after return at the
    deadline rather than waiting for the stalled worker thread."""
    monkeypatch.setattr(settings, "EMAIL_ENABLED", True)
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
