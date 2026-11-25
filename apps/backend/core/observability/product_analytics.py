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

import logging
from typing import Any


from core.config import settings

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
    set_once: dict | None = None,
    event_uuid: str | None = None,
) -> None:
    """Capture one PostHog product event keyed on the subscriber's Clerk id.

    The ``distinct_id`` is the person's own Clerk user id (the person is the tenant).

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
    if set_once:
        props["$set_once"] = {**props.get("$set_once", {}), **set_once}
    _send(client, distinct_id, event, props, event_uuid)


def alias_product_person(*, person_id: str, anonymous_id: str) -> None:
    """Join an anonymous distinct id (a Spark install) onto a person, so its earlier events count as theirs.
    PostHog's `$create_alias` (posthog-python `alias(previous_id, distinct_id)`) adds `distinct_id` to the
    person `previous_id` already names; PostHog refuses it only when the added id is itself an identified
    person, which an install id never is. Best-effort like `capture_product_event`: never raises."""
    client = _client_or_none()
    if client is None or not person_id or not anonymous_id:
        return
    try:
        client.alias(previous_id=person_id, distinct_id=anonymous_id)
    except Exception:
        logger.warning("PostHog alias failed (person=%s)", person_id, exc_info=True)


def _send(client: Any, distinct_id: str, event: str, props: dict, event_uuid: str | None) -> None:
    try:
        if event_uuid:
            client.capture(distinct_id, event, props, uuid=event_uuid)
        else:
            client.capture(distinct_id, event, props)
    except Exception:
        logger.warning("PostHog capture failed (event=%s owner=%s)", event, distinct_id, exc_info=True)
