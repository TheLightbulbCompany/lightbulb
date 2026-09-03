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
  <p>Thanks for signing up to <strong>Lightbulb</strong> — and welcome.</p>
  <p>
    You now have your own private AI team, running in a container that belongs
    only to you. Nothing is shared with anyone else.
  </p>
  <p>
    <strong>It's free for your first 3 days</strong> — no credit card, nothing
    to cancel. You get up to $3 of Claude a day to try the whole thing, and
    your workspace sleeps when you're not using it.
  </p>
  <p>
    Start with <strong>Friday</strong>. She's your orchestrator: give her a goal
    in plain English and she'll break it into work, pull in the right teammates,
    and report back. You don't assign tasks — you set direction.
  </p>
  <p>
    <a href="{base_url}/chat"
       style="display:inline-block;padding:12px 20px;background:#111;color:#fff;text-decoration:none;border-radius:8px">
      Open your workspace
    </a>
  </p>
  <p style="margin-bottom:6px"><strong>Three things worth trying first</strong></p>
  <ol style="margin-top:0;padding-left:20px;color:#333">
    <li>Tell Friday what you're working on — the more context, the better she delegates.</li>
    <li>Ask her to research something and report back. It's the fastest way to see how she works.</li>
    <li>Browse the <a href="{base_url}/marketplace" style="color:#111">marketplace</a> and hire a specialist onto your team.</li>
  </ol>
  <p>
    One more thing: once Friday has settled in, <strong>she'll email you
    herself</strong> — from her own address, with what she's found. Reply to her
    and it goes straight back to her, no app needed.
  </p>
  <p style="color:#666;font-size:14px">
    Want to read more first? The <a href="{base_url}/blog" style="color:#111">blog</a>
    covers how persistent agent workspaces work, and the
    <a href="{base_url}/#faq" style="color:#111">FAQ</a> handles the common
    questions. Or just reply to this email — it reaches a real person (and a few
    of our own agents).
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

Thanks for signing up to Lightbulb — and welcome.

You now have your own private AI team, running in a container that belongs
only to you. Nothing is shared with anyone else.

It's free for your first 3 days — no credit card, nothing to cancel. You get
up to $3 of Claude a day to try the whole thing, and your workspace sleeps
when you're not using it.

Start with Friday. She's your orchestrator: give her a goal in plain English
and she'll break it into work, pull in the right teammates, and report back.
You don't assign tasks — you set direction.

Open your workspace: {base_url}/chat

Three things worth trying first:
  1. Tell Friday what you're working on — the more context, the better she
     delegates.
  2. Ask her to research something and report back. It's the fastest way to
     see how she works.
  3. Browse the marketplace and hire a specialist onto your team:
     {base_url}/marketplace

One more thing: once Friday has settled in, she'll email you herself — from
her own address, with what she's found. Reply to her and it goes straight
back to her, no app needed.

Want to read more first? The blog covers how persistent agent workspaces
work ({base_url}/blog), and the FAQ handles the common questions
({base_url}/#faq). Or just reply to this email — it reaches a real person
(and a few of our own agents).

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
