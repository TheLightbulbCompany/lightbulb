"""Tests for AdminMetricsMiddleware — admin_api.* metrics emission.

The middleware lives at ``core/middleware/admin_metrics.py``. The
middleware contract under test:

1. Only intercept requests under ``/api/v1/admin/*`` — non-admin paths pass
   through with no metrics emitted.
2. Emit ``admin_api.call_count`` (value=1) per admin request, with dimensions
   ``{endpoint: <path>, admin_user_id: <user-id-or-"unknown">}``.
3. Emit ``admin_api.latency_ms`` per admin request, value = elapsed wall-clock
   time in milliseconds, same dimensions.
4. Emit ``admin_api.errors`` (value=1) only when response status >= 500, with
   dimensions ``{endpoint, code: <status>}``.
5. Always run regardless of response status (including 4xx) for call_count
   + latency.
6. Never raise — if metric emission fails, swallow the error so the request
   does not double-fail.
"""

import asyncio
import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

# Ensure auth settings have a placeholder before any backend import path runs.
os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _build_test_app(*, with_auth_user: str | None = None) -> FastAPI:
    """Build a tiny FastAPI app wired with the (not-yet-implemented) middleware.

    The import of ``AdminMetricsMiddleware`` is intentionally deferred until
    call time so that the ImportError surfaces inside individual tests
    (giving each test a descriptive RED failure).

    If ``with_auth_user`` is set, install a fake auth dependency hook so the
    middleware can extract an admin user id from request state.
    """
    from core.middleware.admin_metrics import AdminMetricsMiddleware  # noqa: WPS433

    app = FastAPI()
    app.add_middleware(AdminMetricsMiddleware)

    if with_auth_user is not None:
        # Stash a fake auth context on request.state so the middleware
        # (whose contract we are designing) can pull admin_user_id out.
        @app.middleware("http")
        async def _inject_auth(request: Request, call_next):  # noqa: WPS430
            request.state.admin_user_id = with_auth_user
            return await call_next(request)

    @app.get("/api/v1/admin/foo")
    async def admin_foo():
        return {"ok": True}

    @app.post("/api/v1/admin/anything")
    async def admin_anything():
        return {"ok": True}

    @app.get("/api/v1/admin/explode")
    async def admin_explode():
        raise RuntimeError("boom")

    @app.get("/api/v1/admin/missing")
    async def admin_missing():
        raise HTTPException(status_code=404, detail="nope")

    @app.get("/api/v1/admin/slow")
    async def admin_slow():
        await asyncio.sleep(0.05)
        return {"ok": True}

    @app.get("/api/v1/public/bar")
    async def public_bar():
        return {"ok": True}

    @app.get("/")
    async def root():
        return {"ok": True}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app


def _all_metric_calls(mock_put_metric) -> list[tuple]:
    """Normalize put_metric mock calls into (name, kwargs) tuples.

    The middleware may call put_metric positionally or via kwargs; this
    helper collapses both into ``(name, merged_kwargs)`` for easy assertion.
    """
    calls = []
    for call in mock_put_metric.call_args_list:
        args, kwargs = call
        merged = dict(kwargs)
        # put_metric(name, value, unit, dimensions) — pull positionals into kw.
        if args:
            merged["name"] = args[0]
            if len(args) > 1:
                merged["value"] = args[1]
            if len(args) > 2:
                merged["unit"] = args[2]
            if len(args) > 3:
                merged["dimensions"] = args[3]
        calls.append((merged.get("name"), merged))
    return calls


def _calls_named(mock_put_metric, name: str) -> list[dict]:
    """Return only the merged-kwarg dicts for calls with the given metric name."""
    return [merged for n, merged in _all_metric_calls(mock_put_metric) if n == name]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_admin_path_emits_call_count_and_latency():
    """A successful admin request emits both call_count and latency_ms."""
    with patch("core.middleware.admin_metrics.put_metric") as mock_put:
        app = _build_test_app()
        client = TestClient(app)
        res = client.get("/api/v1/admin/foo")

    assert res.status_code == 200
    count_calls = _calls_named(mock_put, "admin_api.call_count")
    latency_calls = _calls_named(mock_put, "admin_api.latency_ms")
    assert len(count_calls) == 1, f"expected 1 call_count emission, got {count_calls}"
    assert len(latency_calls) == 1, f"expected 1 latency emission, got {latency_calls}"
    # Both should carry the endpoint dimension.
    for merged in (count_calls[0], latency_calls[0]):
        dims = merged.get("dimensions") or {}
        assert dims.get("endpoint") == "/api/v1/admin/foo"


def test_non_admin_path_does_not_emit_metrics():
    """Non-admin paths must pass through without any put_metric calls."""
    with patch("core.middleware.admin_metrics.put_metric") as mock_put:
        app = _build_test_app()
        client = TestClient(app)
        res = client.get("/api/v1/public/bar")

    assert res.status_code == 200
    assert mock_put.call_count == 0, f"expected no metrics, got {mock_put.call_args_list}"


def test_5xx_response_emits_errors_metric():
    """A handler that raises (->500) emits admin_api.errors with code='500'."""
    with patch("core.middleware.admin_metrics.put_metric") as mock_put:
        app = _build_test_app()
        # raise_server_exceptions=False so TestClient surfaces the 500 instead
        # of re-raising the underlying RuntimeError.
        client = TestClient(app, raise_server_exceptions=False)
        res = client.get("/api/v1/admin/explode")

    assert res.status_code == 500
    error_calls = _calls_named(mock_put, "admin_api.errors")
    assert len(error_calls) == 1, f"expected 1 errors emission, got {error_calls}"
    dims = error_calls[0].get("dimensions") or {}
    assert dims.get("endpoint") == "/api/v1/admin/explode"
    assert str(dims.get("code")) == "500"
    # Errors path should still emit count + latency.
    assert len(_calls_named(mock_put, "admin_api.call_count")) == 1
    assert len(_calls_named(mock_put, "admin_api.latency_ms")) == 1


def test_4xx_response_does_not_emit_errors_metric():
    """4xx is a client problem, not a server error — only count+latency emit."""
    with patch("core.middleware.admin_metrics.put_metric") as mock_put:
        app = _build_test_app()
        client = TestClient(app)
        res = client.get("/api/v1/admin/missing")

    assert res.status_code == 404
    assert len(_calls_named(mock_put, "admin_api.call_count")) == 1
    assert len(_calls_named(mock_put, "admin_api.latency_ms")) == 1
    assert _calls_named(mock_put, "admin_api.errors") == []


def test_metric_dimensions_include_endpoint_path():
    """All admin metrics must carry the request path under `endpoint`."""
    with patch("core.middleware.admin_metrics.put_metric") as mock_put:
        app = _build_test_app()
        client = TestClient(app)
        res = client.get("/api/v1/admin/foo")

    assert res.status_code == 200
    for _name, merged in _all_metric_calls(mock_put):
        dims = merged.get("dimensions") or {}
        assert dims.get("endpoint") == "/api/v1/admin/foo", f"missing/incorrect endpoint dimension on {_name}: {dims}"


def test_metric_emission_failure_does_not_break_request():
    """If put_metric blows up, the request must still return 200 OK."""
    with patch(
        "core.middleware.admin_metrics.put_metric",
        side_effect=RuntimeError("cloudwatch unavailable"),
    ):
        app = _build_test_app()
        client = TestClient(app)
        res = client.get("/api/v1/admin/foo")

    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_latency_metric_value_reflects_request_duration():
    """A handler that sleeps 50ms should produce a latency metric >= 50ms."""
    with patch("core.middleware.admin_metrics.put_metric") as mock_put:
        app = _build_test_app()
        client = TestClient(app)
        res = client.get("/api/v1/admin/slow")

    assert res.status_code == 200
    latency_calls = _calls_named(mock_put, "admin_api.latency_ms")
    assert len(latency_calls) == 1
    value = latency_calls[0].get("value")
    assert value is not None, f"latency call missing value: {latency_calls[0]}"
    assert float(value) >= 50.0, f"expected latency >= 50ms, got {value}"


def test_admin_user_id_dimension_when_authenticated():
    """When request.state.admin_user_id is set, dimensions include that ID."""
    with patch("core.middleware.admin_metrics.put_metric") as mock_put:
        app = _build_test_app(with_auth_user="user_admin_42")
        client = TestClient(app)
        res = client.get("/api/v1/admin/foo")

    assert res.status_code == 200
    count_calls = _calls_named(mock_put, "admin_api.call_count")
    assert len(count_calls) == 1
    dims = count_calls[0].get("dimensions") or {}
    assert dims.get("admin_user_id") == "user_admin_42"


def test_admin_user_id_defaults_to_unknown_without_auth_context():
    """No auth context → dimension is the literal string ``unknown``."""
    with patch("core.middleware.admin_metrics.put_metric") as mock_put:
        app = _build_test_app()  # no auth user injected
        client = TestClient(app)
        res = client.get("/api/v1/admin/foo")

    assert res.status_code == 200
    count_calls = _calls_named(mock_put, "admin_api.call_count")
    assert len(count_calls) == 1
    dims = count_calls[0].get("dimensions") or {}
    assert dims.get("admin_user_id") == "unknown"


@pytest.mark.parametrize("path", ["/", "/healthz"])
def test_does_not_intercept_health_check_paths(path):
    """Root and health-check paths must not produce admin metrics."""
    with patch("core.middleware.admin_metrics.put_metric") as mock_put:
        app = _build_test_app()
        client = TestClient(app)
        res = client.get(path)

    assert res.status_code == 200
    assert mock_put.call_count == 0


def test_post_endpoints_also_emit_metrics():
    """Metrics are method-agnostic — POST under /admin must also emit."""
    with patch("core.middleware.admin_metrics.put_metric") as mock_put:
        app = _build_test_app()
        client = TestClient(app)
        res = client.post("/api/v1/admin/anything", json={})

    assert res.status_code == 200
    assert len(_calls_named(mock_put, "admin_api.call_count")) == 1
    assert len(_calls_named(mock_put, "admin_api.latency_ms")) == 1
