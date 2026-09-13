"""Server-side PostHog product-event bridge.

Two telemetry systems, two jobs — this file is the second one:

- CloudWatch EMF (``core/observability/metrics.py``) is OPERATIONAL telemetry:
  aggregate, alertable, person-agnostic (high-cardinality dims like user_id are
  deliberately forbidden). It answers "is the system healthy — did cancellations
  spike, what's provisioning p95".
- PostHog is CUSTOMER analytics: per-person, joinable, behavioral. It answers
  "did THIS user convert/churn, what did they do before churning, what's the
  trial->paid rate for the June cohort".

Billing-lifecycle facts (trial start, trial->paid, cancellation) are BOTH: the
EMF metric drives the spike alarm; this bridge lands the same fact on the
PERSON so trial->paid rate, churn cohorts, and pre-churn behavior become
PostHog funnels. It COMPLEMENTS the EMF metrics — it does not replace them.

``distinct_id`` is the subscriber's Clerk user_id, matching the frontend's
``identify()``, so server events merge onto the same PostHog person. Best-effort
by design: an unset key or a PostHog outage must NEVER break the Stripe webhook
path, so every failure is swallowed (logged) rather than raised.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from core.config import settings
from core.services.trial_cap import utc_today

logger = logging.getLogger(__name__)

# Lazily-initialised singleton. ``_init_attempted`` makes a missing key or an
# init failure a one-time log line, not a per-event retry.
_client: Any = None
_init_attempted = False


def _client_or_none() -> Any:
    global _client, _init_attempted
    if _init_attempted:
        return _client
    _init_attempted = True
    if not settings.POSTHOG_PROJECT_WRITE_KEY:
        logger.info(
            "POSTHOG_PROJECT_WRITE_KEY unset — server-side product-analytics "
            "bridge disabled (billing lifecycle stays EMF-only)."
        )
        return None
    try:
        from posthog import Posthog

        # posthog-python 5.x: first positional / ``api_key`` is the project
        # (phc_) ingestion key — NOT ``project_api_key`` (that kwarg 4xx'd
        # away between majors).
        _client = Posthog(
            api_key=settings.POSTHOG_PROJECT_WRITE_KEY,
            host=settings.POSTHOG_HOST or "https://us.posthog.com",
        )
    except Exception:
        logger.exception("PostHog client init failed — product-analytics bridge disabled")
        _client = None
    return _client


def capture_product_event(
    *,
    distinct_id: str | None,
    event: str,
    properties: dict | None = None,
    set_person: dict | None = None,
    event_uuid: str | None = None,
) -> None:
    """Capture one PostHog product event keyed on the subscriber's Clerk id.

    ``set_person`` is merged into ``$set`` so the person profile carries current
    billing state (subscription_status / is_subscribed / trial_end /
    provider_choice) for segmentation. ``event_uuid`` is forwarded as the
    capture ``uuid`` — a DETERMINISTIC id (e.g. uuid5 of a runId) lets PostHog
    ingestion drop exact duplicates, giving effectively-once emission across
    backend restarts. No-op — logged, never raised — when the write key is
    unset, the client failed to init, or capture errors; telemetry must not
    break the billing path.
    """
    client = _client_or_none()
    if client is None or not distinct_id:
        return
    props = dict(properties or {})
    # Super-property so dev and prod are separable in the shared PostHog project.
    # setdefault: a caller may override it, but every server-side event that
    # doesn't gets stamped exactly once, here (capture_once_per_owner_per_day
    # and the trial helpers all route through this function). Empty ENVIRONMENT
    # (local dev / CI) reads as "local".
    props.setdefault("environment", settings.ENVIRONMENT or "local")
    if set_person:
        props["$set"] = {**props.get("$set", {}), **set_person}
    try:
        if event_uuid:
            client.capture(distinct_id, event, props, uuid=event_uuid)
        else:
            client.capture(distinct_id, event, props)
    except Exception:
        logger.warning("PostHog capture failed (event=%s owner=%s)", event, distinct_id, exc_info=True)


# Bounded in-process dedup for once-per-owner-per-day capture events
# (``trial_cap_reached``, ``trial_expired_block``) — both fire from a
# decision point a caller re-evaluates on every poll/turn once the
# blocking condition holds (the daily cap, an expired local trial), so
# without this an owner stuck behind either block would spam the same
# event on every retry. Insertion-ordered dict used as a bounded FIFO set
# — same idiom as ``run_analytics._seen_run_ids``.
# In-process, SINGLE-BACKEND-INSTANCE scope: a restart (or a fleet running
# more than one backend task) simply re-fires the event once more per
# instance — acceptable for a funnel count, where "roughly once a day"
# beats persisting extra DynamoDB state for exact-once dedup.
_DAILY_DEDUP_CAP = 4096
_daily_dedup_seen: dict[tuple[str, str, str], None] = {}


def capture_once_per_owner_per_day(
    *,
    owner_id: str,
    event: str,
    properties: dict | None = None,
) -> None:
    """``capture_product_event`` gated to at most once per (event, owner,
    UTC day) via the in-process dedup set above. ``owner_id`` doubles as
    ``distinct_id`` — both new callers key exclusively on the personal
    free-tier trial_local path, where the owner IS the Clerk user id.
    """
    key = (event, owner_id, utc_today())
    if key in _daily_dedup_seen:
        return
    # Insert BEFORE capturing so a burst of near-simultaneous callers (a
    # retried request racing the original) can't all slip through as if
    # none of them had been seen yet.
    _daily_dedup_seen[key] = None
    while len(_daily_dedup_seen) > _DAILY_DEDUP_CAP:
        oldest = next(iter(_daily_dedup_seen))
        del _daily_dedup_seen[oldest]
    capture_product_event(distinct_id=owner_id, event=event, properties=properties)


# ---------------------------------------------------------------------------
# trial_first_agent_response — a trial owner's first agent reply, once ever
# ---------------------------------------------------------------------------

# Owners this process has already RESOLVED (fired, already stamped, or not
# on a trial). Added only after the durable check answered, never before an
# await, so a cancelled task cannot leave an owner marked resolved with
# nothing emitted. Unbounded on purpose: one string per owner per process.
_first_response_done: set[str] = set()

# Same-day belt for the durable gate below — PostHog dedups a repeated uuid
# only within one UTC day (ClickHouse sorts on toDate(timestamp)), so this is
# NOT a lifetime key; the billing-row CAS is what makes the event once-ever.
_TRIAL_FIRST_RESPONSE_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:trial_first_agent_response")


def schedule_trial_first_agent_response(owner_id: str) -> "asyncio.Task[None] | None":
    """Spawn the once-ever ``trial_first_agent_response`` capture for an owner.

    Synchronous and I/O-free — safe on the gateway reader loop. Returns the
    task for the caller's drain set, or None when the PostHog bridge is off
    (unit tests, local dev) or this process already resolved the owner.

    Invariants:
    - Once ever per owner: gated by a conditional write of
      ``first_agent_response_at`` on the billing row, which also records the
      time-to-first-value. Survives restarts, deploys and races; the uuid is
      only a same-day belt.
    - Trial owners only (``subscription_status == trial_local`` at the moment
      of the reply); paid and org owners never fire it.
    - Never raises; a failed write is logged and the owner is NOT memoised, so
      a later reply retries.
    """
    if not settings.POSTHOG_PROJECT_WRITE_KEY:
        return None
    if owner_id in _first_response_done:
        return None
    return asyncio.create_task(_capture_trial_first_agent_response(owner_id))


async def _capture_trial_first_agent_response(owner_id: str) -> None:
    try:
        from core.repositories import billing_repo

        won = await billing_repo.mark_trial_first_agent_response(owner_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("trial_first_agent_response: billing CAS failed for %s", owner_id, exc_info=True)
        return
    _first_response_done.add(owner_id)
    if not won:
        return
    capture_product_event(
        distinct_id=owner_id,
        event="trial_first_agent_response",
        event_uuid=str(uuid.uuid5(_TRIAL_FIRST_RESPONSE_NS, f"{owner_id}:{utc_today()}")),
    )
