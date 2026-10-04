"""Server-templated replies for inbound the agent cannot answer.

Sent when a free-tier owner emails their agent but no run is possible. There
is NO agent run here: SES sending is not gated by the trial cap (the cap
fences Bedrock inference), so we can always answer even when we cannot think.
Nothing is reserved and nothing is spent.

Only ever sent to an EXPLICITLY ALLOWLISTED sender (the owner, or someone on the
owner's ``verified_senders`` — `sender_allowed`), and only after the sender is
authenticated. `routers/ses_inbound.py` enforces both before calling here — that
is the entire safety argument for answering at all. An open-mode stranger (an
address with ``send_to_anyone`` on, the default) may wake the agent but is
NEVER answered here: the router drops an over-cap stranger silently
(``dropped_over_cap``). Unknown-recipient and unverified-sender inbound stay
silent for the same reason: replying would confirm the address exists and turn
the endpoint into a reflector.

Two independent loop fences, because this is machine-generated mail:
``Auto-Submitted: auto-replied`` (RFC 3834) is what tells a well-behaved
autoresponder on the other side not to answer back — it is the same signal
our own ``is_auto_submitted`` guard trips on — and the durable per-owner
daily send counter bounds the damage when the other side ignores it (each
bounce carries a fresh Message-ID, so dedup will not catch the loop).
"""

import logging
from datetime import datetime, timezone

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import agent_mail_inbox_repo, container_repo
from core.services import agent_mail_reply, ses_email
from core.services.agent_email import from_header
from core.services.agent_mail_owner_send import DAILY_SEND_CAP
from core.services.trial_cap import DAILY_CAP, INCLUDED_CAP, TRIAL_EXPIRED

logger = logging.getLogger(__name__)

SETTINGS_URL = f"{settings.FRONTEND_URL.rstrip('/')}/settings"

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

# A paying capped owner: nothing to upgrade out of a free tier, just wait for the reset.
_INCLUDED_CAP_BODY = (
    "I've used today's included usage, so I can't pick this up right now.\n\n"
    "It resets at 00:00 UTC — send this again after that.\n"
)

_BODIES = {DAILY_CAP: _DAILY_CAP_BODY, INCLUDED_CAP: _INCLUDED_CAP_BODY, TRIAL_EXPIRED: _TRIAL_EXPIRED_BODY}


async def send_cap_autoreply(
    *, owner_id: str, agent_id: str, reason: str, from_addr: str, to: str, in_reply_to: str | None, subject: str
) -> bool:
    """Answer an inbound that cannot be run, as ``agent_id`` from the team
    address ``from_addr``. True when SES accepted it.

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

        # Charged only once a send is actually going to be attempted. Shares
        # the owner's one durable daily outbound budget with `owner_send` on
        # purpose: it is the same mailbox being written to, and one shared
        # counter is what makes "at most N machine emails a day" true no matter
        # which path loops. See the module docstring.
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not await container_repo.try_consume_daily_email_send(owner_id, day=day, cap=DAILY_SEND_CAP):
            logger.warning("agent_mail_autoreply: daily send cap exhausted for %s", owner_id)
            put_metric("agent_email.autoreply", dimensions={"reason": reason, "result": "rate_limited"})
            return False

        # `in_reply_to` and `subject` both originate in the SENDER's own mail —
        # sanitized here exactly as the sibling reply path does.
        in_reply_to = agent_mail_reply._header_safe(in_reply_to or "")
        # Auto-Submitted marks this as machine mail (RFC 3834) so the other
        # side's autoresponder does not answer it — the same header our own
        # inbound loop guard drops on.
        headers = {"Auto-Submitted": "auto-replied"}
        if in_reply_to:
            headers["In-Reply-To"] = in_reply_to
            headers["References"] = in_reply_to

        sent_subject = agent_mail_reply._header_safe(subject or "")
        message_id = await ses_email.send_ses_reply(
            from_addr=await from_header(owner_id, agent_id, from_addr),
            to=to,
            subject=sent_subject,
            body_text=body,
            body_html=None,
            headers=headers,
        )
        ok = message_id is not None
        if ok:
            await agent_mail_inbox_repo.put_sent(
                address=from_addr,
                message_id=message_id,
                owner_id=owner_id,
                agent_id=agent_id,
                to=to,
                subject=sent_subject,
                text=body,
            )
        put_metric(
            "agent_email.autoreply",
            dimensions={"reason": reason, "result": "sent" if ok else "failed"},
        )
        return ok
    except Exception:
        logger.exception("agent_mail_autoreply failed for reason=%s", reason)
        put_metric("agent_email.autoreply", dimensions={"reason": reason, "result": "error"})
        return False
