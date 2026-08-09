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
worker receives the same value as ``PAPERCLIP_ISOL8_NOTIFY_TOKEN`` via the
fork's worker-env passthrough (set on the Paperclip task by paperclip-stack);
empty setting disables the route entirely. Events are deduped on the
plugin-minted ``eventId`` because a plugin-worker crash-restart re-subscribes
and can double-deliver.
"""

import asyncio
import hmac
import logging
import time

from fastapi import APIRouter, HTTPException, Request

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.push_service import send_push_to_user
from core.services.webhook_dedup import WebhookDedupResult, delete_event, record_event_or_skip

logger = logging.getLogger(__name__)

router = APIRouter()

# agent.run.finished pushes only for runs a human plausibly initiated or
# assigned; timer/automation heartbeats would be pure spam. Failures push
# regardless of source — a broken routine is worth knowing about — but are
# rate-limited below so a crashlooping routine can't page every cycle.
_FINISHED_PUSH_SOURCES = {"assignment", "on_demand"}

# ponytail: fixed hour-bucket cooldown per (company, agent) failure notice;
# upgrade to a sliding window if the bucket boundary double-push ever matters.
_FAILURE_COOLDOWN_S = 3600

# A thread interaction parks its issue until a human answers — the one event
# here that represents stalled work rather than finished work, so it is never
# rate-limited. Bodies mirror the web toast copy in `runToastReducer.ts`; keep
# the two in step. Keys are Paperclip's ISSUE_THREAD_INTERACTION_KINDS.
_INTERACTION_KIND_BODY = {
    "request_confirmation": "A task is paused waiting for your confirmation",
    "request_checkbox_confirmation": "A task is paused waiting for you to verify a checklist",
    "ask_user_questions": "An agent asked you a question and can't continue until you answer",
    "suggest_tasks": "An agent suggested follow-up tasks for your review",
}
_INTERACTION_FALLBACK_BODY = "A task is paused waiting for your input"

# Keys are Paperclip's APPROVAL_TYPES. Same intent as the interaction map: the
# payload knows what is being asked, so the push should say it.
_APPROVAL_TYPE_BODY = {
    "hire_agent": "An agent wants to hire a new agent",
    "approve_ceo_strategy": "An agent needs your sign-off on its strategy",
    "budget_override_required": "An agent needs a budget override approved",
    "request_board_approval": "An agent is requesting your approval",
}


def _payload_of(event: dict) -> dict:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def decide_push(event: dict) -> tuple[str, str] | None:
    """Map a plugin event to ``(title, body)``, or None for no push.

    Pure so it can be tested without HTTP. The agent is never named: the
    payload's agentId is a Paperclip agent id, which the push path can't
    resolve to a display name without a per-event Paperclip round-trip. What
    the copy CAN say precisely is what is being asked, since the payload
    carries its own discriminator (interactionKind, or an approval's type).
    """
    event_type = event.get("eventType")
    payload = _payload_of(event)
    if event_type == "issue.thread_interaction_created":
        # An interaction addressed to a specific agent is that agent's to
        # answer; only an unaddressed one is parked on a human.
        if payload.get("addresseeAgentId"):
            return None
        kind = payload.get("interactionKind")
        return ("A task needs you", _INTERACTION_KIND_BODY.get(kind, _INTERACTION_FALLBACK_BODY))
    if event_type in ("approval.created", "approval.resubmitted"):
        # Both actions carry details.type; fall back to the create/resubmit
        # distinction only when it is missing or is a type we don't know yet.
        body = _APPROVAL_TYPE_BODY.get(payload.get("type")) or (
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


def push_data(event: dict) -> dict[str, str]:
    """Identifiers for the push ``data`` payload (future tap-routing).

    Deliberately NO ``agentId`` key: the mobile client routes ``data.agentId``
    into the gateway agent chat, and a Paperclip agent UUID would land on a
    nonexistent chat. Teams-side routing keys ship under distinct names.
    """
    payload = _payload_of(event)
    data = {"source": "teams", "eventType": str(event.get("eventType") or "")}
    for value, key in (
        (event.get("entityId"), "entityId"),
        (event.get("companyId"), "companyId"),
        (payload.get("runId"), "runId"),
        (payload.get("issueId"), "issueId"),
    ):
        if value:
            data[key] = str(value)
    return data


def _failure_cooldown_key(event: dict) -> str | None:
    """Coarse rate limit for run-failure pushes: one per (company, agent) per
    hour bucket, keyed through the same dedup table (30-day TTL is fine for
    hour-bucketed rows)."""
    if event.get("eventType") != "agent.run.failed":
        return None
    agent_id = _payload_of(event).get("agentId") or "unknown"
    bucket = int(time.time() // _FAILURE_COOLDOWN_S)
    return f"pcplugin_failcool_{event.get('companyId')}_{agent_id}_{bucket}"


@router.post("/paperclip-events")
async def receive_paperclip_event(request: Request):
    secret = settings.PAPERCLIP_EVENTS_WEBHOOK_SECRET
    if not secret:
        raise HTTPException(status_code=404, detail="Not found")
    auth = request.headers.get("authorization", "")
    # Compare as bytes: compare_digest raises TypeError on non-ASCII *str*
    # input, and this is a public endpoint fed arbitrary header bytes.
    if not hmac.compare_digest(auth.encode(), f"Bearer {secret}".encode()):
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

    cooldown_key = _failure_cooldown_key(event)
    if cooldown_key is not None:
        if await record_event_or_skip(cooldown_key, source="paperclip-plugin") is WebhookDedupResult.ALREADY_SEEN:
            return {"ok": True, "pushed": False}

    # Dedupe AFTER the decision so non-push events never burn a dedup write.
    dedup_key = f"pcplugin_{event_id}"
    if await record_event_or_skip(dedup_key, source="paperclip-plugin") is WebhookDedupResult.ALREADY_SEEN:
        return {"ok": True, "pushed": False}

    title, body = decision
    try:
        repo = PaperclipRepo(table_name="paperclip-companies")
        user_ids = await repo.get_user_ids_for_company(company_id)
    except Exception:
        # Roll the dedup row back so the plugin-restart redelivery (the one
        # case eventId dedup exists for) can retry instead of hitting
        # ALREADY_SEEN. Mirrors the metronome webhook's failure handling.
        await delete_event(dedup_key)
        raise
    if not user_ids:
        put_metric("paperclip_events.no_audience")
        return {"ok": True, "pushed": False}

    # Concurrent fan-out; send_push_to_user contractually never raises.
    data = push_data(event)
    await asyncio.gather(*(send_push_to_user(user_id, title, body, data) for user_id in user_ids))
    put_metric(
        "paperclip_events.push",
        dimensions={"event_type": str(event.get("eventType"))},
    )
    return {"ok": True, "pushed": True}
