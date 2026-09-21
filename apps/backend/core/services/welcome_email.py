"""The welcome email sent once when a user signs up.

Invoked best-effort from the Clerk user.created webhook. Send-once is
enforced upstream, not here: the webhook dedupes on svix-id and returns
before reaching this call on any retry (``routers/webhooks.py``). The
transport itself has no idempotency key — SES has no equivalent of the one
Resend offered.
"""

import html
import logging

from core.config import settings
from core.services import email_service

logger = logging.getLogger(__name__)

_SUBJECT = "Welcome to Lightbulb \U0001f44b"


def _render_welcome_html(first_name: str | None, base_url: str) -> str:
    # first_name is user-controlled (from Clerk); escape it so a name with
    # markup can't inject HTML into mail sent from the official sender.
    greeting = f"Hi {html.escape(first_name)}," if first_name else "Hi there,"
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:16px;line-height:1.6;color:#1a1a1a;max-width:520px;margin:0 auto">
  <p>{greeting}</p>
  <p>Thanks for signing up to <strong>Lightbulb</strong>.</p>
  <p>
    Lightbulb is a messaging workspace where your AI team are members of the
    channels, rather than a chatbot you prompt one question at a time. You
    already have one: a <strong>CEO and five specialists</strong> — Engineering,
    Finance, Growth, Analytics and Customer Operations — waiting in #general.
    Say what you want to happen; the CEO works out who does it and reports back
    in the room.
  </p>
  <p>
    The whole workspace runs in a container that belongs only to you. Nothing is
    shared, and nothing resets between conversations — files, context and
    history stay where you left them.
  </p>
  <p>
    <strong>It's free for your first 30 days.</strong> No credit card, and the
    workspace sleeps when you're not using it.
  </p>
  <p>
    <a href="{base_url}/chat"
       style="display:inline-block;padding:12px 20px;background:#111;color:#fff;text-decoration:none;border-radius:8px">
      Open your workspace
    </a>
  </p>
  <p>
    Two things worth knowing. The CEO wakes overnight, moves one thing forward
    and emails you what happened — reply straight from your inbox and it goes
    back to the team. And when you need a skill the six of them don't have, you
    can hire a specialist from the
    <a href="{base_url}/marketplace" style="color:#111">marketplace</a>.
  </p>
  <p style="color:#666;font-size:14px">
    New here? The <a href="{base_url}/docs" style="color:#111">docs</a> walk
    through how it works, and the
    <a href="{base_url}/blog" style="color:#111">blog</a> goes deeper on
    persistent agent workspaces. Or just reply to this email — it reaches a real
    person (and a few of our own agents).
  </p>
  <p style="color:#999;font-size:13px">— The Lightbulb team</p>
</div>"""


def _render_welcome_text(first_name: str | None, base_url: str) -> str:
    """Plain-text alternative. Not decoration: an HTML-only message is a
    deliverability penalty with most providers, and a few clients render it
    badly. Kept in sync with the HTML above by hand — it's short enough that a
    templating layer would cost more than it saves."""
    greeting = f"Hi {first_name}," if first_name else "Hi there,"
    return f"""\
{greeting}

Thanks for signing up to Lightbulb.

Lightbulb is a messaging workspace where your AI team are members of the
channels, rather than a chatbot you prompt one question at a time. You already
have one: a CEO and five specialists — Engineering, Finance, Growth, Analytics
and Customer Operations — waiting in #general. Say what you want to happen; the
CEO works out who does it and reports back in the room.

The whole workspace runs in a container that belongs only to you. Nothing is
shared, and nothing resets between conversations — files, context and history
stay where you left them.

It's free for your first 30 days. No credit card, and the workspace sleeps when
you're not using it.

Open your workspace: {base_url}/chat

Two things worth knowing. The CEO wakes overnight, moves one thing forward and
emails you what happened — reply straight from your inbox and it goes back to
the team. And when you need a skill the six of them don't have, you can hire a
specialist from the marketplace: {base_url}/marketplace

New here? The docs walk through how it works ({base_url}/docs), and the blog
goes deeper on persistent agent workspaces ({base_url}/blog). Or just reply to
this email — it reaches a real person (and a few of our own agents).

— The Lightbulb team"""


async def send_welcome_email(*, to_email: str, first_name: str | None = None) -> str | None:
    """Send the welcome email. Best-effort: returns the SES MessageId or None.
    Never raises (the seam swallows its own errors)."""
    base_url = settings.FRONTEND_URL.rstrip("/")
    return await email_service.send_email(
        to=to_email,
        subject=_SUBJECT,
        html=_render_welcome_html(first_name, base_url),
        text=_render_welcome_text(first_name, base_url),
        from_addr=settings.EMAIL_FROM_WELCOME,
        reply_to=settings.EMAIL_SUPPORT_ADDRESS,
        kind="welcome",
    )
