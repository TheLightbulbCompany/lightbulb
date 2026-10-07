"""The single outbound-email transport: AWS SES (sesv2), raw MIME.

Everything the platform sends leaves through ``send_ses_email``:

- agent replies, FROM a per-owner subdomain address
  (``friday@{slug}.isol8.co``) — via the ``send_ses_reply`` wrapper
- the app's own welcome / support-ticket mail, FROM ``hello@isol8.co`` /
  ``tickets@isol8.co`` — via ``email_service.send_email``, which adds the
  app-mail concerns (``[ENV]`` subject tagging) on top of this transport

Both work because the SES identity is the **apex domain** ``isol8.co``
(``ses-inbound-stack.ts`` — "verified for BOTH receiving and sending"), and
a domain identity authorizes every address beneath it. The backend task
role's grant is scoped to exactly that identity (``service-stack.ts``,
``AgentMailSesSend``).

Properties every send inherits:

- **Gated** — no-ops unless ``AGENT_EMAIL_DOMAIN`` is set. That setting
  holds the *verified SES domain itself* (CDK: ``env === "prod" ?
  "isol8.co" : ""``), so it is a direct proxy for "is the SES identity
  deployed" — the identity only exists where the prod-only
  ``SesInboundStack`` ran. Non-prod envs therefore send no mail at all.
  Clearing it in prod also silences welcome/support mail, not just agent
  replies; that coupling is deliberate (one verified domain, one switch)
  but is the thing to check first if app mail goes quiet.
- **Off the event loop** — boto3 is synchronous; the send runs in a worker
  thread with a hard timeout so a slow/hung SES call can't wedge the caller
  (an agent run, or the Clerk webhook).
- **Best-effort** — never raises. On failure it logs, emits a metric, and
  returns None. Callers (``agent_mail_reply.send_reply``) map None to
  "not_sent" and that must never be reported as "sent".

There is no send-level idempotency key (SES has no equivalent). The one
caller that needs it — the welcome email — is already protected upstream by
the Clerk webhook's svix-id dedupe, which short-circuits a retry *before*
reaching the send. See ``routers/webhooks.py``.
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
    directly, for the same reason every other AWS client in this package is lazy.
    """
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("sesv2", region_name=settings.AWS_REGION)
    return _ses_client


async def send_ses_email(
    *,
    from_addr: str,
    to: str | list[str],
    subject: str,
    body_text: str | None = None,
    body_html: str | None = None,
    headers: dict[str, str] | None = None,
    kind: str = "generic",
    tenant_name: str | None = None,
) -> str | None:
    """Send one email via AWS SES (sesv2 send_email, raw MIME).

    Builds an RFC-5322 message with stdlib email.message.EmailMessage so
    arbitrary headers (`In-Reply-To`/`References` threading, `Reply-To`) can
    be set, then sends the raw bytes. Returns the SES MessageId on success,
    or None on any failure (never raises — callers map None → "not_sent").
    boto3 is synchronous, so the send runs off the event loop with a timeout.

    Pass ``body_html`` alone for an HTML-only message, ``body_text`` alone for
    plain text, or both for a multipart/alternative. ``kind`` is a short label
    ("welcome", "support_ticket", "agent_reply_ses") used only for metric
    dimensions and log context.

    Every send is stamped with ``settings.AGENT_MAIL_CONFIGURATION_SET`` when
    that setting is non-empty (so the configuration set must already exist in
    the environment — SES rejects a send naming one that does not) and with
    ``TenantName`` when ``tenant_name`` is given.
    """
    if not settings.AGENT_EMAIL_DOMAIN.strip():
        # Emit a metric, not just a log line. The other two outcomes are
        # observable (result=ok / result=error) but this one used to be
        # silent, so an unconfigured env dropped every send with nothing in
        # CloudWatch to see it — and for support tickets the email IS the
        # only capture path, so a silent skip loses the request outright.
        put_metric("email.send", dimensions={"result": "skipped_unconfigured", "kind": kind})
        logger.warning("AGENT_EMAIL_DOMAIN not configured; skipping %s SES send to %s", kind, to)
        return None

    try:
        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = ", ".join(to) if isinstance(to, list) else to
        msg["Subject"] = subject
        for key, val in (headers or {}).items():
            msg[key] = val

        # EmailMessage has no "unset body" state, so the first part written
        # decides the top-level content type. HTML-only mail must therefore
        # go through set_content(subtype="html") — calling add_alternative on
        # an empty message would leave a stray empty text/plain part.
        if body_text is not None:
            msg.set_content(body_text)
            if body_html is not None:
                msg.add_alternative(body_html, subtype="html")
        elif body_html is not None:
            msg.set_content(body_html, subtype="html")

        raw = bytes(msg)

        send_kwargs: dict = {"Content": {"Raw": {"Data": raw}}}
        if settings.AGENT_MAIL_CONFIGURATION_SET.strip():
            send_kwargs["ConfigurationSetName"] = settings.AGENT_MAIL_CONFIGURATION_SET.strip()
        if tenant_name:
            send_kwargs["TenantName"] = tenant_name

        with anyio.fail_after(_SEND_TIMEOUT_S):
            # abandon_on_cancel=True so fail_after can actually return at the
            # deadline instead of waiting for a stalled worker thread.
            resp = await anyio.to_thread.run_sync(
                lambda: _client().send_email(**send_kwargs),
                abandon_on_cancel=True,
            )
        message_id = resp.get("MessageId") if isinstance(resp, dict) else None
        put_metric("email.send", dimensions={"result": "ok", "kind": kind})
        logger.info("sent %s email to %s id=%s", kind, to, message_id)
        return message_id
    except Exception:
        put_metric("email.send", dimensions={"result": "error", "kind": kind})
        logger.exception("send_ses_email failed for %s to %s", kind, to)
        return None


async def send_ses_reply(
    *,
    from_addr: str,
    to: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    headers: dict[str, str] | None = None,
    tenant_name: str | None = None,
) -> str | None:
    """Send an agent reply. Thin wrapper over send_ses_email that pins the
    metric kind — kept as its own name because ``agent_mail_reply`` treats a
    None return as the load-bearing "not_sent" signal."""
    return await send_ses_email(
        from_addr=from_addr,
        to=to,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        headers=headers,
        kind="agent_reply_ses",
        tenant_name=tenant_name,
    )
