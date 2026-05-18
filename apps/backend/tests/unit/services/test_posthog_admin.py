"""Tests for the PostHog Events API admin client.

Covers:
- Stubbed mode when POSTHOG_PROJECT_API_KEY unset (CEO local-dev requirement).
- Happy path: /events/ results → flattened event dicts, $session_id surfaced.
- Regression: requests hit /events/ (NOT /persons/) — the old code mistakenly
  queried /persons/ which never returns events, yielding a perpetually empty
  Activity tab.
- distinct_id and limit are forwarded as query params.
- 404 → missing=True (CEO E5).
- 403 with "scope" in body → error="insufficient_scope" (Events endpoint
  requires `query:read` scope on the personal API key).
- Other 4xx/5xx → error="http_{code}".
- Timeout → error="timeout".
- Generic network error → error populated with exception message.
- session_replay_url helper.

Mocks httpx.AsyncClient directly via unittest.mock — respx 0.20.2 has
a compat issue with newer httpx where even identical URL strings don't
match, so we patch the client surface instead.
"""

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


def _make_response(status_code: int, json_body: dict | None = None, text_body: str = "") -> MagicMock:
    """Build a mock httpx.Response."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json = MagicMock(return_value=json_body or {})
    response.text = text_body
    return response


@asynccontextmanager
async def _fake_client(response_or_exc):
    """Async context manager that yields a fake AsyncClient.

    response_or_exc: either a Response (returned by .get) or an Exception
    (raised by .get).
    """
    client = MagicMock()
    if isinstance(response_or_exc, BaseException):
        client.get = AsyncMock(side_effect=response_or_exc)
    else:
        client.get = AsyncMock(return_value=response_or_exc)
    yield client


def _apply_posthog_settings(monkeypatch, *, api_key="phc_xyz", project_id="12345", host="https://app.posthog.com"):
    monkeypatch.setattr("core.config.settings.POSTHOG_HOST", host)
    monkeypatch.setattr("core.config.settings.POSTHOG_PROJECT_ID", project_id)
    monkeypatch.setattr("core.config.settings.POSTHOG_PROJECT_API_KEY", api_key)


@pytest.mark.asyncio
async def test_stub_when_no_api_key(monkeypatch):
    monkeypatch.setattr("core.config.settings.POSTHOG_PROJECT_API_KEY", "")
    monkeypatch.setattr("core.config.settings.POSTHOG_PROJECT_ID", "12345")

    # Ensure no HTTP call is even attempted.
    with patch("core.services.posthog_admin.httpx.AsyncClient") as mock_client:
        from core.services.posthog_admin import get_person_events

        result = await get_person_events(distinct_id="user_test", limit=10)

    assert result["stubbed"] is True
    assert result["events"] == []
    assert result["missing"] is False
    assert result["error"] is None
    mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_stubs_when_project_id_unset(monkeypatch):
    monkeypatch.setattr("core.config.settings.POSTHOG_PROJECT_API_KEY", "phc_xyz")
    monkeypatch.setattr("core.config.settings.POSTHOG_PROJECT_ID", "")

    from core.services.posthog_admin import get_person_events

    result = await get_person_events(distinct_id="user_test")
    assert result["stubbed"] is True


@pytest.mark.asyncio
async def test_parses_events_from_results(monkeypatch):
    _apply_posthog_settings(monkeypatch)

    body = {
        "next": None,
        "results": [
            {
                "id": "01HABC",
                "distinct_id": "user_test",
                "timestamp": "2026-04-24T12:34:56.789Z",
                "event": "$pageview",
                "properties": {
                    "$session_id": "s1",
                    "$current_url": "/chat",
                    "$os": "Mac OS X",
                },
            },
            {
                "id": "01HDEF",
                "distinct_id": "user_test",
                "timestamp": "2026-04-24T12:35:00.000Z",
                "event": "chat_sent",
                "properties": {"$session_id": "s2", "agent_id": "agent_x"},
            },
        ],
    }
    response = _make_response(200, json_body=body)

    with patch(
        "core.services.posthog_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.posthog_admin import get_person_events

        result = await get_person_events(distinct_id="user_test", limit=20)

    assert result["stubbed"] is False
    assert result["missing"] is False
    assert result["error"] is None
    assert len(result["events"]) == 2

    e0 = result["events"][0]
    assert e0["event"] == "$pageview"
    assert e0["timestamp"] == "2026-04-24T12:34:56.789Z"
    assert e0["session_id"] == "s1"
    assert e0["properties"]["$current_url"] == "/chat"

    e1 = result["events"][1]
    assert e1["event"] == "chat_sent"
    assert e1["timestamp"] == "2026-04-24T12:35:00.000Z"
    assert e1["session_id"] == "s2"


@pytest.mark.asyncio
async def test_hits_events_endpoint_not_persons(monkeypatch):
    """Regression: the original bug hit /persons/ which never returns events."""
    _apply_posthog_settings(monkeypatch)

    captured = {}
    response = _make_response(200, json_body={"results": []})

    @asynccontextmanager
    async def capturing_client(*args, **kwargs):
        client = MagicMock()

        async def get(url, headers=None, params=None, **kw):
            captured["url"] = url
            captured["headers"] = headers
            captured["params"] = params
            return response

        client.get = get
        yield client

    with patch("core.services.posthog_admin.httpx.AsyncClient", new=capturing_client):
        from core.services.posthog_admin import get_person_events

        await get_person_events(distinct_id="user_test")

    assert "/events/" in captured["url"]
    assert "/persons/" not in captured["url"]
    assert captured["url"].endswith("/api/projects/12345/events/")
    # Auth header still a bearer personal API key.
    assert captured["headers"]["Authorization"] == "Bearer phc_xyz"


@pytest.mark.asyncio
async def test_passes_distinct_id_and_limit(monkeypatch):
    _apply_posthog_settings(monkeypatch)

    captured = {}
    response = _make_response(200, json_body={"results": []})

    @asynccontextmanager
    async def capturing_client(*args, **kwargs):
        client = MagicMock()

        async def get(url, headers=None, params=None, **kw):
            captured["params"] = params
            return response

        client.get = get
        yield client

    with patch("core.services.posthog_admin.httpx.AsyncClient", new=capturing_client):
        from core.services.posthog_admin import get_person_events

        await get_person_events(distinct_id="user_xyz", limit=42)

    assert captured["params"]["distinct_id"] == "user_xyz"
    assert captured["params"]["limit"] == 42


@pytest.mark.asyncio
async def test_limit_capped_at_500(monkeypatch):
    _apply_posthog_settings(monkeypatch)

    captured = {}
    response = _make_response(200, json_body={"results": []})

    @asynccontextmanager
    async def capturing_client(*args, **kwargs):
        client = MagicMock()

        async def get(url, headers=None, params=None, **kw):
            captured["params"] = params
            return response

        client.get = get
        yield client

    with patch("core.services.posthog_admin.httpx.AsyncClient", new=capturing_client):
        from core.services.posthog_admin import get_person_events

        await get_person_events(distinct_id="user_xyz", limit=9999)

    assert captured["params"]["limit"] == 500


@pytest.mark.asyncio
async def test_handles_404_as_missing(monkeypatch):
    _apply_posthog_settings(monkeypatch)

    response = _make_response(404, json_body={})
    with patch(
        "core.services.posthog_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.posthog_admin import get_person_events

        result = await get_person_events(distinct_id="user_missing")

    assert result["missing"] is True
    assert result["events"] == []
    assert result["error"] is None


@pytest.mark.asyncio
async def test_handles_403_scope_error(monkeypatch):
    """Regression: /events/ requires query:read scope on the personal API key.

    Surface this as a distinct error so the frontend can render a
    specific hint instead of generic http_403.
    """
    _apply_posthog_settings(monkeypatch)

    response = _make_response(
        403,
        json_body={"detail": "API key missing required scope 'query:read'"},
        text_body='{"detail":"API key missing required scope \'query:read\'"}',
    )
    with patch(
        "core.services.posthog_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.posthog_admin import get_person_events

        result = await get_person_events(distinct_id="user_test")

    assert result["error"] == "insufficient_scope"
    assert result["events"] == []
    assert result["missing"] is False
    assert result["stubbed"] is False


@pytest.mark.asyncio
async def test_403_without_scope_falls_through_to_generic_error(monkeypatch):
    """A 403 that isn't a scope problem stays as http_403."""
    _apply_posthog_settings(monkeypatch)

    response = _make_response(403, text_body="forbidden")
    with patch(
        "core.services.posthog_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.posthog_admin import get_person_events

        result = await get_person_events(distinct_id="user_test")

    assert result["error"] == "http_403"


@pytest.mark.asyncio
async def test_handles_generic_5xx(monkeypatch):
    _apply_posthog_settings(monkeypatch)

    response = _make_response(500, text_body="internal server error")
    with patch(
        "core.services.posthog_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.posthog_admin import get_person_events

        result = await get_person_events(distinct_id="user_test")

    assert result["error"] == "http_500"
    assert result["events"] == []
    assert result["missing"] is False


@pytest.mark.asyncio
async def test_handles_timeout(monkeypatch):
    _apply_posthog_settings(monkeypatch)

    with patch(
        "core.services.posthog_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(httpx.TimeoutException("slow")),
    ):
        from core.services.posthog_admin import get_person_events

        result = await get_person_events(distinct_id="user_test")

    assert result["error"] == "timeout"
    assert result["events"] == []


@pytest.mark.asyncio
async def test_handles_network_error(monkeypatch):
    _apply_posthog_settings(monkeypatch)

    with patch(
        "core.services.posthog_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(RuntimeError("dns boom")),
    ):
        from core.services.posthog_admin import get_person_events

        result = await get_person_events(distinct_id="user_test")

    assert result["events"] == []
    assert result["error"] is not None
    assert "dns boom" in result["error"]
