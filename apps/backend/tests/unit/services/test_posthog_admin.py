"""The PostHog admin client: saved Endpoints for the dashboard (`endpoint`) and person deletion (`delete_person`).

httpx.AsyncClient is replaced with a stand-in rather than respx: respx 0.20.2 does not match URLs under newer httpx.
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


class _Scripted:
    """An httpx.AsyncClient stand-in: answers each request from `script[(method, path after /endpoints/)]` (a list is
    used up in order) and records what was asked."""

    def __init__(self, script):
        self.script, self.calls, self.kwargs = script, [], {}

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, json=None):
        path = url.split("/endpoints/")[1]
        self.calls.append((method, path, json))
        answer = self.script[(method, path)]
        answer = answer.pop(0) if isinstance(answer, list) else answer
        if isinstance(answer, BaseException):
            raise answer
        return answer


SQL = "select 1"
NAME = "spark_prod_installs_32320039"  # stem + "_" + the first 8 hex digits of sha1(SQL)
ROWS = {
    "columns": ["week", "n"],
    "results": [["2026-08-17", 3]],
    "endpoint_materialized_at": "2026-11-26T16:15:03+00:00",
}


async def _endpoint(monkeypatch, script, **settings):
    _apply_posthog_settings(monkeypatch, host="https://us.posthog.com", **settings)
    from core.services import posthog_admin

    client = _Scripted(script)
    with patch("core.services.posthog_admin.httpx.AsyncClient", client):
        return await posthog_admin.endpoint("spark_prod_installs", SQL, timeout_s=7.0), client


@pytest.mark.asyncio
async def test_endpoint_stubbed_without_key(monkeypatch):
    out, client = await _endpoint(monkeypatch, {}, api_key="")
    assert out["stubbed"] is True and out["error"] is None and out["results"] == [] and client.calls == []


@pytest.mark.asyncio
async def test_endpoint_runs_the_saved_query_by_name(monkeypatch):
    import hashlib

    assert NAME == "spark_prod_installs_" + hashlib.sha1(SQL.encode()).hexdigest()[:8]
    out, client = await _endpoint(monkeypatch, {("POST", f"{NAME}/run/"): _make_response(200, ROWS)})
    assert out == {
        "columns": ["week", "n"],
        "results": [["2026-08-17", 3]],
        "as_of": "2026-11-26T16:15:03+00:00",
        "stubbed": False,
        "error": None,
    }
    assert client.calls == [("POST", f"{NAME}/run/", {})]  # nothing is published or listed when the endpoint exists
    assert client.kwargs["headers"] == {"Authorization": "Bearer phc_xyz"} and client.kwargs["timeout"] == 7.0


@pytest.mark.asyncio
async def test_endpoint_as_of_is_the_cache_time_when_not_materialized_yet(monkeypatch):
    body = {"columns": [], "results": [], "last_refresh": "2026-11-26T16:15:20Z"}
    out, _ = await _endpoint(monkeypatch, {("POST", f"{NAME}/run/"): _make_response(200, body)})
    assert out["as_of"] == "2026-11-26T16:15:20Z"


@pytest.mark.asyncio
async def test_endpoint_publishes_a_missing_query_and_retires_its_older_versions_first(monkeypatch):
    """The name carries a hash of the SQL, so a 404 means this query was never published. Older endpoints of the same
    stem go first: a materialized endpoint is recomputed on a schedule until it is deleted."""
    listing = {
        "results": [
            {"name": "spark_prod_installs_0000aaaa"},  # the query before this one
            {"name": "spark_prod_installs_weekly_0000bbbb"},  # another query whose stem starts the same
            {"name": "spark_dev_installs_0000cccc"},  # the dev backend's
            {"name": "someone_elses"},
        ]
    }
    script = {
        ("POST", f"{NAME}/run/"): [_make_response(404), _make_response(200, ROWS)],
        ("GET", ""): _make_response(200, listing),
        ("DELETE", "spark_prod_installs_0000aaaa/"): _make_response(204),
        ("POST", ""): _make_response(201, {"name": NAME}),
    }
    out, client = await _endpoint(monkeypatch, script)
    assert out["error"] is None and out["results"] == [["2026-08-17", 3]]
    assert [c[:2] for c in client.calls] == [
        ("POST", f"{NAME}/run/"),
        ("GET", ""),
        ("DELETE", "spark_prod_installs_0000aaaa/"),
        ("POST", ""),
        ("POST", f"{NAME}/run/"),
    ]
    created = client.calls[3][2]
    assert created["name"] == NAME and created["query"] == {"kind": "HogQLQuery", "query": SQL}
    assert created["is_materialized"] is True and created["data_freshness_seconds"] == 900


@pytest.mark.asyncio
async def test_endpoint_runs_again_when_another_task_published_it_first(monkeypatch):
    script = {
        ("POST", f"{NAME}/run/"): [_make_response(404), _make_response(200, ROWS)],
        ("GET", ""): _make_response(200, {"results": []}),
        ("POST", ""): _make_response(400, text_body="An endpoint with this name already exists"),
    }
    out, _ = await _endpoint(monkeypatch, script)
    assert out["error"] is None and out["results"] == [["2026-08-17", 3]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer, error",
    [
        (_make_response(403, text_body='{"detail":"API key missing required scope"}'), "insufficient_scope"),
        (_make_response(429, text_body="Too many concurrent requests"), "http_429"),
        (_make_response(503, text_body="query_capacity"), "http_503"),
        (httpx.TimeoutException("slow"), "timeout"),
        (_make_response(200, {"results": [[1]], "hasMore": True}), "truncated"),
    ],
)
async def test_endpoint_failures_are_named_and_carry_no_rows(monkeypatch, answer, error):
    out, _ = await _endpoint(monkeypatch, {("POST", f"{NAME}/run/"): answer})
    assert out["error"] == error and out["results"] == [] and out["stubbed"] is False


@pytest.mark.asyncio
async def test_endpoint_invalid_json_response(monkeypatch):
    resp = _make_response(200, {})
    resp.json = MagicMock(side_effect=ValueError("bad json"))
    out, _ = await _endpoint(monkeypatch, {("POST", f"{NAME}/run/"): resp})
    assert out["error"] == "invalid_response" and out["results"] == []


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


@pytest.mark.asyncio
async def test_delete_person_flushes_queued_events_before_the_delete(monkeypatch, metrics):
    _deploy(monkeypatch)
    from core.observability import product_analytics
    from core.services import posthog_admin

    order = []
    monkeypatch.setattr(product_analytics, "flush", lambda: order.append("flush"))

    @asynccontextmanager
    async def client(*args, **kwargs):
        c = MagicMock()

        async def post(*a, **k):
            order.append("delete")
            return _make_response(202, {"deletion_errors": []})

        c.post = post
        yield c

    with patch("core.services.posthog_admin.httpx.AsyncClient", new=client):
        await posthog_admin.delete_person("user_a")
    assert order == ["flush", "delete"]
