"""The welcome email sent once when a user signs up.

Invoked best-effort from the Clerk user.created webhook. Idempotency-keyed on
the (stable) Clerk user id so a webhook retry / double-process can't send a
second welcome within Resend's 24h window.
"""

import logging

from core.config import settings
from core.services import email_service

logger = logging.getLogger(__name__)

_SUBJECT = "Welcome to Isol8 \U0001f44b"


def _render_welcome_html(first_name: str | None, chat_url: str) -> str:
    greeting = f"Hi {first_name}," if first_name else "Hi there,"
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:16px;line-height:1.5;color:#1a1a1a;max-width:520px;margin:0 auto">
  <p>{greeting}</p>
  <p>Welcome to <strong>Isol8</strong> — your own AI agent team is ready.</p>
  <p>Head to your workspace to meet Friday (your orchestrator) and start delegating work:</p>
  <p>
    <a href="{chat_url}/chat"
       style="display:inline-block;padding:12px 20px;background:#111;color:#fff;text-decoration:none;border-radius:8px">
      Open your workspace
    </a>
  </p>
  <p style="color:#666;font-size:14px">
    Questions or hit a snag? Just reply to this email and our team (and our
    own agents) will help.
  </p>
  <p style="color:#999;font-size:13px">— The Isol8 team</p>
</div>"""


async def send_welcome_email(*, to_email: str, user_id: str, first_name: str | None = None) -> str | None:
    """Send the welcome email. Best-effort: returns the Resend id or None.
    Never raises (the seam swallows its own errors)."""
    html = _render_welcome_html(first_name, settings.FRONTEND_URL)
    return await email_service.send_email(
        to=to_email,
        subject=_SUBJECT,
        html=html,
        from_addr=settings.EMAIL_FROM_WELCOME,
        reply_to=settings.EMAIL_SUPPORT_ADDRESS,
        idempotency_key=f"welcome-user/{user_id}",
        kind="welcome",
    )
