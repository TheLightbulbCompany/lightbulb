"""Inbound provisioned-agent email: sender parsing, loop guard, body fetch.

Resend's inbound webhook payload is a thin envelope (sender, subject, an
`email_id`) — the actual body/headers live behind Resend's receiving REST
API and must be fetched separately (`fetch_received_email`). This module is
the "make it agent-shaped" step of that pipeline: normalize the `From`
address, guard against autoresponder loops (an agent replying to a bounce
or a mailing-list digest would otherwise re-trigger itself indefinitely),
fetch the full message, and format the string handed to `/hooks/agent`.
"""

import logging
from email.utils import parseaddr
from typing import Any

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

RESEND_RECEIVING_URL = "https://api.resend.com/emails/receiving/{email_id}"

# Header-based loop guard: values that mark a message as machine-generated
# (autoresponders, mailing lists, bulk senders) rather than a human reply.
# Case-insensitive on both header name and value — mail clients/relays are
# inconsistent about casing on both.
_AUTO_SUBMITTED_HEADER = "auto-submitted"
_PRECEDENCE_HEADER = "precedence"
_PRECEDENCE_BLOCK_VALUES = {"bulk", "list", "junk", "auto_reply"}

# Sender-authentication verdict header: values are provider-formatted (e.g.
# `"mx.google.com; dmarc=pass header.from=x.com"`), so we match on the
# substring token rather than parsing full RFC 8601-style Auth-Results ABNF.
_AUTHENTICATION_RESULTS_HEADER = "authentication-results"


def parse_from_address(raw_from: str) -> str:
    """Extract a bare, lowercased, trimmed email address from a `From` header.

    Handles both `"Acme <a@b.com>"` and bare `"a@b.com"` forms via
    `email.utils.parseaddr`.
    """
    _, addr = parseaddr(raw_from)
    return addr.strip().lower()


def is_auto_submitted(headers: dict[str, str]) -> bool:
    """Loop guard: True if this message looks machine-generated.

    True when the `Auto-Submitted` header is present and not `"no"`, OR the
    `Precedence` header is one of bulk/list/junk/auto_reply. Both header
    keys and values are matched case-insensitively.
    """
    lowered = {k.lower(): v for k, v in headers.items()}

    auto_submitted = lowered.get(_AUTO_SUBMITTED_HEADER)
    if auto_submitted is not None and auto_submitted.strip().lower() != "no":
        return True

    precedence = lowered.get(_PRECEDENCE_HEADER)
    if precedence is not None and precedence.strip().lower() in _PRECEDENCE_BLOCK_VALUES:
        return True

    return False


def passes_sender_authentication(headers: dict) -> bool:
    """True iff the fetched email's headers show it passed sender authentication.

    Looks for the `Authentication-Results` header (case-insensitive key) and
    accepts the mail if DMARC passed, or (absent a DMARC verdict) if either SPF
    or DKIM passed. Absent/uninterpretable → False (fail-closed): the From header
    is forgeable, so we do not trust an unauthenticated sender even when it
    matches the allowlist.

    DMARC is authoritative when present: an explicit `dmarc=fail` (or any
    other non-`pass` dmarc verdict) is NOT rescued by a passing SPF/DKIM
    token elsewhere in the same header — the spf/dkim fallback only applies
    when there is no `dmarc=` token in the header at all.
    """
    if not isinstance(headers, dict):
        return False

    lowered = {k.lower(): v for k, v in headers.items()}
    value = lowered.get(_AUTHENTICATION_RESULTS_HEADER)
    if not isinstance(value, str):
        return False

    value = value.lower()
    if "dmarc=" in value:
        return "dmarc=pass" in value

    return "spf=pass" in value or "dkim=pass" in value


def _client_factory() -> httpx.AsyncClient:
    """Isolated so tests can inject a `httpx.MockTransport`-backed client."""
    return httpx.AsyncClient(timeout=10)


async def fetch_received_email(email_id: str) -> dict[str, Any] | None:
    """Fetch the full received-email payload (text/html/headers/…) from Resend.

    Never raises — network errors, non-200 responses, and malformed JSON
    all resolve to None so callers can treat "couldn't fetch" uniformly.
    """
    url = RESEND_RECEIVING_URL.format(email_id=email_id)
    headers = {"Authorization": f"Bearer {settings.RESEND_API_KEY}"}
    try:
        async with _client_factory() as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.warning(
                "resend_receiving_fetch_non_200",
                extra={"email_id": email_id, "status_code": resp.status_code},
            )
            return None
        return resp.json()
    except Exception:  # noqa: BLE001 — any transport/parse error → None, never raise
        logger.exception("resend_receiving_fetch_failed", extra={"email_id": email_id})
        return None


def build_delivered_message(*, from_addr: str, subject: str, body_text: str, reply_token: str) -> str:
    """Format the string delivered to the agent via `/hooks/agent`.

    A header block (sender/subject/reply-token) comes first so the agent
    can see how to reply before reading the body, followed by a separator
    and the raw body text.
    """
    return (
        "You received an email.\n"
        "\n"
        f"From: {from_addr}\n"
        f"Subject: {subject}\n"
        f"Reply-Token: {reply_token}\n"
        "\n"
        "To reply, use the isol8-mail tool with the Reply-Token above.\n"
        "\n"
        "---\n"
        f"{body_text}"
    )
