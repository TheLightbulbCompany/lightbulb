"""Turn a decoded reply-context into a sent, threaded reply email.

Consumes the ``agent-email-addresses`` address book (via
``agent_email_repo``) to resolve which of an owner's addresses belongs to
the replying agent, and the single outbound-email seam (``email_service``)
to actually send. Both are imported as modules (not `from ... import ...`)
so tests can monkeypatch their attributes without needing to patch this
module's own names.

``send_reply`` never raises — a reply failure must never take down the
inbound-mail handler that called it. It returns a short result string the
caller can log/metric on instead.
"""

import html as _html
import logging

from core.config import settings
from core.repositories import agent_email_repo
from core.services import email_service

logger = logging.getLogger(__name__)


def _header_safe(value: str) -> str:
    """Strip CR/LF and other control characters from a value bound for an email
    header. The value (a Message-ID from the original inbound mail) is
    sender-controlled, so we sanitize before it reaches the provider — defense
    in depth against header injection regardless of the provider's own handling."""
    return "".join(ch for ch in value if ch == "\t" or (ch >= " " and ch != "\x7f")).strip()


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

    Returns "sent", "no_address", "disabled", or "error". Never raises.
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
            headers["References"] = in_reply_to

        html_body = body_html if body_html is not None else f"<pre>{_html.escape(body_text)}</pre>"

        await email_service.send_email(
            to=ctx["reply_to"],
            subject=reply_subject(ctx["subject"]),
            html=html_body,
            text=body_text,
            from_addr=from_addr,
            headers=(headers or None),
            kind="agent_reply",
        )
        return "sent"
    except Exception:
        logger.exception("send_reply failed for agent_id=%s", ctx.get("agent_id"))
        return "error"
