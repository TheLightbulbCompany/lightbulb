"""The "You're #N on the list" waitlist email sent by user.created while the
launch competition is in its waitlist phase."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.services import launch_waitlist_email

_DROP_AT = datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)  # midnight ET (UTC-4 in September)

_KWARGS = dict(
    to_email="ada@example.com",
    first_name="Ada",
    effective_position=42,
    referral_link="https://flickmeon.ai/?ref=ABCD1234",
    drop_at=_DROP_AT,
    referral_jump=10,
    comp_slots=220,
)


@pytest.mark.asyncio
async def test_sends_via_email_service_with_kind_and_from_address():
    with patch(
        "core.services.launch_waitlist_email.email_service.send_email", new=AsyncMock(return_value="msg_1")
    ) as mock_send:
        result = await launch_waitlist_email.send_waitlist_email(**_KWARGS)

    assert result == "msg_1"
    mock_send.assert_awaited_once()
    call = mock_send.call_args.kwargs
    assert call["to"] == "ada@example.com"
    assert call["kind"] == "launch_waitlist"
    assert call["from_addr"] == "Lightbulb <hello@thelightbulb.company>"
    assert call["subject"] == "You're #42 on the Lightbulb list"


@pytest.mark.asyncio
async def test_html_and_text_contain_position_link_date_and_jump():
    captured = {}

    async def _fake_send(**kwargs):
        captured.update(kwargs)
        return "msg_1"

    with patch("core.services.launch_waitlist_email.email_service.send_email", new=_fake_send):
        await launch_waitlist_email.send_waitlist_email(**_KWARGS)

    for body in (captured["html"], captured["text"]):
        assert "#42" in body
        assert "https://flickmeon.ai/?ref=ABCD1234" in body
        assert "Friday, September 18" in body
        assert "12:00 AM ET" in body
        assert "Each referral moves you up 10 spots" in body
        assert "first 220" in body

    # The share link is a real anchor in the HTML body, not just plain text.
    assert '<a href="https://flickmeon.ai/?ref=ABCD1234"' in captured["html"]


@pytest.mark.asyncio
async def test_escapes_first_name_in_html_but_not_text():
    async def _fake_send(**kwargs):
        return kwargs

    with patch("core.services.launch_waitlist_email.email_service.send_email", new=_fake_send):
        kwargs = {**_KWARGS, "first_name": "<b>Ada</b>"}
        sent = await launch_waitlist_email.send_waitlist_email(**kwargs)

    assert "<b>Ada</b>" not in sent["html"]
    assert "&lt;b&gt;Ada&lt;/b&gt;" in sent["html"]
    assert "<b>Ada</b>" in sent["text"]


@pytest.mark.asyncio
async def test_never_raises_even_if_the_sender_blows_up():
    with patch(
        "core.services.launch_waitlist_email.email_service.send_email",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = await launch_waitlist_email.send_waitlist_email(**_KWARGS)

    assert result is None


@pytest.mark.asyncio
async def test_no_first_name_falls_back_to_generic_greeting():
    async def _fake_send(**kwargs):
        return kwargs

    with patch("core.services.launch_waitlist_email.email_service.send_email", new=_fake_send):
        kwargs = {**_KWARGS, "first_name": None}
        sent = await launch_waitlist_email.send_waitlist_email(**kwargs)

    assert "Hi there" in sent["html"]
    assert "Hi there" in sent["text"]
