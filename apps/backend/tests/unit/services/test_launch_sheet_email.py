"""Sheet emails — join confirmation + the two winner variants. No pot
numbers anywhere."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from core.services import launch_sheet_email as em

CLOSES = datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)  # noon ET


async def _send_join(**over):
    kwargs = dict(
        to_email="a@x.com",
        first_name="Ada",
        position=7,
        referral_link="https://dev.flickmeon.ai/?ref=ABCD2345",
        referral_jump=10,
        comp_slots=200,
        sheet_closes_at=CLOSES,
    )
    kwargs.update(over)
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="msg_1")) as send:
        result = await em.send_join_email(**kwargs)
    return result, send


async def test_join_email_subject_and_body():
    result, send = await _send_join()
    assert result == "msg_1"
    kw = send.await_args.kwargs
    assert kw["subject"] == "You're #7 for the Lightbulb launch competition"
    assert kw["to"] == "a@x.com" and kw["kind"] == "launch_sheet_join"
    for body in (kw["html"], kw["text"]):
        assert "#7" in body
        assert "The first 200 on the sheet get their first month free." in body
        assert "Every friend who subscribes moves you up 10 spots" in body
        assert "https://dev.flickmeon.ai/?ref=ABCD2345" in body
        assert "Wednesday, September 30" in body and "12:00 PM ET" in body


async def test_join_email_escapes_the_first_name_in_html():
    _, send = await _send_join(first_name="<img src=x>")
    kw = send.await_args.kwargs
    assert "<img" not in kw["html"] and "&lt;img" in kw["html"]


async def test_join_email_never_raises():
    with patch.object(em.email_service, "send_email", AsyncMock(side_effect=RuntimeError("ses down"))):
        assert (
            await em.send_join_email(
                to_email="a@x.com",
                first_name=None,
                position=1,
                referral_link="x",
                referral_jump=10,
                comp_slots=200,
                sheet_closes_at=CLOSES,
            )
            is None
        )


async def test_winner_paid_email_copy():
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="msg_2")) as send:
        assert await em.send_winner_paid_email(to_email="a@x.com", first_name=None) == "msg_2"
    kw = send.await_args.kwargs
    assert "Your free month is set — your next invoice is $0." in kw["text"]
    assert kw["kind"] == "launch_winner"


async def test_winner_unpaid_email_copy():
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="msg_3")) as send:
        assert await em.send_winner_unpaid_email(to_email="a@x.com", first_name=None) == "msg_3"
    kw = send.await_args.kwargs
    assert "Your first month of Starter is free — applied automatically at checkout." in kw["text"]


async def test_no_email_mentions_the_pot():
    _, join_send = await _send_join()
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="m")) as paid_send:
        await em.send_winner_paid_email(to_email="a@x.com", first_name=None)
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="m")) as unpaid_send:
        await em.send_winner_unpaid_email(to_email="a@x.com", first_name=None)
    import re

    for send in (join_send, paid_send, unpaid_send):
        kw = send.await_args.kwargs
        # \bpot\b, not a substring check — "spots" is fine, the pot is not.
        assert not re.search(r"\bpot\b", kw["html"], re.I) and not re.search(r"\bpot\b", kw["text"], re.I)
        assert "$1,000" not in kw["text"] and "1000" not in kw["text"]
