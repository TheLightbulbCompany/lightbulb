"""App-generated mail (welcome email, support-ticket delivery).

This is the seam for mail the *platform* sends about itself, as opposed to
mail an *agent* sends on a user's behalf (``ses_email.send_ses_reply``).
Both ride the same SES transport — this module adds only the concerns that
are specific to app mail:

- **[ENV] subject tagging** so a dev/staging test send is obvious in the
  inbox ("[DEV] Welcome to Isol8"). Prod sends are unprefixed. Agent replies
  deliberately do NOT get this — they go to real customers.
- **Reply-To routing** to the support mailbox.

Everything else is inherited from ``ses_email.send_ses_email``: the
AGENT_EMAIL_DOMAIN gate, running off the event loop, never raising, and the
``email.send`` metric — including the ``result=skipped_unconfigured``
outcome, so an env that cannot send is never silent. That last one matters
for support tickets specifically: the email IS the only capture path, so a
silent skip loses the request outright.

Previously this sent via Resend. It moved to SES because ``isol8.co`` was
already a verified SES domain identity that the backend task role could
already send as, making Resend a second vendor, a second secret, and a
second domain verification for no capability we used.
"""

import logging

from core.config import settings
from core.services import ses_email

logger = logging.getLogger(__name__)


async def send_email(
    *,
    to: str | list[str],
    subject: str,
    html: str,
    from_addr: str,
    reply_to: str | list[str] | None = None,
    text: str | None = None,
    kind: str = "generic",
) -> str | None:
    """Send one app email. Returns the SES MessageId, or None when sending is
    not configured / on failure. Never raises.

    kind is a short label ("welcome", "support_ticket") used only for metric
    dimensions and log context.
    """
    # Tag non-prod mail so dev/staging test sends are obvious in the inbox.
    env = settings.ENVIRONMENT.strip()
    if env and env.lower() not in ("prod", "production"):
        subject = f"[{env.upper()}] {subject}"

    headers = {}
    if reply_to is not None:
        headers["Reply-To"] = ", ".join(reply_to) if isinstance(reply_to, list) else reply_to

    return await ses_email.send_ses_email(
        from_addr=from_addr,
        to=to,
        subject=subject,
        body_text=text,
        body_html=html,
        headers=headers or None,
        kind=kind,
    )
