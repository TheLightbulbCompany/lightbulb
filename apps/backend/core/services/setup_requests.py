"""The founding-teams door: store a guided-setup request, email Aden, emit
``setup_requested``.

The row is the record. The email and the PostHog event are best-effort and
never fail a submission (both helpers already swallow their own errors).
"""

from __future__ import annotations

import html
import logging
import uuid

from core.config import settings
from core.dynamodb import utc_now_iso
from core.observability.product_analytics import capture_product_event
from core.repositories import setup_requests_repo
from core.services import email_service
from schemas.setup_request import SetupRequest

logger = logging.getLogger(__name__)

# Aden replies to setup requests from his own address within a business day
# (founder decision F3), so they go to him, not the support queue.
SETUP_REQUEST_TO = "aden@thelightbulb.company"


async def submit(req: SetupRequest) -> dict:
    request_id = str(uuid.uuid4())
    item = {
        "id": request_id,
        "submitted_at": utc_now_iso(),
        **req.model_dump(),
    }
    await setup_requests_repo.put(item)

    capture_product_event(
        distinct_id=req.anonymous_id or f"setup_request:{request_id}",
        event="setup_requested",
        event_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"setup_requested:{request_id}")),
        properties={
            "team_size": req.team_size,
            "agents": req.agents,
            "source": req.source,
            "source_other": req.source_other or None,
            "source_param": req.source_param or None,
            "headline_variant": req.headline_variant or None,
            **{k: v for k, v in req.attribution.items() if v},
        },
    )
    await _email_aden(request_id, req)
    logger.info("setup request %s stored (team_size=%s source=%s)", request_id, req.team_size, req.source)
    return {"id": request_id}


async def _email_aden(request_id: str, req: SetupRequest) -> None:
    rows = [
        ("Name", req.name),
        ("Email", req.email),
        ("Company", req.company),
        ("Team size", req.team_size),
        ("Agents", ", ".join(req.agents) + (f" ({req.agents_other})" if req.agents_other else "")),
        ("First handoff", req.first_handoff),
        ("Heard about us", req.source + (f" ({req.source_other})" if req.source_other else "")),
        ("Thread", req.thread_link),
        ("?source=", req.source_param),
        *sorted(req.attribution.items()),
        ("Request id", request_id),
    ]
    rows = [(k, v) for k, v in rows if v]
    body_html = "".join(f"<p><strong>{html.escape(k)}:</strong> {html.escape(v, quote=False)}</p>" for k, v in rows)
    body_text = "".join(f"{k}: {v}\n" for k, v in rows)
    await email_service.send_email(
        to=SETUP_REQUEST_TO,
        subject=f"Setup request: {req.company} ({req.team_size})",
        html=f"<div>{body_html}</div>",
        text=body_text,
        from_addr=settings.EMAIL_FROM_TICKETS,
        reply_to=req.email,
        kind="setup_request",
    )
