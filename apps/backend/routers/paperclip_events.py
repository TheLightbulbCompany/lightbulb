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
from types import SimpleNamespace

import httpx
from fastapi import APIRouter, HTTPException, Request

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import activity, paperclip_escalation
from core.services import notification_copy as copy
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie
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

# The bounded vocabulary for the paperclip.run.failed{error_code} dimension.
# Paperclip's errorCode is free text as far as we are concerned, so anything
# outside this set lands on "other" rather than minting a new metric identity.
# P30/P31/W82 in observability-stack.ts bind three of these values literally.
_RUN_FAILED_CODES = frozenset(
    {
        "setup_failed",
        "process_lost",
        "timeout",
        "hermes_gateway_auth_failed",
        "hermes_gateway_upstream_error",
        "hermes_gateway_run_failed",
    }
)

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

    Pure so it can be tested without HTTP, so it never names the agent. For an
    approval this is only the FALLBACK: the handler replaces it with
    ``_approval_copy`` (one Paperclip round-trip naming the agent and the ask)
    and keeps this line when that lookup fails.
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
        code = payload.get("errorCode")
        return (
            "Team run failed",
            copy.run_failure_line(code if isinstance(code, str) else None, str(error) if error else None),
        )
    if event_type == "agent.run.finished":
        if payload.get("invocationSource") not in _FINISHED_PUSH_SOURCES:
            return None
        return ("Team run completed", "Finished a run")
    return None


# The approval lookup below never holds the push longer than this.
_APPROVAL_COPY_BUDGET_S = 3.0
_PUSH_TITLE_CHARS = 110
_PUSH_BODY_CHARS = 180


def approval_push_copy(item: "activity.ActivityItem") -> tuple[str, str]:
    """``("Growth · LB-12", "Needs your confirmation: Launch email")`` from the SAME
    rendering Activity shows, so the push, the web notification and the row agree
    (``approvalNotificationCopy`` in packages/shared is the TS twin)."""
    who = item.actor.name
    title = item.summary.title
    if title.startswith(f"{who}: "):
        what = f"Needs your approval: {title[len(who) + 2 :]}"
    elif title.startswith(f"{who} "):
        rest = title[len(who) + 1 :]
        what = rest[:1].upper() + rest[1:]
    else:
        what = f"Needs your approval: {title}"
    return activity._clip(copy.approval_title(who), _PUSH_TITLE_CHARS), copy.line(what)


async def _run_copy(event: dict, company_id: str) -> tuple[str, str] | None:
    """``("Pulse · ISOA-400", "Couldn't finish: …" / "Finished: <task>")`` for a team
    run, or None (Paperclip slow/down): the caller keeps the generic line."""
    payload = _payload_of(event)
    issue_id = payload.get("issueId")
    agent_id = payload.get("agentId")

    async def fetch() -> tuple[str, str]:
        async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=_APPROVAL_COPY_BUDGET_S) as http:
            admin = PaperclipAdminClient(http_client=http)
            cookie = await get_admin_session_cookie(http)
            agents, issue = await asyncio.gather(
                admin.list_agents(session_cookie=cookie, company_id=company_id),
                admin.get_issue(session_cookie=cookie, issue_id=str(issue_id)) if issue_id else asyncio.sleep(0),
            )
        if not isinstance(issue, dict) or issue.get("companyId") != company_id:
            issue = {}
        who = _agent_names(agents).get(agent_id) or "An agent"
        head = copy.title(who, issue.get("identifier") if isinstance(issue.get("identifier"), str) else None)
        if event.get("eventType") == "agent.run.failed":
            code, error = payload.get("errorCode"), payload.get("error")
            return head, copy.run_failure_line(code if isinstance(code, str) else None, str(error) if error else None)
        task = issue.get("title") if isinstance(issue.get("title"), str) else None
        return head, copy.line(f"Finished: {task}") if task else "Finished a run"

    try:
        return await asyncio.wait_for(fetch(), _APPROVAL_COPY_BUDGET_S)
    except Exception as exc:  # noqa: BLE001 -- the generic push beats no push
        logger.warning("paperclip_events: run copy unavailable error_type=%s", type(exc).__name__)
        return None


def _agent_names(agents) -> dict[str, str]:
    return {
        a["id"]: a.get("name") or "An agent" for a in activity._rows(agents, "agents") if isinstance(a.get("id"), str)
    }


# No viewer here: a human requester renders as "A teammate", never as "You".
_NO_VIEWER = SimpleNamespace(paperclip_user_id=None, user_id=None)


async def _approval_copy(event: dict, company_id: str) -> tuple[str, str] | None:
    """The specific copy for one approval or thread interaction ("<Agent> needs your
    confirmation: <task>"), or None (Paperclip slow/down, wrong company): the caller
    then falls back to the generic per-type line and still pushes."""
    entity_id = str(event.get("entityId") or "")
    interaction_id = _payload_of(event).get("interactionId")
    is_interaction = event.get("eventType") == "issue.thread_interaction_created"

    async def fetch() -> tuple[str, str] | None:
        async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=_APPROVAL_COPY_BUDGET_S) as http:
            admin = PaperclipAdminClient(http_client=http)
            cookie = await get_admin_session_cookie(http)
            if is_interaction:
                issue, rows, agents = await asyncio.gather(
                    admin.get_issue(session_cookie=cookie, issue_id=entity_id),
                    admin.list_issue_interactions(session_cookie=cookie, issue_id=entity_id),
                    admin.list_agents(session_cookie=cookie, company_id=company_id),
                )
            else:
                approval, agents = await asyncio.gather(
                    admin.get_approval(session_cookie=cookie, approval_id=entity_id),
                    admin.list_agents(session_cookie=cookie, company_id=company_id),
                )
        if is_interaction:
            row = next((r for r in activity._rows(rows, "interactions") if r.get("id") == interaction_id), None)
            if not isinstance(issue, dict) or issue.get("companyId") != company_id or row is None:
                return None
            item = activity.render_interaction(row, issue, agent_names=_agent_names(agents), ctx=_NO_VIEWER)
            return approval_push_copy(item)
        if not isinstance(approval, dict) or approval.get("companyId") != company_id:
            return None
        return approval_push_copy(activity.render_approval(approval, agent_names=_agent_names(agents), ctx=_NO_VIEWER))

    try:
        return await asyncio.wait_for(fetch(), _APPROVAL_COPY_BUDGET_S)
    except Exception as exc:  # noqa: BLE001 -- the generic push beats no push
        logger.warning("paperclip_events: approval copy unavailable error_type=%s", type(exc).__name__)
        return None


def push_data(event: dict) -> dict[str, str]:
    """Identifiers for the push ``data`` payload (future tap-routing).

    Deliberately NO ``agentId`` key: the mobile client routes ``data.agentId``
    into the gateway agent chat, and a Paperclip agent UUID would land on a
    nonexistent chat. Teams-side routing keys ship under distinct names.
    """
    payload = _payload_of(event)
    data = {"source": "teams", "eventType": str(event.get("eventType") or "")}
    if str(event.get("eventType") or "").startswith("approval.") and event.get("entityId"):
        # Tap-routing key: the phone opens Activity focused on this approval.
        data["approvalId"] = str(event["entityId"])
    if event.get("eventType") == "issue.thread_interaction_created" and payload.get("interactionId"):
        # Same for an agent's question: Activity focused on it (entityId is its issue).
        data["interactionId"] = str(payload["interactionId"])
        data["issueId"] = str(event.get("entityId") or "")
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

    # Count EVERY run failure before the push decision: the cooldown and
    # dedupe below exist to spare phones, and they swallowed the 2026-09-23
    # outage (15 h of setup_failed runs, no alarm). The alarms need the raw rate.
    if event.get("eventType") == "agent.run.failed":
        code = _payload_of(event).get("errorCode")
        put_metric(
            "paperclip.run.failed",
            dimensions={"error_code": code if code in _RUN_FAILED_CODES else "other"},
        )
        # A platform-failed issue run may leave the issue recovery-blocked; look again
        # later and hand it on (background task — this handler still returns now).
        paperclip_escalation.schedule(event)

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

    if event.get("eventType") in (
        "approval.created",
        "approval.resubmitted",
        "issue.thread_interaction_created",
    ) and event.get("entityId"):
        specific = await _approval_copy(event, str(company_id))
        if specific is not None:
            title, body = specific
    elif event.get("eventType") in ("agent.run.failed", "agent.run.finished"):
        specific = await _run_copy(event, str(company_id))
        if specific is not None:
            title, body = specific

    # Concurrent fan-out; send_push_to_user contractually never raises.
    data = push_data(event)
    await asyncio.gather(*(send_push_to_user(user_id, title, body, data) for user_id in user_ids))
    put_metric(
        "paperclip_events.push",
        dimensions={"event_type": str(event.get("eventType"))},
    )
    return {"ok": True, "pushed": True}
