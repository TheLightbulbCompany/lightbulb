"""PostHog Events API client for the admin dashboard.

Used by /admin/users/{user_id}/posthog to render the Activity tab —
recent events for a specific user plus a deep link to PostHog's
session-replay UI.

The user's PostHog `distinct_id` is the Clerk user_id, because
apps/frontend/src/components/PostHogProvider.tsx:51 already calls
`posthog.identify(userId, ...)` with the Clerk JWT's `sub`.

Stubs gracefully when POSTHOG_PROJECT_API_KEY is unset (returns
{events: [], stubbed: True}) — local dev works without a real
PostHog project. Returns missing=True on 404 so the UI can render
"no PostHog activity recorded" instead of treating the user as
broken.

NOTE: do NOT query `/api/projects/{id}/persons/?distinct_id=X` expecting
a `person["events"]` field — the Persons API returns only {type, id,
uuid, distinct_ids, properties, ...}. Use the Events API directly:
`GET /api/projects/{id}/events/?distinct_id=X&limit=N` which returns
`{results: [...]}` with each element already event-shaped.

The /events/ endpoint requires the `query:read` scope on the personal
API key; a 403 with "scope" in the body is surfaced as
error="insufficient_scope" so the frontend can render a targeted hint
instead of a generic `http_403`.
"""

import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)


_TIMEOUT_S = 5.0


async def get_person_events(*, distinct_id: str, limit: int = 100) -> dict:
    """Fetch recent events for a Clerk user from PostHog.

    Queries PostHog's Events API
    (`GET /api/projects/{id}/events/?distinct_id=...`) rather than the
    Persons API, because the Persons API response does not include the
    `events` field and will always yield an empty list.

    Returns:
        {events: list[dict], stubbed: bool, missing: bool, error: str | None}

    - stubbed=True when POSTHOG_PROJECT_API_KEY is unset; events=[].
    - missing=True when PostHog returns 404; events=[].
    - error="insufficient_scope" when PostHog returns 403 due to the
      personal API key missing the `query:read` scope required by the
      Events endpoint; events=[].
    - error populated on transient failures (timeout, 5xx, other 4xx);
      events=[].
    - Otherwise, events is a list of {timestamp, event, properties, session_id}.

    Each event includes `session_id` (from the `$session_id` property).
    """
    if not settings.POSTHOG_PROJECT_API_KEY or not settings.POSTHOG_PROJECT_ID:
        return {"events": [], "stubbed": True, "missing": False, "error": None}

    url = f"{settings.POSTHOG_HOST}/api/projects/{settings.POSTHOG_PROJECT_ID}/events/"
    headers = {"Authorization": f"Bearer {settings.POSTHOG_PROJECT_API_KEY}"}
    params = {"distinct_id": distinct_id, "limit": min(limit, 500)}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(url, headers=headers, params=params)
    except httpx.TimeoutException:
        return {"events": [], "stubbed": False, "missing": False, "error": "timeout"}
    except Exception as e:  # noqa: BLE001
        logger.warning("posthog_admin.get_person_events network error: %s", e)
        return {"events": [], "stubbed": False, "missing": False, "error": str(e)}

    if response.status_code == 404:
        return {"events": [], "stubbed": False, "missing": True, "error": None}

    if response.status_code == 403 and "scope" in response.text.lower():
        logger.warning(
            "posthog_admin.get_person_events insufficient scope (403): %s",
            response.text[:200],
        )
        return {
            "events": [],
            "stubbed": False,
            "missing": False,
            "error": "insufficient_scope",
        }

    if response.status_code >= 400:
        logger.warning(
            "posthog_admin.get_person_events HTTP %s: %s",
            response.status_code,
            response.text[:200],
        )
        return {
            "events": [],
            "stubbed": False,
            "missing": False,
            "error": f"http_{response.status_code}",
        }

    data = response.json()
    events: list[dict] = []
    for ev in data.get("results", []):
        properties = ev.get("properties") or {}
        events.append(
            {
                "timestamp": ev.get("timestamp"),
                "event": ev.get("event"),
                "properties": properties,
                "session_id": properties.get("$session_id"),
            }
        )
    return {"events": events, "stubbed": False, "missing": False, "error": None}


async def hogql(query: str, *, timeout_s: float = 15.0) -> dict:
    """Run one HogQL statement via ``POST /api/projects/{id}/query/``.

    Needs the ``query:read`` scope on the personal ``phx_`` key (verified on
    the prod key 2026-08-26). PostHog caps execution at 10s and rows at 100
    by default / 50k with an explicit LIMIT; the ``/query`` bucket is 2,400
    requests/hour shared across the team, so callers cache.

    Returns ``{columns, results, stubbed, error}``; never raises.
    """
    empty: dict = {"columns": [], "results": []}
    if not settings.POSTHOG_PROJECT_API_KEY or not settings.POSTHOG_PROJECT_ID:
        return {**empty, "stubbed": True, "error": None}

    url = f"{settings.POSTHOG_HOST}/api/projects/{settings.POSTHOG_PROJECT_ID}/query/"
    headers = {"Authorization": f"Bearer {settings.POSTHOG_PROJECT_API_KEY}"}
    body = {"query": {"kind": "HogQLQuery", "query": query}}

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException:
        return {**empty, "stubbed": False, "error": "timeout"}
    except Exception as e:  # noqa: BLE001
        logger.warning("posthog_admin.hogql network error: %s", e)
        return {**empty, "stubbed": False, "error": str(e)}

    if response.status_code == 403 and "scope" in response.text.lower():
        return {**empty, "stubbed": False, "error": "insufficient_scope"}
    if response.status_code != 200:
        return {**empty, "stubbed": False, "error": f"http_{response.status_code}"}

    try:
        data = response.json()
    except ValueError as e:
        logger.warning("posthog_admin.hogql invalid JSON response: %s", e)
        return {**empty, "stubbed": False, "error": "invalid_response"}
    return {
        "columns": list(data.get("columns") or []),
        "results": list(data.get("results") or []),
        "stubbed": False,
        "error": None,
    }
