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


def _apply_posthog_settings(monkeypatch, *, api_key="phc_xyz", project_id="12345", host="https://app.posthog.com"):
    monkeypatch.setattr("core.config.settings.POSTHOG_HOST", host)
    monkeypatch.setattr("core.config.settings.POSTHOG_PROJECT_ID", project_id)
    monkeypatch.setattr("core.config.settings.POSTHOG_PROJECT_API_KEY", api_key)


@asynccontextmanager
async def _fake_post_client(response_or_exc):
    client = MagicMock()
    if isinstance(response_or_exc, BaseException):
        client.post = AsyncMock(side_effect=response_or_exc)
    else:
        client.post = AsyncMock(return_value=response_or_exc)
    yield client


@pytest.mark.asyncio
async def test_hogql_stubbed_without_key(monkeypatch):
    _apply_posthog_settings(monkeypatch, api_key="")
    from core.services import posthog_admin

    out = await posthog_admin.hogql("select 1")
    assert out == {"columns": [], "results": [], "stubbed": True, "error": None}


@pytest.mark.asyncio
async def test_hogql_posts_query_kind_and_parses(monkeypatch):
    """Regression-proofed against a broken assertion path: a MagicMock's
    ``.return_value`` set to a real ``@asynccontextmanager`` instance does
    NOT make ``__aenter__`` itself a mock (it stays the real bound method),
    so introspecting via ``fake.__aenter__.return_value`` raises
    AttributeError. Capture the POST call args directly instead, matching
    this file's existing ``capturing_client`` pattern.
    """
    _apply_posthog_settings(monkeypatch, host="https://us.posthog.com")
    from core.services import posthog_admin

    captured = {}
    resp = _make_response(200, {"columns": ["week", "n"], "results": [["2026-08-17", 3]]})

    @asynccontextmanager
    async def capturing_client(*args, **kwargs):
        client = MagicMock()

        async def post(url, headers=None, json=None, **kw):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return resp

        client.post = post
        yield client

    with patch("core.services.posthog_admin.httpx.AsyncClient", new=capturing_client):
        out = await posthog_admin.hogql("select 1")
    assert out == {"columns": ["week", "n"], "results": [["2026-08-17", 3]], "stubbed": False, "error": None}
    assert captured["url"] == "https://us.posthog.com/api/projects/12345/query/"
    assert captured["json"] == {"query": {"kind": "HogQLQuery", "query": "select 1"}}
    assert captured["headers"]["Authorization"] == "Bearer phc_xyz"


@pytest.mark.asyncio
async def test_hogql_403_scope(monkeypatch):
    _apply_posthog_settings(monkeypatch)
    from core.services import posthog_admin

    resp = _make_response(403, text_body='{"detail":"API key missing required scope"}')
    with patch("core.services.posthog_admin.httpx.AsyncClient", return_value=_fake_post_client(resp)):
        out = await posthog_admin.hogql("select 1")
    assert out["error"] == "insufficient_scope" and out["results"] == []


@pytest.mark.asyncio
async def test_hogql_timeout(monkeypatch):
    _apply_posthog_settings(monkeypatch)
    from core.services import posthog_admin

    with patch(
        "core.services.posthog_admin.httpx.AsyncClient",
        return_value=_fake_post_client(httpx.TimeoutException("slow")),
    ):
        out = await posthog_admin.hogql("select 1")
    assert out["error"] == "timeout"


@pytest.mark.asyncio
async def test_hogql_invalid_json_response(monkeypatch):
    _apply_posthog_settings(monkeypatch)
    from core.services import posthog_admin

    resp = _make_response(200, {})
    resp.json = MagicMock(side_effect=ValueError("bad json"))
    with patch("core.services.posthog_admin.httpx.AsyncClient", return_value=_fake_post_client(resp)):
        out = await posthog_admin.hogql("select 1")
    assert out["error"] == "invalid_response"
    assert out["results"] == []


# --- delete_person -------------------------------------------------------------------------------------------


@pytest.fixture
def metrics(monkeypatch):
    seen = []
    monkeypatch.setattr("core.services.posthog_admin.put_metric", lambda name, *a, **k: seen.append(name))
    return seen


def _deploy(monkeypatch):
    _apply_posthog_settings(monkeypatch, host="https://us.posthog.com")
    monkeypatch.setattr("core.config.settings.ENVIRONMENT", "prod")


@pytest.mark.asyncio
async def test_delete_person_posts_the_distinct_id_with_events(monkeypatch, metrics):
    _deploy(monkeypatch)
    from core.services import posthog_admin

    captured = {}
    resp = _make_response(202, {"persons_queued_for_deletion": 1, "deletion_errors": []})

    @asynccontextmanager
    async def capturing_client(*args, **kwargs):
        client = MagicMock()

        async def post(url, headers=None, json=None, **kw):
            captured.update(url=url, headers=headers, json=json, timeout=kwargs.get("timeout"))
            return resp

        client.post = post
        yield client

    with patch("core.services.posthog_admin.httpx.AsyncClient", new=capturing_client):
        await posthog_admin.delete_person("user_a")
    assert captured["url"] == "https://us.posthog.com/api/projects/12345/persons/bulk_delete/"
    assert captured["json"] == {"distinct_ids": ["user_a"], "delete_events": True}
    assert captured["headers"]["Authorization"] == "Bearer phc_xyz"
    assert captured["timeout"] == 5.0
    assert metrics == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        _make_response(404),
        _make_response(202, {"persons_found": 0, "deletion_errors": []}),
    ],
)
async def test_delete_person_treats_no_such_person_as_success(monkeypatch, metrics, result):
    _deploy(monkeypatch)
    from core.services import posthog_admin

    with patch("core.services.posthog_admin.httpx.AsyncClient", return_value=_fake_post_client(result)):
        await posthog_admin.delete_person("user_a")
    assert metrics == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        _make_response(403, text_body="missing scope"),
        _make_response(500),
        _make_response(202, {"deletion_errors": [{"person_uuid": "x", "step": "y"}]}),
        httpx.TimeoutException("slow"),
    ],
)
async def test_delete_person_failures_count_the_metric_and_do_not_raise(monkeypatch, metrics, caplog, result):
    _deploy(monkeypatch)
    from core.services import posthog_admin

    with patch("core.services.posthog_admin.httpx.AsyncClient", return_value=_fake_post_client(result)):
        await posthog_admin.delete_person("user_secret_id")
    assert metrics == ["posthog.person_delete_failed"]
    assert "user_secret_id" not in caplog.text and "missing scope" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("key,env", [("", "prod"), ("phc_xyz", "local")])
async def test_delete_person_calls_nothing_when_not_configured(monkeypatch, metrics, key, env):
    _apply_posthog_settings(monkeypatch, api_key=key)
    monkeypatch.setattr("core.config.settings.ENVIRONMENT", env)
    from core.services import posthog_admin

    with patch("core.services.posthog_admin.httpx.AsyncClient", side_effect=AssertionError("called")):
        await posthog_admin.delete_person("user_a")
    assert metrics == []
