"""PostHog for the admin: the dashboard's saved endpoints (``endpoint``) and person deletion (``delete_person``)."""

import asyncio
import contextlib
import hashlib
import logging
import re

import httpx

from core.config import settings
from core.observability import product_analytics
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)


_TIMEOUT_S = 5.0


ENDPOINT_FRESH_S = 900  # how often PostHog recomputes a materialized endpoint; 15 minutes is its shortest setting
_HEX8 = "[0-9a-f]{8}"


def _rows(error: str | None = None, *, stubbed: bool = False, **data) -> dict:
    return {"columns": [], "results": [], "as_of": None, **data, "stubbed": stubbed, "error": error}


async def endpoint(stem: str, sql: str, *, timeout_s: float = 15.0) -> dict:
    """Rows of one saved PostHog endpoint: ``POST /api/projects/{id}/endpoints/{name}/run/``.

    PostHog's guidance (posthog.com/docs/endpoints, read 2026-11-26): code that runs a query repeatedly calls a saved
    endpoint, not the ad hoc ``/query`` API; a materialized endpoint is recomputed on a schedule (``ENDPOINT_FRESH_S``),
    answers from the stored result and allows 10 requests at once, where ``/query`` and an endpoint that is not
    materialized (yet) allow 3 and make the rest wait. Endpoints are in beta.

    The query stays in this repo. The endpoint's name is ``<stem>_<first 8 hex digits of sha1(sql)>``, so "PostHog has
    this exact query" is "the name exists". On a 404 this deletes the stem's older endpoints (a materialized endpoint is
    recomputed until it is deleted), publishes the query and runs it; until its first computation finishes PostHog runs
    it live. Deleting first means a call cut short leaves nothing behind that the next call does not finish. The
    personal key needs ``endpoint:read`` and ``endpoint:write``.

    Returns ``{columns, results, as_of, stubbed, error}``; never raises. ``as_of`` is when PostHog computed the rows.
    """
    if not settings.POSTHOG_PROJECT_API_KEY or not settings.POSTHOG_PROJECT_ID:
        return _rows(stubbed=True)

    name = f"{stem}_{hashlib.sha1(sql.encode()).hexdigest()[:8]}"
    base = f"{settings.POSTHOG_HOST}/api/projects/{settings.POSTHOG_PROJECT_ID}/endpoints/"
    headers = {"Authorization": f"Bearer {settings.POSTHOG_PROJECT_API_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=timeout_s, headers=headers) as client:
            response = await client.request("POST", f"{base}{name}/run/", json={})
            if response.status_code == 404:
                listing = await client.request("GET", base)
                published = (listing.json().get("results") or []) if listing.status_code == 200 else []
                for old in (e["name"] for e in published):
                    if old != name and re.fullmatch(f"{re.escape(stem)}_{_HEX8}", old):
                        await client.request("DELETE", f"{base}{old}/")
                body = {
                    "name": name,
                    "query": {"kind": "HogQLQuery", "query": sql},
                    "is_materialized": True,
                    "data_freshness_seconds": ENDPOINT_FRESH_S,
                }
                await client.request("POST", base, json=body)  # refused when another task published it first
                response = await client.request("POST", f"{base}{name}/run/", json={})
    except httpx.TimeoutException:
        return _rows("timeout")
    except Exception as e:  # noqa: BLE001
        logger.warning("posthog_admin.endpoint error: %s", type(e).__name__)
        return _rows(type(e).__name__)

    if response.status_code == 403 and "scope" in response.text.lower():
        return _rows("insufficient_scope")
    if response.status_code != 200:
        return _rows(f"http_{response.status_code}")
    try:
        data = response.json()
    except ValueError:
        return _rows("invalid_response")
    if data.get("hasMore"):  # a first page only: wrong numbers are worse than a section that says it could not load
        return _rows("truncated")
    return _rows(
        columns=list(data.get("columns") or []),
        results=list(data.get("results") or []),
        as_of=data.get("endpoint_materialized_at") or data.get("last_refresh"),
    )


async def delete_person(user_id: str) -> None:
    """Delete the person whose distinct id is `user_id` from PostHog, with their events, when their account is
    deleted. One call: `POST /api/projects/{id}/persons/bulk_delete/` takes distinct ids and `delete_events`.
    Needs the `person:write` scope on the personal key. PostHog answers 202 and removes the person in the
    background; only events captured before the request are deleted. Best effort and never raises: a failure
    counts `posthog.person_delete_failed` and logs the status code or exception type only."""
    if not settings.POSTHOG_PROJECT_API_KEY or not settings.POSTHOG_PROJECT_ID:
        return
    if settings.ENVIRONMENT not in ("dev", "prod"):
        return
    # Events captured earlier in this request may still be queued: send them first, or one could recreate the person.
    # A slow PostHog times the wait out; the thread flushes on and the delete goes ahead.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.to_thread(product_analytics.flush), _TIMEOUT_S)
    url = f"{settings.POSTHOG_HOST}/api/projects/{settings.POSTHOG_PROJECT_ID}/persons/bulk_delete/"
    headers = {"Authorization": f"Bearer {settings.POSTHOG_PROJECT_API_KEY}"}
    body = {"distinct_ids": [user_id], "delete_events": True}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.post(url, headers=headers, json=body)
        if response.status_code == 404:  # no such person
            return
        if response.status_code != 202:
            raise RuntimeError(f"http_{response.status_code}")
        if response.json().get("deletion_errors"):  # a 202 can still report persons not deleted
            raise RuntimeError("deletion_errors")
    except Exception as e:  # noqa: BLE001  best effort: the account is deleted either way
        put_metric("posthog.person_delete_failed")
        logger.warning("posthog person delete failed: %s", str(e) if isinstance(e, RuntimeError) else type(e).__name__)
