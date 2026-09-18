"""Tests for browser_state — isol8-side browser session persistence.

Snapshot (on a container roll) reads cookies + active-tab localStorage from the
running container over the ``browser.request`` gateway RPC and writes a small
JSON to EFS; restore (on the readiness poll, once ``ready``) applies it back and
consumes the file. Both are strictly best-effort: never raise, no-op cleanly
when there's nothing to do.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.services.browser_state as bs
from core.containers.workspace import WorkspaceError
from core.services.messaging import ApiProblem


def _running_container_mock(container, ip):
    """container_rpc.running_container stand-in: raises for a cold container
    (mirroring the real ApiProblem it raises), resolves otherwise. The snapshot
    pre-check only cares whether this raises, not the returned tuple."""
    if container is None or ip is None:
        return AsyncMock(side_effect=ApiProblem("RUNTIME_UNAVAILABLE", "no container", retryable=True))
    return AsyncMock(return_value=(container, ip))


def _call_from_routes(routes):
    """container_rpc.call stand-in that dispatches on the browser.request path,
    recording every call. ``routes`` maps (METHOD, path) -> return value or Exception."""
    calls = []

    async def _call(owner_id, method, params, **kwargs):
        assert method == "browser.request"
        key = (params["method"], params["path"])
        calls.append({"key": key, "params": params, "kwargs": kwargs})
        val = routes.get(key)
        if isinstance(val, Exception):
            raise val
        return val

    mock = AsyncMock(side_effect=_call)
    mock._calls = calls
    return mock


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
        patch.object(bs.container_rpc, "running_container", _running_container_mock(None, None)),
        patch.object(bs.container_rpc, "call") as call,
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        assert await bs.snapshot_browser_state("org_1") is False
        call.assert_not_called()
        ws.write_file.assert_not_called()


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
        patch.object(
            bs.container_rpc, "running_container", _running_container_mock({"gateway_token": "t"}, "10.0.0.1")
        ),
        patch.object(bs.container_rpc, "call", _call_from_routes(routes)),
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        assert await bs.snapshot_browser_state("org_1") is False
    ws.write_file.assert_not_called()


@pytest.mark.asyncio
async def test_snapshot_both_gets_fail_is_noop():
    routes = {
        ("GET", "/cookies"): RuntimeError("down"),
        ("GET", "/storage/local"): RuntimeError("down"),
    }
    ws = _workspace()
    with (
        patch.object(
            bs.container_rpc, "running_container", _running_container_mock({"gateway_token": "t"}, "10.0.0.1")
        ),
        patch.object(bs.container_rpc, "call", _call_from_routes(routes)),
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        assert await bs.snapshot_browser_state("org_1") is False
    ws.write_file.assert_not_called()


@pytest.mark.asyncio
async def test_snapshot_agentcore_owner_is_noop_before_any_rpc():
    """A pre-migration snapshot leaves a stale EFS file behind; the container's
    real browser.request stub would otherwise be invoked at the AgentCore
    ingress floor (90s) for nothing. Refuse before touching container_rpc."""
    ws = _workspace()
    with (
        patch.object(bs.runtime_transport, "is_agentcore_owner", AsyncMock(return_value=True)) as is_ac,
        patch.object(bs.container_rpc, "running_container") as running,
        patch.object(bs.container_rpc, "call") as call,
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        assert await bs.snapshot_browser_state("org_1") is False
    is_ac.assert_awaited_once_with("org_1")
    running.assert_not_called()
    call.assert_not_called()
    ws.write_file.assert_not_called()


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_no_snapshot_is_fast_noop():
    ws = _workspace()
    ws.read_file.side_effect = WorkspaceError("not found")
    with (
        patch.object(bs.container_rpc, "call") as call,
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        assert await bs.restore_browser_state("org_1") is False
        call.assert_not_called()
        ws.delete_file.assert_not_called()


@pytest.mark.asyncio
async def test_restore_agentcore_owner_is_noop_before_any_rpc():
    """Up to _MAX_COOKIES sequential 90s runtime invokes each hitting the
    bridge's browser.request stub is the exact defect this refusal kills --
    it must fire before even reading the EFS snapshot."""
    ws = _workspace()
    with (
        patch.object(bs.runtime_transport, "is_agentcore_owner", AsyncMock(return_value=True)) as is_ac,
        patch.object(bs.container_rpc, "running_container") as running,
        patch.object(bs.container_rpc, "call") as call,
        patch.object(bs, "get_workspace", return_value=ws),
    ):
        assert await bs.restore_browser_state("org_1") is False
    is_ac.assert_awaited_once_with("org_1")
    running.assert_not_called()
    call.assert_not_called()
    ws.read_file.assert_not_called()
    ws.delete_file.assert_not_called()
