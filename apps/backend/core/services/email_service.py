"""Single outbound-email seam over the Resend SDK.

Everything that sends app-generated mail (welcome email; Plan 2 support
ticket delivery) goes through send_email. It is:

- **Gated** — no-ops unless a real Resend key (re_…) is configured, so an env
  still holding the CDK-generated placeholder secret never 403s. Presence of a
  real key is the only switch.
- **Off the event loop** — the Resend SDK is synchronous; we run it in a
  worker thread with a hard timeout so a slow send can't wedge the caller
  (e.g. the Clerk webhook).
- **Best-effort** — it never raises. On failure it logs, emits a metric, and
  returns None. Callers that care can branch on the return value.
"""

import logging

import anyio
import resend

from core.config import settings
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

# Hard ceiling so a hung Resend request cannot block the calling coroutine
# (e.g. the webhook handler). The worker thread may keep running after this
# fires — acceptable for best-effort mail.
_SEND_TIMEOUT_S = 10


async def send_email(
    *,
    to: str | list[str],
    subject: str,
    html: str,
    from_addr: str,
    reply_to: str | list[str] | None = None,
    text: str | None = None,
    idempotency_key: str | None = None,
    tags: list[dict[str, str]] | None = None,
    kind: str = "generic",
) -> str | None:
    """Send one email via Resend. Returns the Resend email id, or None when
    no real Resend key is configured / on failure. Never raises.

    kind is a short label ("welcome", "support_ticket") used only for metric
    dimensions and log context.
    """
    # Real Resend keys are re_-prefixed; this also skips the random placeholder
    # the CDK secret holds until an operator sets the real key, so we never 403
    # against it. Presence of a real key is the only switch — no enable flag.
    if not settings.RESEND_API_KEY.startswith("re_"):
        logger.debug("no real Resend key configured; skipping %s send to %s", kind, to)
        return None

    # Tag non-prod mail so dev/staging test sends are obvious in the inbox
    # (e.g. "[DEV] Welcome to Isol8"). Prod sends are unprefixed.
    env = settings.ENVIRONMENT.strip()
    if env and env.lower() not in ("prod", "production"):
        subject = f"[{env.upper()}] {subject}"

    resend.api_key = settings.RESEND_API_KEY

    params: resend.Emails.SendParams = {
        "from": from_addr,
        "to": to,
        "subject": subject,
        "html": html,
    }
    if text is not None:
        params["text"] = text
    if reply_to is not None:
        params["reply_to"] = reply_to
    if tags is not None:
        params["tags"] = tags

    options = {"idempotency_key": idempotency_key} if idempotency_key else None

    try:
        with anyio.fail_after(_SEND_TIMEOUT_S):
            # abandon_on_cancel=True so fail_after can actually return at the
            # deadline. anyio shields a default run_sync from cancellation and
            # would otherwise wait for the stalled thread, blocking the caller
            # (the Clerk webhook) past the timeout. The orphaned thread
            # finishing late is fine for best-effort mail.
            resp = await anyio.to_thread.run_sync(lambda: resend.Emails.send(params, options), abandon_on_cancel=True)
        email_id = resp.get("id") if isinstance(resp, dict) else getattr(resp, "id", None)
        put_metric("email.send", dimensions={"result": "ok", "kind": kind})
        logger.info("sent %s email to %s id=%s", kind, to, email_id)
        return email_id
    except Exception:
        put_metric("email.send", dimensions={"result": "error", "kind": kind})
        logger.exception("send_email failed for %s to %s", kind, to)
        return None
