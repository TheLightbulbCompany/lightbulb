"""One server-side PostHog event per completed agent run: ``agent_run_completed``.

Emitted from the run-completion callers (the room chat path's
``handle_run_event``, the operator relay tap, and the usage callback for
non-room runs) — the one signal billing already trusts for EVERY session
type (webchat, channel, cron, room). It
carries the owner, initiation type (autonomous vs human), trigger provider and
outcome, powering the activation gate, the North Star (successful autonomous
runs / active owner / week) and retention cohorts.

Session-key classification is synchronous; everything with I/O (the internal-owner lookup, the capture itself) runs in a
spawned task the caller tracks and cancels on connection close (best-effort —
an emit lost at teardown is the same accepted loss class as backend-down). The whole path is
best-effort by contract: no failure here may ever raise into or slow the run
path.

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


from core.auth import is_org_owner_id, is_internal_email
from core.config import settings
from core.observability.metrics import put_metric
from core.observability.product_analytics import capture_product_event
from core.services import clerk_admin

logger = logging.getLogger(__name__)

EVENT_NAME = "agent_run_completed"

# Namespace for the deterministic capture uuid — uuid5(namespace, owner:runId)
# is stable across restarts, so PostHog ingestion can drop exact re-emits.
# DO NOT RENAME the "isol8:" seed: it fixes the capture uuid PostHog dedupes on;
# changing it re-emits every already-ingested run as a new event.
_CAPTURE_UUID_NS = uuid.uuid5(uuid.NAMESPACE_URL, "isol8:agent_run_completed")

# North Star numerator sources. Everything else classified is human-initiated;
# only an unresolved source stays "unknown".
_AUTONOMOUS_SOURCES = frozenset({"routine_schedule", "routine_webhook", "cron", "heartbeat_timer"})

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
    """Coarse run origin for the CloudWatch ``run.completed`` metric."""
    if source in ("dm", "group", "channel"):
        return "channel"
    if source in ("webchat", "cron", "room"):
        return source
    return "unknown"


def schedule_run_completed(
    *, owner_id: str, parsed: dict, payload: dict, phase: str, person_id: str | None = None
) -> asyncio.Task | None:
    """Emit a run-completion terminal to CloudWatch and PostHog (both gated).

    ``parsed`` is the ``_parse_session_key`` dict for the event's sessionKey;
    ``phase`` is ``"end"`` or ``"error"``. ``person_id`` is the human the run
    answered (a room run's requester); without one the workspace's creator is the
    person (``capture_product_event``'s identity rule). Deduped on ``owner:run_id`` so a run
    seen by both the chat reader loop and the operator relay tap emits once.
    Fires the ``run.completed`` CloudWatch metric; returns the PostHog
    capture task (for the caller's drain set), or None when the run
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
    # ``source`` is intentionally coarse — webchat / channel / cron / room.
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
    if source == "webchat":
        props["run_source"] = "webchat"
    elif source == "room":
        props["run_source"] = "room"
        props["room_id"] = parsed.get("room_id")
    elif source in ("dm", "group", "channel"):
        props["run_source"] = "channel"
        props["channel"] = parsed.get("channel")
    elif source == "cron":
        props["run_source"] = "cron"
        if parsed.get("cron_id"):
            props["cron_id"] = parsed["cron_id"]
    else:
        props["run_source"] = "unknown"

    props["workspace_id"] = owner_id
    return asyncio.create_task(_capture(owner_id=owner_id, props=props, person_id=person_id))


async def _capture(*, owner_id: str, props: dict, person_id: str | None = None) -> None:
    """Task body — never raises (same posture as the billing task wrapper)."""
    try:
        props["initiation_type"] = _initiation_type(props["run_source"])
        props["is_internal"] = await _is_internal_owner(owner_id)
        run_id = props.get("run_id")
        capture_product_event(
            distinct_id=person_id or owner_id,
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
    result = is_internal_email(email)
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
