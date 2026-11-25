"""Build, enrich, and deliver a support ticket.

Identity is derived server-side from the JWT — the client is never trusted for it. Delivered via email_service to
the support@ inbox, From a dedicated tickets@ identity so the agent can
filter app tickets from direct customer mail. Best-effort: returns the email
id or None.
"""

import html
import json
import logging
from datetime import datetime, timedelta, timezone

from core.auth import AuthContext, resolve_owner_id
from core.config import settings
from core.observability.logging import request_id_var
from core.services import cloudwatch_url, email_service
from schemas.support import SupportTicketRequest

logger = logging.getLogger(__name__)


def _posthog_person_url(user_id: str) -> str | None:
    if not settings.POSTHOG_PROJECT_ID:
        return None
    return f"https://us.posthog.com/project/{settings.POSTHOG_PROJECT_ID}/person/{user_id}"


def _admin_user_url(user_id: str) -> str:
    # The admin surface is always `admin.` in front of the frontend host, so
    # prod (`isol8.co`) gives `admin.isol8.co` and dev (`dev.isol8.co`) gives
    # `admin.dev.isol8.co` — the allowlist in apps/frontend/src/middleware.ts.
    return f"https://admin.{settings.frontend_host}/admin/users/{user_id}"


async def _build_metadata(auth: AuthContext, req: SupportTicketRequest) -> dict:
    owner_id = resolve_owner_id(auth)
    now = datetime.now(timezone.utc)

    cloudwatch = cloudwatch_url.build_insights_url(
        user_id=auth.user_id,
        start=(now - timedelta(hours=6)).isoformat(),
        end=now.isoformat(),
        level="ERROR",
    )

    request_ids = list(req.recent_request_ids)
    current = request_id_var.get()
    if current and current not in request_ids:
        request_ids.insert(0, current)

    return {
        "identity": {
            "user_id": auth.user_id,
            "email": auth.email,
            "owner_id": owner_id,
            "env": settings.ENVIRONMENT or "local",
        },
        "client": {
            "route": req.route,
            "user_agent": req.user_agent,
            "app_sha": req.app_sha,
        },
        "error": ({"message": req.error_message, "stack": req.error_stack} if req.error_message else None),
        "links": {
            "posthog_person": _posthog_person_url(auth.user_id),
            "cloudwatch_logs": cloudwatch,
            "admin_user": _admin_user_url(auth.user_id),
        },
        "request_ids": request_ids,
        "submitted_at": now.isoformat(),
    }


def _render(req: SupportTicketRequest, meta: dict) -> str:
    safe_msg = html.escape(req.message)
    safe_cat = html.escape(req.category)
    blob = html.escape(json.dumps(meta, indent=2, default=str))
    links = meta["links"]
    link_rows = "".join(
        f'<li><a href="{html.escape(url)}">{name}</a></li>'
        for name, url in (
            ("PostHog person", links["posthog_person"]),
            ("CloudWatch logs", links["cloudwatch_logs"]),
            ("Admin", links["admin_user"]),
        )
        if url
    )
    from_label = html.escape(meta["identity"].get("email") or meta["identity"]["user_id"])
    env_label = html.escape(meta["identity"]["env"])
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:15px;line-height:1.5;color:#1a1a1a">
  <p><strong>Category:</strong> {safe_cat}</p>
  <p><strong>From:</strong> {from_label} ({env_label})</p>
  <p><strong>Message:</strong></p>
  <blockquote style="border-left:3px solid #ddd;padding-left:12px;color:#333;white-space:pre-wrap">{safe_msg}</blockquote>
  <p><strong>Debug links:</strong></p>
  <ul>{link_rows}</ul>
  <p><strong>Metadata:</strong></p>
  <pre style="background:#f6f6f6;padding:12px;border-radius:6px;overflow:auto"><code>```json
{blob}
```</code></pre>
</div>"""


async def submit_ticket(auth: AuthContext, req: SupportTicketRequest) -> str | None:
    """Enrich + deliver a support ticket. Returns the RFC Message-ID or None."""
    meta = await _build_metadata(auth, req)
    subject = f"[Support] {req.category}: {req.message[:60]}"
    reply_to = auth.email or settings.EMAIL_SUPPORT_ADDRESS
    email_id = await email_service.send_email(
        to=settings.EMAIL_SUPPORT_ADDRESS,
        subject=subject,
        html=_render(req, meta),
        from_addr=settings.EMAIL_FROM_TICKETS,
        reply_to=reply_to,
        kind="support_ticket",
    )
    if email_id is None:
        # The email is the ONLY capture path — there is no ticket table by
        # design — so a failed send would otherwise discard what the owner
        # asked for, leaving support a user id and a timestamp. Log enough to
        # act on: who, what category, and the message. Bounded to 2000 chars
        # (of the schema's 8000) so a pasted stack trace can't flood the group,
        # and the reply-to address so support can answer without the email.
        logger.warning(
            "support ticket UNDELIVERED — reply_to=%s category=%s message=%r",
            reply_to,
            req.category,
            req.message[:2000],
        )
    return email_id
