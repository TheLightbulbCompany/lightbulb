"""Build, enrich, and deliver a support ticket.

Identity + account/container state are derived server-side from the JWT +
repos — the client is never trusted for them. Delivered via the Plan-1
email_service to the support@ inbox, From a dedicated tickets@ identity so the
agent (Plan 3) can filter app tickets from direct customer mail. Best-effort:
returns the email id or None.
"""

import html
import json
import logging
from datetime import datetime, timedelta, timezone

from core.auth import AuthContext, get_owner_type, resolve_owner_id
from core.config import settings
from core.observability.logging import request_id_var
from core.repositories import billing_repo, container_repo
from core.services import cloudwatch_url, email_service
from schemas.support import SupportTicketRequest

logger = logging.getLogger(__name__)


def _posthog_person_url(user_id: str) -> str | None:
    if not settings.POSTHOG_PROJECT_ID:
        return None
    return f"https://us.posthog.com/project/{settings.POSTHOG_PROJECT_ID}/person/{user_id}"


def _admin_user_url(user_id: str) -> str:
    host = "admin.isol8.co" if settings.ENVIRONMENT == "prod" else f"admin.{settings.ENVIRONMENT or 'dev'}.isol8.co"
    return f"https://{host}/admin/users/{user_id}"


async def _build_metadata(auth: AuthContext, req: SupportTicketRequest) -> dict:
    owner_id = resolve_owner_id(auth)
    now = datetime.now(timezone.utc)
    container = await container_repo.get_by_owner_id(owner_id)
    billing = await billing_repo.get_by_owner_id(owner_id)

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
            "owner_type": get_owner_type(auth),
            "env": settings.ENVIRONMENT or "local",
        },
        "billing": {
            "subscription_status": (billing or {}).get("subscription_status"),
            "provider_choice": (billing or {}).get("provider_choice"),
            "byo_provider": (billing or {}).get("byo_provider"),
        },
        "container": {
            "status": (container or {}).get("status"),
            "substatus": (container or {}).get("substatus"),
            "service_name": (container or {}).get("service_name"),
            "last_error": (container or {}).get("last_error"),
            "last_provisioned_at": (container or {}).get("updated_at"),
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
    """Enrich + deliver a support ticket. Returns the Resend id or None."""
    meta = await _build_metadata(auth, req)
    subject = f"[Support] {req.category}: {req.message[:60]}"
    reply_to = auth.email or settings.EMAIL_SUPPORT_ADDRESS
    return await email_service.send_email(
        to=settings.EMAIL_SUPPORT_ADDRESS,
        subject=subject,
        html=_render(req, meta),
        from_addr=settings.EMAIL_FROM_TICKETS,
        reply_to=reply_to,
        kind="support_ticket",
    )
