"""The "You're #N on the list" waitlist email — sent once when a user signs
up while the Launch Competition is still in its waitlist phase (before the
drop), in place of the generic welcome email (see ``routers/webhooks.py``
``user.created``).

Mirrors ``welcome_email.py``'s shape: best-effort, HTML + text, never raises.
"""

import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from core.config import settings
from core.services import email_service

logger = logging.getLogger(__name__)

_SUBJECT = "You're #{n} on the Lightbulb list"
_ET = ZoneInfo("America/New_York")


def _doors_open_date(drop_at: datetime) -> str:
    # Same format as the pre-drop hold copy (launch_hold.py) — one phrase,
    # one place readers learn it.
    local = drop_at.astimezone(_ET)
    return f"{local.strftime('%A, %B %-d')} at {local.strftime('%-I:%M %p')} ET"


def _render_waitlist_html(
    first_name: str | None,
    effective_position: int,
    referral_link: str,
    drop_at: datetime,
    referral_jump: int,
    comp_slots: int,
) -> str:
    # first_name is user-controlled (from Clerk); escape it so a name with
    # markup can't inject HTML into mail sent from the official sender.
    greeting = f"Hi {html.escape(first_name)}," if first_name else "Hi there,"
    doors_open = _doors_open_date(drop_at)
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:16px;line-height:1.6;color:#1a1a1a;max-width:520px;margin:0 auto">
  <p>{greeting}</p>
  <p>You're <strong>#{effective_position}</strong> on the Lightbulb waitlist.</p>
  <p>Doors open <strong>{doors_open}</strong>.</p>
  <p>Each referral moves you up {referral_jump} spots. Share your link:</p>
  <p>
    <a href="{referral_link}"
       style="display:inline-block;padding:12px 20px;background:#111;color:#fff;text-decoration:none;border-radius:8px">
      {referral_link}
    </a>
  </p>
  <p style="color:#666;font-size:14px">
    The first {comp_slots} on the list get their first month free.
  </p>
  <p style="color:#999;font-size:13px">— The Lightbulb team</p>
</div>"""


def _render_waitlist_text(
    first_name: str | None,
    effective_position: int,
    referral_link: str,
    drop_at: datetime,
    referral_jump: int,
    comp_slots: int,
) -> str:
    greeting = f"Hi {first_name}," if first_name else "Hi there,"
    doors_open = _doors_open_date(drop_at)
    return f"""\
{greeting}

You're #{effective_position} on the Lightbulb waitlist.

Doors open {doors_open}.

Each referral moves you up {referral_jump} spots. Share your link:
{referral_link}

The first {comp_slots} on the list get their first month free.

— The Lightbulb team"""


async def send_waitlist_email(
    *,
    to_email: str,
    first_name: str | None,
    effective_position: int,
    referral_link: str,
    drop_at: datetime,
    referral_jump: int,
    comp_slots: int,
) -> str | None:
    """Send the waitlist email. Best-effort: returns the SES MessageId or
    None. Never raises — ``email_service.send_email`` already swallows its
    own errors, but this is on the webhook path so we guard anyway."""
    try:
        return await email_service.send_email(
            to=to_email,
            subject=_SUBJECT.format(n=effective_position),
            html=_render_waitlist_html(
                first_name, effective_position, referral_link, drop_at, referral_jump, comp_slots
            ),
            text=_render_waitlist_text(
                first_name, effective_position, referral_link, drop_at, referral_jump, comp_slots
            ),
            from_addr=settings.EMAIL_FROM_WELCOME,
            reply_to=settings.EMAIL_SUPPORT_ADDRESS,
            kind="launch_waitlist",
        )
    except Exception:  # noqa: BLE001 — this is on the user.created webhook path; never fail it
        logger.exception("launch waitlist email send raised for %s", to_email)
        return None
