"""Competition sign-up sheet emails — the join confirmation ("You're #N")
sent when an owner joins the sheet, and the four close variants sent by the
close action (``core.services.launch_close``): the two winner ones, plus the
paying non-winner ("still in") and the dropped non-winner ("not this round").

Mirrors ``welcome_email.py``'s shape: best-effort, HTML + text, never raises.
No pot numbers in any email — the pot is admin-only.
"""

import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from core.config import settings
from core.services import email_service

logger = logging.getLogger(__name__)

_JOIN_SUBJECT = "You're #{n} for the Lightbulb launch competition"
_WINNER_SUBJECT = "You won a free month of Lightbulb"
_COMPETING_SUBJECT = "You're in the launch competition"
_NOT_SELECTED_SUBJECT = "The launch competition starts October 1"
_ET = ZoneInfo("America/New_York")

_STYLE = (
    "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
    "font-size:16px;line-height:1.6;color:#1a1a1a;max-width:520px;margin:0 auto"
)


def _closes_date(sheet_closes_at: datetime) -> str:
    local = sheet_closes_at.astimezone(_ET)
    return f"{local.strftime('%A, %B %-d')} at {local.strftime('%-I:%M %p')} ET"


def _greeting(first_name: str | None, *, escape: bool) -> str:
    if not first_name:
        return "Hi there,"
    # first_name is user-controlled (from Clerk); escape it so a name with
    # markup can't inject HTML into mail sent from the official sender.
    return f"Hi {html.escape(first_name) if escape else first_name},"


async def send_join_email(
    *,
    to_email: str,
    first_name: str | None,
    position: int,
    referral_link: str,
    referral_jump: int,
    comp_slots: int,
    sheet_closes_at: datetime,
) -> str | None:
    """The join confirmation. Best-effort: returns the SES MessageId or
    None. Never raises — the join must succeed even if SES hiccups."""
    closes = _closes_date(sheet_closes_at)
    referral_line = f"Every friend who subscribes moves you up {referral_jump} spots. Share your link:"
    free_line = f"The first {comp_slots} on the sheet get their first month free."
    html_body = f"""\
<div style="{_STYLE}">
  <p>{_greeting(first_name, escape=True)}</p>
  <p>You're <strong>#{position}</strong> on the Lightbulb launch competition sheet.</p>
  <p>{free_line}</p>
  <p>{referral_line}</p>
  <p>
    <a href="{referral_link}"
       style="display:inline-block;padding:12px 20px;background:#111;color:#fff;text-decoration:none;border-radius:8px">
      {referral_link}
    </a>
  </p>
  <p style="color:#666;font-size:14px">The sheet closes {closes}.</p>
  <p style="color:#999;font-size:13px">— The Lightbulb team</p>
</div>"""
    text_body = f"""\
{_greeting(first_name, escape=False)}

You're #{position} on the Lightbulb launch competition sheet.

{free_line}

{referral_line}
{referral_link}

The sheet closes {closes}.

— The Lightbulb team"""
    try:
        return await email_service.send_email(
            to=to_email,
            subject=_JOIN_SUBJECT.format(n=position),
            html=html_body,
            text=text_body,
            from_addr=settings.EMAIL_FROM_WELCOME,
            reply_to=settings.EMAIL_SUPPORT_ADDRESS,
            kind="launch_sheet_join",
        )
    except Exception:  # noqa: BLE001 — a failed email must never fail the join
        logger.exception("launch sheet join email send raised for %s", to_email)
        return None


async def _send_close(
    to_email: str, first_name: str | None, subject: str, lead: str, detail: str, kind: str
) -> str | None:
    """One close-email shape for every outcome variant — greeting, bold lead,
    detail, sign-off. Best-effort: returns the SES MessageId or None, never
    raises (a failed email must not fail the close)."""
    html_body = f"""\
<div style="{_STYLE}">
  <p>{_greeting(first_name, escape=True)}</p>
  <p><strong>{lead}</strong></p>
  <p>{detail}</p>
  <p style="color:#999;font-size:13px">— The Lightbulb team</p>
</div>"""
    text_body = f"""\
{_greeting(first_name, escape=False)}

{lead}

{detail}

— The Lightbulb team"""
    try:
        return await email_service.send_email(
            to=to_email,
            subject=subject,
            html=html_body,
            text=text_body,
            from_addr=settings.EMAIL_FROM_WELCOME,
            reply_to=settings.EMAIL_SUPPORT_ADDRESS,
            kind=kind,
        )
    except Exception:  # noqa: BLE001 — a failed email must never fail the close action
        logger.exception("launch close email (%s) send raised for %s", kind, to_email)
        return None


async def send_winner_paid_email(*, to_email: str, first_name: str | None) -> str | None:
    """Winner who already pays: the coupon is on their subscription."""
    return await _send_close(
        to_email,
        first_name,
        _WINNER_SUBJECT,
        "Your free month is set — your next invoice is $0.",
        "You made the top of the launch competition sheet, so your next month of Lightbulb is on us.",
        "launch_winner",
    )


async def send_winner_unpaid_email(*, to_email: str, first_name: str | None) -> str | None:
    """Winner not yet subscribed: the coupon pre-applies at checkout. The
    free month is a STARTER month — the coupon is product-restricted, so the
    copy says so."""
    return await _send_close(
        to_email,
        first_name,
        _WINNER_SUBJECT,
        "Your first month of Starter is free — applied automatically at checkout.",
        "You made the top of the launch competition sheet, so your first month of Lightbulb Starter is on us.",
        "launch_winner",
    )


async def send_still_competing_email(*, to_email: str, first_name: str | None, comp_slots: int) -> str | None:
    """Not a winner, but paying — they stay in the competition, just without
    the free month. ``comp_slots`` is passed in (like the join email's) so the
    number always matches the close that produced this send."""
    return await _send_close(
        to_email,
        first_name,
        _COMPETING_SUBJECT,
        "The sign-up sheet is closed, and you're in the competition.",
        f"The {comp_slots} free months went to the earliest spots on the sheet, so this one's on your own "
        "subscription — but you're competing on equal footing from October 1. The board ranks the dollar lift "
        "over your prior 30 days, so what matters now is what your company actually does between now and "
        "December 1.",
        "launch_competing",
    )


async def send_not_selected_email(*, to_email: str, first_name: str | None, comp_slots: int) -> str | None:
    """Neither a winner nor paying — out of the competition, keeps the
    product."""
    return await _send_close(
        to_email,
        first_name,
        _NOT_SELECTED_SUBJECT,
        f"The sign-up sheet is closed, and the {comp_slots} free months are taken.",
        "You're not in the competition this round — those spots went to the earliest sign-ups and the people "
        "who brought others along. Nothing changes about your company: Friday and your agents keep working, "
        "and everything you've built stays yours. Subscribe any time and keep going; we'll be building in "
        "public the whole way through.",
        "launch_not_selected",
    )
