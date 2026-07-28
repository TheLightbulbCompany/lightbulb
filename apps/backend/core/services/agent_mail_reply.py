"""Turn a decoded reply-context into a sent, threaded reply email.

Consumes the ``agent-email-addresses`` address book (via
``agent_email_repo``) to resolve which of an owner's addresses belongs to
the replying agent, and the SES-backed outbound-email seam (``ses_email``)
to actually send the reply from the agent's own subdomain address. Resend
cannot originate mail from an arbitrary owner subdomain, so agent replies
go out via SES while the app's own welcome/support mail stays on
``email_service`` (Resend) — see that module's docstring. Both dependencies
are imported as modules (not `from ... import ...`) so tests can monkeypatch
their attributes without needing to patch this module's own names.

``send_reply`` never raises — a reply failure must never take down the
inbound-mail handler that called it. It returns a short result string the
caller can log/metric on instead.
"""

import logging

from core.config import settings
from core.repositories import agent_email_repo, billing_repo, container_repo
from core.services import ses_email

logger = logging.getLogger(__name__)

_COLD_WAKE_FOOTER = (
    "\n\n———\n"
    "I was asleep when this arrived — on the free plan I sleep when idle, so "
    "replies wait for me to wake. https://isol8.co/settings\n"
)


async def _maybe_cold_wake_footer(owner_id: str) -> str:
    """The free-tier "I was asleep" note, or "".

    Consumed (and cleared) here so it appears exactly once. Free tier only: a
    paid container never sleeps, so the note would be a lie. Never raises — a
    footer must not cost a reply.
    """
    try:
        if not await container_repo.consume_email_cold_wake(owner_id):
            return ""
        account = await billing_repo.get_by_owner_id(owner_id) or {}
        if account.get("subscription_status") != "trial_local":
            return ""
        return _COLD_WAKE_FOOTER
    except Exception:  # noqa: BLE001
        logger.warning("cold-wake footer lookup failed for %s", owner_id)
        return ""


def _header_safe(value: str) -> str:
    """Strip CR/LF and other control characters from a value bound for an email
    header. The value (a Message-ID from the original inbound mail) is
    sender-controlled, so we sanitize before it reaches the provider — defense
    in depth against header injection regardless of the provider's own handling."""
    return "".join(ch for ch in value if ch == "\t" or (ch >= " " and ch != "\x7f")).strip()


# Cap on how many message-ids the References chain carries. RFC 5322 §3.6.4
# explicitly permits truncating an over-long chain, and prescribes keeping the
# FIRST id — the thread root — which is also what any thread identity derived
# from References[0] depends on. So drop from the middle, never the head.
_MAX_REFERENCES = 20


def build_references(parent_refs: str, parent_message_id: str) -> str:
    """The reply's References: the parent's chain plus the parent's Message-ID.

    Truncates from the MIDDLE when the chain grows past ``_MAX_REFERENCES``,
    preserving the root and the most recent ancestors — dropping the head
    instead would move the thread root and split one conversation in two.
    """
    ids = [part for part in f"{parent_refs} {parent_message_id}".split() if part]
    # De-dupe while preserving order: a client that already appended the
    # parent's Message-ID to References would otherwise repeat it.
    seen: set[str] = set()
    deduped = [i for i in ids if not (i in seen or seen.add(i))]
    if len(deduped) <= _MAX_REFERENCES:
        return " ".join(deduped)
    return " ".join([deduped[0], *deduped[-(_MAX_REFERENCES - 1) :]])


def reply_subject(original: str) -> str:
    """Prefix with 'Re: ', unless original already carries a Re: prefix."""
    if original.strip().lower().startswith("re:"):
        return original
    return f"Re: {original}"


async def resolve_from_address(owner_id: str, agent_id: str) -> str | None:
    """The active address row for ``agent_id`` among ``owner_id``'s rows, or None.

    An owner can accumulate multiple rows per agent over time (aliases); only
    the current ``status == "active"`` row is a valid from-address.
    """
    rows = await agent_email_repo.query_by_owner(owner_id)
    for row in rows:
        if row.get("agent_id") == agent_id and row.get("status") == "active":
            return row.get("address")
    return None


async def send_reply(*, ctx: dict, body_text: str, body_html: str | None = None) -> str:
    """Send a threaded reply for a decoded ReplyContext ``ctx``.

    Returns "sent", "not_sent", "no_address", "disabled", or "error". Never raises.
    """
    try:
        if not settings.AGENT_EMAIL_DOMAIN.strip():
            return "disabled"

        from_addr = await resolve_from_address(ctx["owner_id"], ctx["agent_id"])
        if from_addr is None:
            return "no_address"

        in_reply_to = _header_safe(ctx.get("in_reply_to") or "")
        headers: dict[str, str] = {}
        if in_reply_to:
            headers["In-Reply-To"] = in_reply_to
            # RFC 5322 §3.6.4: References is the PARENT'S References plus the
            # parent's Message-ID — the whole ancestry, not just the parent.
            #
            # This used to be `headers["References"] = in_reply_to`, which
            # truncated the chain on every agent reply and let the thread root
            # drift: Friday sends A, owner replies (refs "A"), Friday replies
            # (refs "B" — A is gone), owner replies (refs "B C"). The root now
            # reads as B. Any thread identity derived from References[0] would
            # therefore split one conversation into several, and strict mail
            # clients fragment the thread.
            parent_refs = _header_safe(ctx.get("references") or "")
            headers["References"] = build_references(parent_refs, in_reply_to)

        # A first contact (no in_reply_to -- no thread to answer) is a brand
        # new email Friday is sending unprompted, not a reply -- prepending
        # "Re: " to it would be a lie about what it is.
        subject = ctx["subject"] if not in_reply_to else reply_subject(ctx["subject"])

        body_text = body_text + await _maybe_cold_wake_footer(ctx["owner_id"])

        message_id = await ses_email.send_ses_reply(
            from_addr=from_addr,
            to=ctx["reply_to"],
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            headers=(headers or None),
        )
        if message_id is None:
            logger.error(
                "send_reply: send_ses_reply returned None for agent_id=%s (SES send failed or agent email disabled)",
                ctx.get("agent_id"),
            )
            return "not_sent"
        return "sent"
    except Exception:
        logger.exception("send_reply failed for agent_id=%s", ctx.get("agent_id"))
        return "error"
