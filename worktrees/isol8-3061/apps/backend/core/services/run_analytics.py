"""One server-side PostHog event per completed agent run: ``agent_run_completed``.

Emitted from the run-completion callers (the room chat path's
``handle_run_event``, the operator relay tap, and the usage callback for
non-room runs) — the one signal billing already trusts for EVERY session
type (webchat, channel, cron, Paperclip issue). It
carries the owner, initiation type (autonomous vs human), trigger provider and
outcome, powering the activation gate, the North Star (successful autonomous
runs / active owner / week) and retention cohorts.

Session-key classification is synchronous; everything with I/O (the Paperclip
enrichment join, the internal-owner lookup, the capture itself) runs in a
spawned task the caller tracks and cancels on connection close (best-effort —
an emit lost at teardown is the same accepted loss class as backend-down). The whole path is
best-effort by contract: no failure here may ever raise into or slow the run
path. On enrichment failure the event is still emitted, degraded
(``initiation_type: "unknown"``, ``enrichment_failed: true``) — the event
COUNT stays truthful; only properties degrade.

Dedup stance: effectively-once with an at-least-once floor. OpenClaw's
``lifecycleEnded`` guard means one terminal per run; the bounded runId FIFO
below is the in-process belt, and the deterministic capture uuid (uuid5 of the
runId) lets PostHog ingestion drop exact duplicates across backend restarts.
Runs terminating while the backend is down are lost — bounded, accepted for
analytics.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import httpx

from core.auth import is_org_owner_id, is_platform_admin_email
from core.config import settings
from core.observability.metrics import put_metric
from core.observability.product_analytics import capture_product_event
from core.services import clerk_admin
from core.services.agent_routine_provisioning import NIGHTLY_CEO_ROUTINE_TITLE, NIGHTLY_CEO_SENTINEL
from core.services.inbound_triggers import automation_from_title
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger(__name__)

EVENT_NAME = "agent_run_completed"

# Namespace for the deterministic capture uuid — uuid5(namespace, owner:runId)
# is stable across restarts, so PostHog ingestion can drop exact re-emits.
# DO NOT RENAME the "isol8:" seed: it fixes the capture uuid PostHog dedupes on;
# changing it re-emits every already-ingested run as a new event.
_CAPTURE_UUID_NS = uuid.uuid5(uuid.NAMESPACE_URL, "isol8:agent_run_completed")

# North Star numerator sources. Everything else classified is human-initiated;
# only an unresolved source stays "unknown".
_AUTONOMOUS_SOURCES = frozenset({"routine_schedule", "routine_webhook", "cron"})

_ROUTINE_RUN_SOURCES = frozenset({"schedule", "webhook", "manual", "api"})

# Total budget for the Paperclip enrichment join (admin cookie + up to three
# GETs). One attempt, no retries — over budget the event emits degraded.
_ENRICH_TIMEOUT_S = 5.0

# Bounded FIFO set of already-emitted owner:runId pairs (same idiom as
# product_analytics._daily_dedup_seen). Owner-scoped because runId is
# supplied by the owner's own container — one owner's colliding id must not
# suppress another owner's event.
_SEEN_RUN_IDS_CAP = 4096
_seen_run_ids: dict[str, None] = {}

# owner_id -> (is_internal, monotonic stamp). Bounded, 1h TTL — one Clerk
# email resolution per owner per hour instead of one per run.
_INTERNAL_TTL_S = 3600.0
_INTERNAL_CACHE_CAP = 4096
_internal_cache: dict[str, tuple[bool, float]] = {}


def _coarse_source(source: str) -> str:
    """Coarse run origin for the CloudWatch ``run.completed`` metric.

    The fine-grained routine-schedule / issue-assignment split needs the async
    Paperclip join and lives in PostHog; the metric keeps the synchronously-known
    bucket (all Paperclip-driven runs collapse to ``paperclip``, which is exactly
    the autonomous-work bucket to alarm on)."""
    if source in ("dm", "group", "channel"):
        return "channel"
    if source in ("webchat", "cron", "paperclip"):
        return source
    return "unknown"


def schedule_run_completed(*, owner_id: str, parsed: dict, payload: dict, phase: str) -> asyncio.Task | None:
    """Emit a run-completion terminal to CloudWatch and PostHog (both gated).

    ``parsed`` is the ``_parse_session_key`` dict for the event's sessionKey;
    ``phase`` is ``"end"`` or ``"error"``. Deduped on ``owner:run_id`` so a run
    seen by both the chat reader loop and the operator relay tap emits once.
    Fires the ``run.completed`` CloudWatch metric; returns the PostHog
    enrichment+capture task (for the caller's drain set), or None when the run
    was already emitted or the PostHog write key is unset. Synchronous and
    I/O-free — safe on the reader loop.
    """
    run_id = payload.get("runId")
    # Dedup ONCE — this gates BOTH the CloudWatch metric and the PostHog event,
    # so a run observed by both the chat reader loop and the operator relay tap
    # emits each exactly once.
    if run_id:
        dedup_key = f"{owner_id}:{run_id}"
        if dedup_key in _seen_run_ids:
            return None
        _seen_run_ids[dedup_key] = None
        while len(_seen_run_ids) > _SEEN_RUN_IDS_CAP:
            del _seen_run_ids[next(iter(_seen_run_ids))]

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if phase == "error":
        outcome = "failure"
    elif data.get("aborted"):
        outcome = "aborted"
    else:
        outcome = "success"

    # CloudWatch run-outcome metric — the alarmable reliability signal. Emitted
    # for EVERY run terminal, INDEPENDENT of the PostHog write key (a
    # product-analytics switch must never blind ops), and deduped above.
    # ``source`` is intentionally coarse — webchat / channel / cron / paperclip;
    # the fine-grained routine-vs-issue split needs the async Paperclip join and
    # stays in PostHog. For storm detection, paperclip+failure is the signal.
    put_metric("run.completed", dimensions={"outcome": outcome, "source": _coarse_source(parsed.get("source", ""))})

    # PostHog product event — fine-grained, gated on the analytics write key.
    if not settings.POSTHOG_PROJECT_WRITE_KEY:
        return None

    props: dict = {
        "run_id": run_id,
        "agent_id": parsed.get("agent_id"),
        "outcome": outcome,
        "trigger_provider": None,
        "channel": None,
        "is_nightly_ceo": False,
        "enrichment_failed": False,
    }
    duration_ms = data.get("durationMs")
    if isinstance(duration_ms, int) and not isinstance(duration_ms, bool) and duration_ms >= 0:
        props["duration_ms"] = duration_ms
    else:
        started, ended = data.get("startedAt"), data.get("endedAt")
        if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
            props["duration_ms"] = int(ended - started)

    source = parsed.get("source", "")
    issue_id = parsed.get("issue_id")
    needs_enrichment = False
    if source == "webchat":
        props["run_source"] = "webchat"
    elif source in ("dm", "group", "channel"):
        props["run_source"] = "channel"
        props["channel"] = parsed.get("channel")
    elif source == "cron":
        props["run_source"] = "cron"
        if parsed.get("cron_id"):
            props["cron_id"] = parsed["cron_id"]
    elif source == "paperclip" and issue_id:
        # schedule/webhook/manual/api vs issue-assignment lives only in
        # Paperclip's DB — resolved by the async join below.
        props["issue_id"] = issue_id
        needs_enrichment = True
    elif source == "paperclip":
        props["run_source"] = "paperclip_other"
    else:
        props["run_source"] = "unknown"

    return asyncio.create_task(_enrich_and_capture(owner_id=owner_id, props=props, needs_enrichment=needs_enrichment))


async def _enrich_and_capture(*, owner_id: str, props: dict, needs_enrichment: bool) -> None:
    """Task body — never raises (same posture as the billing task wrapper)."""
    try:
        if needs_enrichment:
            try:
                props.update(
                    await asyncio.wait_for(_enrich_from_paperclip(props["issue_id"]), timeout=_ENRICH_TIMEOUT_S)
                )
            except Exception:
                logger.warning(
                    "run_analytics: paperclip enrichment failed for owner %s issue %s — emitting degraded",
                    owner_id,
                    props.get("issue_id"),
                    exc_info=True,
                )
                props["run_source"] = "unknown"
                props["enrichment_failed"] = True
        if props.get("is_nightly_ceo"):
            # The only per-run signal that the retention loop actually fired and how it ended.
            put_metric("nightly_ceo.run", dimensions={"outcome": props["outcome"]})
        props["initiation_type"] = _initiation_type(props["run_source"])
        props["is_internal"] = await _is_internal_owner(owner_id)
        run_id = props.get("run_id")
        capture_product_event(
            distinct_id=owner_id,
            event=EVENT_NAME,
            properties=props,
            event_uuid=str(uuid.uuid5(_CAPTURE_UUID_NS, f"{owner_id}:{run_id}")) if run_id else None,
        )
    except Exception:
        logger.exception("run_analytics: %s emit failed for owner %s", EVENT_NAME, owner_id)


def _initiation_type(run_source: str) -> str:
    if run_source == "unknown":
        return "unknown"
    return "autonomous" if run_source in _AUTONOMOUS_SOURCES else "human"


async def _enrich_from_paperclip(issue_id: str) -> dict:
    """The two-call join: issue -> origin routine -> dispatching run row.

    Uses the process-wide instance-admin cookie (cross-company, same as
    agent_export's routine-detail fetch). Returns the property delta; raises
    on any failure so the caller emits the degraded event instead.
    """
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=_ENRICH_TIMEOUT_S) as http:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)

        issue = await admin.get_issue(session_cookie=cookie, issue_id=issue_id)
        # The execution issue interpolates the routine's title/description, so
        # the seeded nightly-CEO sentinel + frozen title carry over verbatim.
        issue_title = issue.get("title")
        delta: dict = {
            "is_nightly_ceo": (
                str(issue.get("description") or "").startswith(NIGHTLY_CEO_SENTINEL)
                or issue_title == NIGHTLY_CEO_ROUTINE_TITLE
            ),
        }
        if issue.get("originKind") != "routine_execution":
            delta["run_source"] = "issue_assignment"
            return delta

        routine_id = issue.get("originId")
        if not routine_id:
            raise LookupError(f"routine_execution issue {issue_id} has no originId")
        delta["routine_id"] = routine_id
        detail = await admin.get_routine(session_cookie=cookie, routine_id=routine_id)
        runs = detail.get("recentRuns") if isinstance(detail, dict) else None
        run = _match_dispatching_run(runs or [], issue_id)
        if run is None:
            # Outside the detail's 25-row recentRuns window — pull full history.
            runs = await admin.list_routine_runs(routine_id=routine_id, session_cookie=cookie)
            run = _match_dispatching_run(runs, issue_id)
        if run is None:
            raise LookupError(f"no dispatching routine run matched issue {issue_id}")

        source = run.get("source")
        if source not in _ROUTINE_RUN_SOURCES:
            raise LookupError(f"unrecognized routine-run source {source!r} for issue {issue_id}")
        delta["run_source"] = f"routine_{source}"

        # Provider via the seed-title inversion: trigger label first, then the
        # issue title (== the routine title, interpolated verbatim). The
        # fallback matters for poll-shaped automations — gmail.poll's seeded
        # schedule trigger carries NO label, but its routine title is exactly
        # what the inversion keys on. An owner-renamed ROUTINE still inverts
        # to None -> provider stays null, same accepted limitation as
        # agent_export.
        trigger = run.get("trigger")
        label = trigger.get("label") if isinstance(trigger, dict) else None
        automation = (automation_from_title(label) if label else None) or (
            automation_from_title(issue_title) if issue_title else None
        )
        if automation:
            delta["trigger_provider"] = automation[0]
        return delta


def _match_dispatching_run(runs: list, issue_id: str) -> dict | None:
    """The routine_runs row that DISPATCHED this issue. A coalesced/skipped
    fire is finalized with the LIVE issue's linkedIssueId, so several rows can
    share it — only a non-coalesced/skipped row is the dispatcher."""
    for run in runs:
        if (
            isinstance(run, dict)
            and run.get("linkedIssueId") == issue_id
            and run.get("status") not in ("coalesced", "skipped")
        ):
            return run
    return None


async def resolve_internal_owner(owner_id: str) -> bool:
    """Isol8-team tagging via the platform-admin email rule on the owner's
    Clerk PRIMARY email — Clerk is the email truth (the users table carries no
    email attribute). Org owners resolve through the org's ``created_by``
    user. Real answers cache for an hour.

    RAISES on any miss/failure in the Clerk chain (can't-reach-Clerk, a
    genuinely email-less user, or an org with no resolvable ``created_by``) —
    UNCACHED, so a possibly-transient failure never sticks a wrong answer for
    an hour. This is the resolver ops/growth reads use directly: segmentation
    there is load-bearing (every aggregate assumes it ran BEFORE aggregation),
    so a Clerk failure must fail the whole read UNAVAILABLE, never silently
    resolve to a wrong-but-green False. The emit path uses the swallowing
    wrapper below instead — it may never raise into or slow the run path.
    """
    now = time.monotonic()
    cached = _internal_cache.get(owner_id)
    if cached is not None and (now - cached[1]) < _INTERNAL_TTL_S:
        return cached[0]
    user_id = owner_id
    if is_org_owner_id(owner_id):
        org = await clerk_admin.get_organization(owner_id)
        user_id = (org or {}).get("created_by")
        if not user_id:
            raise RuntimeError(f"run_analytics: org {owner_id} has no resolvable created_by")
    email = await clerk_admin.get_primary_email(user_id)
    result = is_platform_admin_email(email)
    _internal_cache[owner_id] = (result, now)
    while len(_internal_cache) > _INTERNAL_CACHE_CAP:
        del _internal_cache[next(iter(_internal_cache))]
    return result


async def _is_internal_owner(owner_id: str) -> bool:
    """Swallowing wrapper around ``resolve_internal_owner`` for the emit path
    (fire-and-forget task, never the reader loop): any miss in the Clerk
    chain -> False (they are customers), same as before this was split out.
    Ops/growth reads must NOT use this — see ``resolve_internal_owner``.
    """
    try:
        return await resolve_internal_owner(owner_id)
    except Exception:
        logger.warning("run_analytics: internal-owner lookup failed for %s", owner_id, exc_info=True)
        return False


# Public swallowing-wrapper alias. Ops/growth reads use resolve_internal_owner
# directly instead (segmentation there must fail loud, not swallow to False).
is_internal_owner = _is_internal_owner
