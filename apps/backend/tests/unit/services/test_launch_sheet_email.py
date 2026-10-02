"""Sheet emails — join confirmation + the four close variants (two winner,
still-competing, not-selected). No pot numbers anywhere."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.services import launch_sheet_email as em

CLOSES = datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)  # noon ET


async def _send_join(**over):
    kwargs = dict(
        to_email="a@x.com",
        first_name="Ada",
        position=7,
        referral_link="https://dev.thelightbulb.company/?ref=ABCD2345",
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
        assert "https://dev.thelightbulb.company/?ref=ABCD2345" in body
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
    assert "Your first month of Workspace is free — applied automatically at checkout." in kw["text"]


async def test_still_competing_email_copy():
    # A NON-default slot count: the number is threaded in from the close, so
    # a hard-coded 200 in the copy would fail here.
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="msg_4")) as send:
        assert await em.send_still_competing_email(to_email="a@x.com", first_name=None, comp_slots=50) == "msg_4"
    kw = send.await_args.kwargs
    assert kw["subject"] == "You're in the launch competition"
    assert kw["kind"] == "launch_competing"
    for body in (kw["html"], kw["text"]):
        assert "The sign-up sheet is closed, and you're in the competition." in body
        assert "The 50 free months went to the earliest spots on the sheet" in body
        assert "200" not in body
        assert "competing on equal footing from October 1" in body


async def test_not_selected_email_copy():
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="msg_5")) as send:
        assert await em.send_not_selected_email(to_email="a@x.com", first_name=None, comp_slots=50) == "msg_5"
    kw = send.await_args.kwargs
    assert kw["subject"] == "The launch competition starts October 1"
    assert kw["kind"] == "launch_not_selected"
    for body in (kw["html"], kw["text"]):
        assert "The sign-up sheet is closed, and the 50 free months are taken." in body
        assert "200" not in body
        assert "You're not in the competition this round" in body


async def test_close_emails_never_raise():
    with patch.object(em.email_service, "send_email", AsyncMock(side_effect=RuntimeError("ses down"))):
        assert await em.send_still_competing_email(to_email="a@x.com", first_name=None, comp_slots=200) is None
        assert await em.send_not_selected_email(to_email="a@x.com", first_name=None, comp_slots=200) is None


async def test_no_email_mentions_the_pot():
    _, join_send = await _send_join()
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="m")) as paid_send:
        await em.send_winner_paid_email(to_email="a@x.com", first_name=None)
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="m")) as unpaid_send:
        await em.send_winner_unpaid_email(to_email="a@x.com", first_name=None)
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="m")) as competing_send:
        await em.send_still_competing_email(to_email="a@x.com", first_name=None, comp_slots=200)
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="m")) as not_selected_send:
        await em.send_not_selected_email(to_email="a@x.com", first_name=None, comp_slots=200)
    import re

    for send in (join_send, paid_send, unpaid_send, competing_send, not_selected_send):
        kw = send.await_args.kwargs
        # \bpot\b, not a substring check — "spots" is fine, the pot is not.
        assert not re.search(r"\bpot\b", kw["html"], re.I) and not re.search(r"\bpot\b", kw["text"], re.I)
        assert "$1,000" not in kw["text"] and "1000" not in kw["text"]


# ---------------------------------------------------------------------------
# Admin-editable copy — the row holds OVERRIDES, the module holds the defaults
# ---------------------------------------------------------------------------


async def test_an_override_replaces_the_default_copy():
    copy = {"competing": {"subject": "Custom subject", "lead": "Custom lead.", "detail": "Custom detail."}}
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="m")) as send:
        await em.send_still_competing_email(to_email="a@x.com", first_name=None, comp_slots=200, copy=copy)
    kw = send.await_args.kwargs
    assert kw["subject"] == "Custom subject"
    assert "Custom lead." in kw["text"] and "Custom detail." in kw["html"]
    assert "you're in the competition" not in kw["text"]


async def test_a_blank_or_missing_override_field_falls_back_to_the_default():
    # subject overridden, lead blank (whitespace only), detail absent.
    copy = {"not_selected": {"subject": "Only the subject", "lead": "   "}}
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="m")) as send:
        await em.send_not_selected_email(to_email="a@x.com", first_name=None, comp_slots=200, copy=copy)
    kw = send.await_args.kwargs
    assert kw["subject"] == "Only the subject"
    assert "The sign-up sheet is closed, and the 200 free months are taken." in kw["text"]
    assert "You're not in the competition this round" in kw["text"]


async def test_no_override_at_all_is_the_shipped_default():
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="m")) as send:
        await em.send_still_competing_email(to_email="a@x.com", first_name=None, comp_slots=200, copy={})
    assert "and you're in the competition." in send.await_args.kwargs["text"]


async def test_admin_copy_is_escaped_never_rendered_as_markup():
    """Admin copy is PLAIN TEXT — a `<b>` in it must reach the inbox as
    characters, exactly like a user-supplied first name does."""
    copy = {"competing": {"lead": "<b>bold</b> & <script>alert(1)</script>"}}
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="m")) as send:
        await em.send_still_competing_email(to_email="a@x.com", first_name=None, comp_slots=200, copy=copy)
    kw = send.await_args.kwargs
    assert "<b>" not in kw["html"] and "<script>" not in kw["html"]
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; &lt;script&gt;" in kw["html"]
    # …and apostrophes/quotes stay readable (quote=False — these are text nodes).
    assert "&#x27;" not in kw["html"]


async def test_an_unknown_placeholder_renders_literally_and_never_raises():
    """A typo in admin copy must not take the close down with a KeyError."""
    copy = {"not_selected": {"detail": "Slots: {comp_slots}, typo: {compslots} {referral_link}"}}
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="m")) as send:
        assert await em.send_not_selected_email(to_email="a@x.com", first_name=None, comp_slots=50, copy=copy)
    body = send.await_args.kwargs["text"]
    assert "Slots: 50, typo: {compslots} {referral_link}" in body


async def test_unbalanced_braces_render_raw_instead_of_raising():
    copy = {"competing": {"lead": "half open {comp_slots"}}
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="m")) as send:
        assert await em.send_still_competing_email(to_email="a@x.com", first_name=None, comp_slots=9, copy=copy)
    assert "half open {comp_slots" in send.await_args.kwargs["text"]


async def test_join_email_copy_can_be_overridden_with_its_placeholders():
    copy = {"join": {"lead": "Spot {position} of {comp_slots}.", "detail": "Closes {sheet_closes}."}}
    result, send = await _send_join(copy=copy)
    assert result == "msg_1"
    kw = send.await_args.kwargs
    for body in (kw["html"], kw["text"]):
        assert "Spot 7 of 200." in body
        assert "Closes Wednesday, September 30 at 12:00 PM ET." in body
    # The referral CTA and the button are code, not copy — still there.
    assert "https://dev.thelightbulb.company/?ref=ABCD2345" in kw["html"]


def test_placeholders_in_finds_names_and_rejects_unbalanced():
    assert em.placeholders_in("a {comp_slots} b {position}") == {"comp_slots", "position"}
    assert em.placeholders_in("no placeholders") == set()
    with pytest.raises(ValueError):
        em.placeholders_in("{oops")


async def test_send_test_uses_a_distinguishable_kind():
    values = em.sample_values(comp_slots=200, referral_jump=10, sheet_closes_at=CLOSES)
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="m")) as send:
        assert await em.send_test("competing", to_email="admin@x.com", copy=None, values=values) == "m"
    kw = send.await_args.kwargs
    assert kw["to"] == "admin@x.com" and kw["kind"] == "launch_competing_test"


async def test_every_variant_renders_with_sample_values():
    values = em.sample_values(comp_slots=200, referral_jump=10, sheet_closes_at=CLOSES)
    for variant in em.VARIANTS:
        body = em.render(variant, copy=None, values=values)
        assert body["subject"] and body["html"].startswith("<div") and body["text"]


def test_placeholders_in_rejects_attribute_and_index_traversal():
    """`format_map` walks attributes at RENDER time, so `{comp_slots.__class__}`
    would put a value's internals in a customer's inbox. The gate is in the
    renderer's own path, not only in the admin validator."""
    for bad in ("{comp_slots.__class__}", "{comp_slots[0]}", "{x.y.z}"):
        with pytest.raises(ValueError, match="attribute and index"):
            em.placeholders_in(bad)


def test_placeholders_in_rejects_format_specs_and_conversions():
    """`{comp_slots:>999999999}` renders a ~1GB string before any guard can
    run, on the single non-autoscaled backend task."""
    with pytest.raises(ValueError, match="format specs"):
        em.placeholders_in("{comp_slots:>999999999}")
    with pytest.raises(ValueError, match="format specs"):
        em.placeholders_in("{comp_slots!r}")


async def test_a_traversing_or_amplifying_placeholder_renders_raw():
    """Even reaching render() directly — no validator in the way — the copy
    comes out as literal text, never expanded."""
    for bad in ("{comp_slots.__class__}", "{comp_slots:>999999}"):
        body = em.render("competing", copy={"competing": {"lead": bad}}, values={"comp_slots": 200})
        assert bad in body["text"]  # literal, not expanded
        assert "&lt;class" not in body["html"] and "<class" not in body["text"]
        assert len(body["text"]) < 2000  # no width-spec amplification


def test_preview_document_carries_its_own_light_ground():
    """The body fragment hardcodes a dark text colour, so the preview must
    bring its own white ground rather than inheriting the admin console's
    theme — and it must live in the DOCUMENT, not on the iframe element, so
    the console itself stays on brand tokens."""
    doc = em.preview_document("<div><p>body</p></div>")
    assert doc.startswith("<!doctype html>")
    assert "background:#ffffff" in doc
    assert "<div><p>body</p></div>" in doc


async def test_a_real_send_is_the_bare_fragment_not_the_preview_document():
    with patch.object(em.email_service, "send_email", AsyncMock(return_value="m")) as send:
        await em.send_still_competing_email(to_email="a@x.com", first_name=None, comp_slots=200)
    assert send.await_args.kwargs["html"].startswith("<div")
