"""Inbound event receiver for the Paperclip ``isol8.notifications`` plugin.

The plugin (fork: ``packages/plugins/plugin-isol8-notifications``) runs inside
the Paperclip server, subscribes to approval + run-lifecycle domain events,
and POSTs each one here. This router decides which events become push
notifications and fans them out to every active member of the company via the
existing Expo push path.

Delivery contract: fire-and-forget on BOTH sides by design. The plugin has no
retry; this handler acks 200 even when a push fails. Intermittent loss is
accepted (explicit product decision) — there is deliberately no backstop poll.

Auth: a shared bearer secret (``PAPERCLIP_EVENTS_WEBHOOK_SECRET``). The plugin
carries it in ``configJson`` (set by scripts/configure_paperclip_notify_plugin
.py); empty setting disables the route entirely. Events are deduped on the
plugin-minted ``eventId`` because a plugin-worker crash-restart re-subscribes
and can double-deliver.
"""

import hmac
import logging

from fastapi import APIRouter, HTTPException, Request

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.push_service import send_push_to_user
from core.services.webhook_dedup import WebhookDedupResult, record_event_or_skip

logger = logging.getLogger(__name__)

router = APIRouter()

# agent.run.finished pushes only for runs a human plausibly initiated or
# assigned; timer/automation heartbeats would be pure spam. Failures push
# regardless of source — a broken routine is worth knowing about.
_FINISHED_PUSH_SOURCES = {"assignment", "on_demand"}


def decide_push(event: dict) -> tuple[str, str] | None:
    """Map a plugin event to ``(title, body)``, or None for no push.

    Pure so it can be tested without HTTP. Copy stays generic: the payload's
    agentId is a Paperclip agent id, which the push path can't resolve to a
    display name without a per-event Paperclip round-trip.
    """
    event_type = event.get("eventType")
    payload = event.get("payload") or {}
    if event_type in ("approval.created", "approval.resubmitted"):
        body = (
            "An agent resubmitted work for your approval"
            if event_type == "approval.resubmitted"
            else "An agent submitted work for your approval"
        )
        return ("Awaiting your approval", body)
    if event_type == "agent.run.failed":
        error = payload.get("error")
        body = str(error)[:120] if error else "A team agent run failed"
        return ("Team run failed", body)
    if event_type == "agent.run.finished":
        if payload.get("invocationSource") not in _FINISHED_PUSH_SOURCES:
            return None
        return ("Team run completed", "A team agent finished a run")
    return None


@router.post("/paperclip-events")
async def receive_paperclip_event(request: Request):
    secret = settings.PAPERCLIP_EVENTS_WEBHOOK_SECRET
    if not secret:
        raise HTTPException(status_code=404, detail="Not found")
    auth = request.headers.get("authorization", "")
    if not hmac.compare_digest(auth, f"Bearer {secret}"):
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        event = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Invalid event")
    event_id = event.get("eventId")
    company_id = event.get("companyId")
    if not event_id or not company_id:
        raise HTTPException(status_code=400, detail="Missing eventId/companyId")

    decision = decide_push(event)
    if decision is None:
        return {"ok": True, "pushed": False}

    # Dedupe AFTER the decision so non-push events never burn a dedup write.
    result = await record_event_or_skip(f"pcplugin_{event_id}", source="paperclip-plugin")
    if result is WebhookDedupResult.ALREADY_SEEN:
        return {"ok": True, "pushed": False}

    title, body = decision
    repo = PaperclipRepo(table_name="paperclip-companies")
    user_ids = await repo.get_user_ids_for_company(company_id)
    if not user_ids:
        put_metric("paperclip_events.no_audience")
        return {"ok": True, "pushed": False}

    pushed = 0
    for user_id in user_ids:
        try:
            await send_push_to_user(user_id, title, body, {})
            pushed += 1
        except Exception:
            # One member's push failure must not block the rest; loss is
            # accepted by contract.
            logger.exception("paperclip-events push failed for %s", user_id)
    put_metric(
        "paperclip_events.push",
        dimensions={"event_type": str(event.get("eventType"))},
    )
    return {"ok": True, "pushed": pushed > 0}
