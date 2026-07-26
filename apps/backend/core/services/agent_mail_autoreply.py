"""Server-templated replies for inbound the agent cannot answer.

Sent when a free-tier owner emails their agent but no run is possible. There
is NO agent run here: SES sending is not gated by the trial cap (the cap
fences Bedrock inference), so we can always answer even when we cannot think.
Nothing is reserved and nothing is spent.

Only ever sent to the OWNER. `routers/ses_inbound.py` enforces a sender
allowlist before this point, so an inbound that reaches the cap check is
authenticated. Unknown-recipient and unverified-sender inbound must stay
silent — replying there would confirm the address exists and turn the
endpoint into a reflector.
"""

import logging

from core.config import settings
from core.observability.metrics import put_metric
from core.services import ses_email
from core.services.trial_cap import DAILY_CAP, TRIAL_EXPIRED

logger = logging.getLogger(__name__)

SETTINGS_URL = "https://isol8.co/settings"

# DAILY_CAP resets at 00:00 UTC, so waiting is a real answer and is offered
# FIRST. Upgrading appears only as an alternative. Telling this owner they
# must pay would simply be false.
_DAILY_CAP_BODY = (
    "I've used today's free allowance, so I can't pick this up right now.\n\n"
    "It resets at 00:00 UTC — send this again after that, or upgrade to remove "
    f"the limit: {SETTINGS_URL}\n"
)

# TRIAL_EXPIRED has nothing to wait for. This is the only state that may
# present payment as required.
_TRIAL_EXPIRED_BODY = (
    "Your free plan has ended, so I can't run right now.\n\n"
    f"Upgrade and I'll pick this straight back up: {SETTINGS_URL}\n"
)

_BODIES = {DAILY_CAP: _DAILY_CAP_BODY, TRIAL_EXPIRED: _TRIAL_EXPIRED_BODY}


async def send_cap_autoreply(*, reason: str, from_addr: str, to: str, in_reply_to: str | None, subject: str) -> bool:
    """Answer an inbound that cannot be run. True when SES accepted it.

    Never raises — the ingest webhook must stay terminal.
    """
    try:
        if not (settings.AGENT_EMAIL_DOMAIN or "").strip():
            return False

        body = _BODIES.get(reason)
        if body is None:
            # An unrecognised reason must not produce a generic upgrade nudge:
            # we would be guessing whether the owner has to pay.
            logger.warning("agent_mail_autoreply: no template for reason %r", reason)
            return False

        headers = {"In-Reply-To": in_reply_to, "References": in_reply_to} if in_reply_to else None

        message_id = await ses_email.send_ses_reply(
            from_addr=from_addr,
            to=to,
            subject=subject,
            body_text=body,
            body_html=None,
            headers=headers,
        )
        ok = message_id is not None
        put_metric(
            "agent_email.autoreply",
            dimensions={"reason": reason, "result": "sent" if ok else "failed"},
        )
        return ok
    except Exception:
        logger.exception("agent_mail_autoreply failed for reason=%s", reason)
        put_metric("agent_email.autoreply", dimensions={"reason": reason, "result": "error"})
        return False
