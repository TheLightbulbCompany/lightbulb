"""Tests for browser_state — Lightbulb AI-side browser session persistence.

Snapshot (on a container roll) reads cookies + active-tab localStorage from the
running container over the ``browser.request`` gateway RPC and writes a small
JSON to EFS; restore (on the readiness poll, once ``ready``) applies it back and
consumes the file. Both are strictly best-effort: never raise, no-op cleanly
when there's nothing to do.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.services.browser_state as bs
from core.containers.workspace import WorkspaceError


def _ecs(container, ip):
    m = MagicMock()
    m.resolve_running_container = AsyncMock(return_value=(container, ip))
    return m


def _pool_from_routes(routes):
    """Pool whose send_rpc dispatches on the browser.request path, recording
    every call. ``routes`` maps (METHOD, path) -> return value or Exception."""
    calls = []

    async def _send_rpc(*, user_id, req_id, method, params, ip, token, timeout=None):
        assert method == "browser.request"
        key = (params["method"], params["path"])
        calls.append({"key": key, "params": params})
        val = routes.get(key)
        if isinstance(val, Exception):
            raise val
        return val

    pool = MagicMock()
    pool.send_rpc = AsyncMock(side_effect=_send_rpc)
    pool._calls = calls
    return pool


def _workspace():
    ws = MagicMock()
    ws.write_file = MagicMock()
    ws.delete_file = MagicMock()
    ws.read_file = MagicMock()
    return ws


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_cold_container_is_noop():
    ws = _workspace()
    with (
        patch.object(bs, "get_ecs_manager", return_value=_ecs(None, None)),
        patch.object(bs, "get_gateway_pool") as pool,
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        assert await bs.snapshot_browser_state("org_1") is False
        pool.assert_not_called()
        ws.write_file.assert_not_called()


@pytest.mark.asyncio
async def test_snapshot_writes_cookies_and_local():
    cookies = [{"name": "sid", "value": "abc", "domain": ".x.com", "path": "/", "expires": -1}]
    routes = {
        ("GET", "/cookies"): {"ok": True, "targetId": "t", "cookies": cookies},
        ("GET", "/storage/local"): {"ok": True, "targetId": "t", "values": {"k": "v"}},
    }
    pool = _pool_from_routes(routes)
    ws = _workspace()
    with (
        patch.object(bs, "get_ecs_manager", return_value=_ecs({"gateway_token": "tok"}, "10.0.0.9")),
        patch.object(bs, "get_gateway_pool", return_value=pool),
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        assert await bs.snapshot_browser_state("org_1") is True

    ws.write_file.assert_called_once()
    owner, path, content = ws.write_file.call_args.args
    assert owner == "org_1"
    assert path == bs.STATE_PATH
    saved = json.loads(content)
    assert saved["cookies"] == cookies
    assert saved["local"] == {"k": "v"}
    # Session cookie (expires: -1) is persisted verbatim — no expires mangling.
    assert saved["cookies"][0]["expires"] == -1


@pytest.mark.asyncio
async def test_snapshot_empty_capture_does_not_clobber():
    # Browser reachable but no cookies and empty localStorage → write nothing,
    # so a prior good snapshot survives a subsequent roll before restore.
    routes = {
        ("GET", "/cookies"): {"ok": True, "cookies": []},
        ("GET", "/storage/local"): {"ok": True, "values": {}},
    }
    ws = _workspace()
    with (
        patch.object(bs, "get_ecs_manager", return_value=_ecs({"gateway_token": "t"}, "10.0.0.1")),
        patch.object(bs, "get_gateway_pool", return_value=_pool_from_routes(routes)),
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        assert await bs.snapshot_browser_state("org_1") is False
    ws.write_file.assert_not_called()


@pytest.mark.asyncio
async def test_snapshot_partial_when_one_get_fails():
    # localStorage GET raises; cookies still captured and written.
    routes = {
        ("GET", "/cookies"): {"ok": True, "cookies": [{"name": "a", "value": "b"}]},
        ("GET", "/storage/local"): RuntimeError("storage boom"),
    }
    ws = _workspace()
    with (
        patch.object(bs, "get_ecs_manager", return_value=_ecs({"gateway_token": "t"}, "10.0.0.1")),
        patch.object(bs, "get_gateway_pool", return_value=_pool_from_routes(routes)),
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        assert await bs.snapshot_browser_state("org_1") is True
    saved = json.loads(ws.write_file.call_args.args[2])
    assert "cookies" in saved and "local" not in saved


@pytest.mark.asyncio
async def test_snapshot_both_gets_fail_is_noop():
    routes = {
        ("GET", "/cookies"): RuntimeError("down"),
        ("GET", "/storage/local"): RuntimeError("down"),
    }
    ws = _workspace()
    with (
        patch.object(bs, "get_ecs_manager", return_value=_ecs({"gateway_token": "t"}, "10.0.0.1")),
        patch.object(bs, "get_gateway_pool", return_value=_pool_from_routes(routes)),
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        assert await bs.snapshot_browser_state("org_1") is False
    ws.write_file.assert_not_called()


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_no_snapshot_is_fast_noop():
    ws = _workspace()
    ws.read_file.side_effect = WorkspaceError("not found")
    with (
        patch.object(bs, "get_gateway_pool") as pool,
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        assert await bs.restore_browser_state("org_1", "10.0.0.1", "tok") is False
        pool.assert_not_called()
        ws.delete_file.assert_not_called()


@pytest.mark.asyncio
async def test_restore_applies_then_consumes():
    state = {
        "v": 1,
        "cookies": [
            {"name": "sid", "value": "abc", "domain": ".x.com", "path": "/", "expires": -1},
            {"name": "csrf", "value": "z", "domain": ".x.com", "path": "/"},
        ],
        "local": {"theme": "dark"},
    }
    routes = {
        ("POST", "/cookies/set"): {"ok": True},
        ("POST", "/storage/local/set"): {"ok": True},
    }
    pool = _pool_from_routes(routes)
    ws = _workspace()
    ws.read_file.return_value = json.dumps(state)
    with (
        patch.object(bs, "get_gateway_pool", return_value=pool),
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        assert await bs.restore_browser_state("org_1", "10.0.0.1", "tok") is True

    cookie_sets = [c for c in pool._calls if c["key"] == ("POST", "/cookies/set")]
    local_sets = [c for c in pool._calls if c["key"] == ("POST", "/storage/local/set")]
    assert len(cookie_sets) == 2
    assert len(local_sets) == 1
    # Each cookie is wrapped in {"cookie": ...}; session cookie preserved.
    assert cookie_sets[0]["params"]["body"]["cookie"]["expires"] == -1
    assert local_sets[0]["params"]["body"] == {"key": "theme", "value": "dark"}
    # Consume-once: the file is deleted after applying.
    ws.delete_file.assert_called_once_with("org_1", bs.STATE_PATH)


@pytest.mark.asyncio
async def test_restore_corrupt_json_is_discarded():
    ws = _workspace()
    ws.read_file.return_value = "{not json"
    with (
        patch.object(bs, "get_gateway_pool") as pool,
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        assert await bs.restore_browser_state("org_1", "10.0.0.1", "tok") is False
        pool.assert_not_called()
        ws.delete_file.assert_called_once_with("org_1", bs.STATE_PATH)


@pytest.mark.asyncio
async def test_restore_is_best_effort_per_cookie():
    # First cookie POST raises; restore continues and still consumes the file.
    state = {"cookies": [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}]}
    routes = {("POST", "/cookies/set"): RuntimeError("rejected")}
    pool = _pool_from_routes(routes)
    ws = _workspace()
    ws.read_file.return_value = json.dumps(state)
    with (
        patch.object(bs, "get_gateway_pool", return_value=pool),
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        # Zero applied → returns False, but still deletes (reachable, best shot).
        assert await bs.restore_browser_state("org_1", "10.0.0.1", "tok") is False
    assert len([c for c in pool._calls if c["key"] == ("POST", "/cookies/set")]) == 2
    ws.delete_file.assert_called_once()
