"""PostHog HogQL client for the admin dashboard (``hogql``)."""

import logging

import httpx

from core.config import settings

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
