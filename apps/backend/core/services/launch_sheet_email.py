"""Competition sign-up sheet emails — the join confirmation ("You're #N")
sent when an owner joins the sheet, and the two winner variants sent by the
close action (``core.services.launch_close``).

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


async def _send_winner(to_email: str, first_name: str | None, lead: str, detail: str) -> str | None:
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
            subject=_WINNER_SUBJECT,
            html=html_body,
            text=text_body,
            from_addr=settings.EMAIL_FROM_WELCOME,
            reply_to=settings.EMAIL_SUPPORT_ADDRESS,
            kind="launch_winner",
        )
    except Exception:  # noqa: BLE001 — a failed email must never fail the close action
        logger.exception("launch winner email send raised for %s", to_email)
        return None


async def send_winner_paid_email(*, to_email: str, first_name: str | None) -> str | None:
    """Winner who already pays: the coupon is on their subscription."""
    return await _send_winner(
        to_email,
        first_name,
        "Your free month is set — your next invoice is $0.",
        "You made the top of the launch competition sheet, so your next month of Lightbulb is on us.",
    )


async def send_winner_unpaid_email(*, to_email: str, first_name: str | None) -> str | None:
    """Winner not yet subscribed: the coupon pre-applies at checkout."""
    return await _send_winner(
        to_email,
        first_name,
        "Your free month is waiting — subscribe and it's applied automatically at checkout.",
        "You made the top of the launch competition sheet, so your first month of Lightbulb is on us.",
    )
