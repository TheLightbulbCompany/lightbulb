"""Outbound-email seam over AWS SES (sesv2) for agent replies.

An agent's reply must be sent FROM its own subdomain address
(``friday@{slug}.isol8.co``). Resend cannot originate mail from an
arbitrary per-owner subdomain, so agent replies go out via SES while the
app's own welcome/support mail stays on ``email_service`` (Resend) — see
that module's docstring for the seam it owns.

This mirrors ``email_service.send_email``'s shape:

- **Gated** — no-ops unless ``AGENT_EMAIL_DOMAIN`` is configured, so an env
  that hasn't turned on agent email never attempts a send.
- **Off the event loop** — boto3 is synchronous; the send runs in a worker
  thread with a hard timeout so a slow/hung SES call can't wedge the caller.
- **Best-effort** — never raises. On failure it logs, emits a metric, and
  returns None. Callers (``agent_mail_reply.send_reply``) map None to
  "not_sent" and that must never be reported as "sent".

IAM note: the backend ECS task role needs ``ses:SendEmail`` /
``ses:SendRawEmail`` granted in service-stack (CDK) — a separate infra
chunk, not done here.
"""

import logging
from email.message import EmailMessage

import anyio
import boto3

from core.config import settings
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

# Hard ceiling so a hung SES request cannot block the calling coroutine.
# The worker thread may keep running after this fires — acceptable for
# best-effort mail, matching email_service's timeout posture.
_SEND_TIMEOUT_S = 10

_ses_client = None


def _client():
    """Lazy boto3 SESv2 client — deferred so import-time works without creds.

    A plain module-level function (not a class) so tests can monkeypatch it
    directly, mirroring the `_client()` pattern in core/crypto/kms_secrets.py.
    """
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("sesv2", region_name=settings.AWS_REGION)
    return _ses_client


async def send_ses_reply(
    *,
    from_addr: str,
    to: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    headers: dict[str, str] | None = None,
) -> str | None:
    """Send an agent reply via AWS SES (sesv2 send_email, raw MIME).

    Builds an RFC-5322 message with stdlib email.message.EmailMessage so
    `In-Reply-To`/`References` threading headers can be set, then sends the
    raw bytes. Returns the SES MessageId on success, or None on any failure
    (never raises — the caller maps None → "not_sent"). boto3 is synchronous,
    so the send runs off the event loop with a timeout, mirroring email_service.
    """
    if not settings.AGENT_EMAIL_DOMAIN.strip():
        logger.debug("AGENT_EMAIL_DOMAIN not configured; skipping SES send to %s", to)
        return None

    try:
        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = to
        msg["Subject"] = subject
        for key, val in (headers or {}).items():
            msg[key] = val
        msg.set_content(body_text)
        if body_html:
            msg.add_alternative(body_html, subtype="html")

        raw = bytes(msg)

        with anyio.fail_after(_SEND_TIMEOUT_S):
            # abandon_on_cancel=True so fail_after can actually return at the
            # deadline instead of waiting for a stalled worker thread — same
            # reasoning as email_service.send_email.
            resp = await anyio.to_thread.run_sync(
                lambda: _client().send_email(Content={"Raw": {"Data": raw}}),
                abandon_on_cancel=True,
            )
        message_id = resp.get("MessageId") if isinstance(resp, dict) else None
        put_metric("email.send", dimensions={"result": "ok", "kind": "agent_reply_ses"})
        logger.info("sent agent_reply_ses email to %s id=%s", to, message_id)
        return message_id
    except Exception:
        put_metric("email.send", dimensions={"result": "error", "kind": "agent_reply_ses"})
        logger.exception("send_ses_reply failed to %s", to)
        return None
