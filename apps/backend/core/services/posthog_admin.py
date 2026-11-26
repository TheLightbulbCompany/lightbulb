"""PostHog HogQL client for the admin dashboard (``hogql``)."""

import asyncio
import contextlib
import logging

import httpx

from core.config import settings
from core.observability import product_analytics
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)


_TIMEOUT_S = 5.0


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
