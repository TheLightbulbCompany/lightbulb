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
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

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

    Identity rule (the workspace is the unit, the person is a human): an ``org_``
    ``distinct_id`` is moved onto the ``workspace_id`` property and the org's
    creator becomes the person.

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
    if distinct_id.startswith("org_"):
        # The identity rule: a workspace is never a PERSON. The org id becomes the
        # ``workspace_id`` property and the workspace's creator is the person.
        props.setdefault("workspace_id", distinct_id)
        creator = _org_creators.get(distinct_id)
        if creator is None:
            # Unknown creator: one Clerk read, off the caller's thread/loop.
            _RESOLVER.submit(_resolve_then_send, client, distinct_id, event, props, event_uuid)
            return
        distinct_id = creator
    _send(client, distinct_id, event, props, event_uuid)


def _send(client: Any, distinct_id: str, event: str, props: dict, event_uuid: str | None) -> None:
    try:
        if event_uuid:
            client.capture(distinct_id, event, props, uuid=event_uuid)
        else:
            client.capture(distinct_id, event, props)
    except Exception:
        logger.warning("PostHog capture failed (event=%s owner=%s)", event, distinct_id, exc_info=True)


# org id -> creator user id (Clerk ``created_by``, immutable). Bounded FIFO.
_ORG_CREATOR_CAP = 4096
_org_creators: dict[str, str] = {}
_RESOLVER = ThreadPoolExecutor(max_workers=2, thread_name_prefix="posthog-org-creator")


def remember_org_creator(org_id: str, user_id: str | None) -> None:
    """Seed the org -> creator cache from a caller that already knows it (the
    ``organization.created`` webhook), so that workspace's events never need a Clerk read."""
    if not org_id or not user_id:
        return
    _org_creators[org_id] = user_id
    while len(_org_creators) > _ORG_CREATOR_CAP:
        del _org_creators[next(iter(_org_creators))]


def _fetch_org_creator(org_id: str) -> str | None:
    """Synchronous Clerk read (runs on ``_RESOLVER``). None on any failure."""
    if not settings.CLERK_SECRET_KEY:
        return None
    try:
        response = httpx.get(
            f"https://api.clerk.com/v1/organizations/{org_id}",
            headers={"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"},
            timeout=5.0,
        )
        if response.status_code >= 400:
            return None
        return response.json().get("created_by") or None
    except Exception:
        logger.warning("PostHog org-creator lookup failed for %s", org_id, exc_info=True)
        return None


def _resolve_then_send(client: Any, org_id: str, event: str, props: dict, event_uuid: str | None) -> None:
    creator = _fetch_org_creator(org_id)
    if creator:
        remember_org_creator(org_id, creator)
    else:
        # ponytail: Clerk unreachable -> the event still lands, on the org id; the
        # readout counts workspace_id, so only the person view degrades.
        logger.warning("PostHog: no creator for %s; event %s keyed on the workspace", org_id, event)
    _send(client, creator or org_id, event, props, event_uuid)


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
    distinct_id: str | None = None,
) -> None:
    """``capture_product_event`` gated to at most once per (event, owner,
    UTC day) via the in-process dedup set above.

    ``owner_id`` is the workspace (a Clerk org id) and is stamped on the event
    as ``workspace_id``; the PostHog PERSON is ``distinct_id`` — the human who
    hit the door — so the event lands on the same timeline as the browser's
    ``$identify``. Callers with no human in hand (server-side wake paths)
    omit it and the workspace's creator becomes the person (see the identity
    rule in ``capture_product_event``).
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
    props = {"workspace_id": owner_id, **(properties or {})}
    capture_product_event(distinct_id=distinct_id or owner_id, event=event, properties=props)


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


def schedule_trial_first_agent_response(owner_id: str, distinct_id: str | None = None) -> "asyncio.Task[None] | None":
    """Spawn the once-ever ``trial_first_agent_response`` capture for an owner.

    Synchronous and I/O-free — safe on the gateway reader loop. Returns the
    task for the caller's drain set, or None when the PostHog bridge is off
    (unit tests, local dev) or this process already resolved the owner.

    ``distinct_id`` is the human whose message the agent answered, so the
    event lands on their PostHog timeline; ``owner_id`` (the workspace) rides
    along as ``workspace_id``. Without a human in hand the workspace id is
    the person.

    Invariants:
    - Once ever per owner: gated by a conditional write of
      ``first_agent_response_at`` on the billing row, which also records the
      time-to-first-value. Survives restarts, deploys and races; the uuid is
      only a same-day belt.
    - Trial workspaces only (``subscription_status == trial_local`` at the
      moment of the reply); a paid workspace never fires it.
    - Never raises; a failed write is logged and the owner is NOT memoised, so
      a later reply retries.
    """
    if not settings.POSTHOG_PROJECT_WRITE_KEY:
        return None
    if owner_id in _first_response_done:
        return None
    return asyncio.create_task(_capture_trial_first_agent_response(owner_id, distinct_id))


async def _capture_trial_first_agent_response(owner_id: str, distinct_id: str | None = None) -> None:
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
        distinct_id=distinct_id or owner_id,
        event="trial_first_agent_response",
        properties={"workspace_id": owner_id},
        event_uuid=str(uuid.uuid5(_TRIAL_FIRST_RESPONSE_NS, f"{owner_id}:{utc_today()}")),
    )
