import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.services import container_rpc
from core.services.messaging import ApiProblem

CONTAINER = {"gateway_token": "gw", "status": "running"}

# Captured before any patch runs: the patch target is `httpx.AsyncClient` itself
# (container_rpc's `httpx` is the same module object), so a factory that looked
# it up by name at call time would recursively call itself once patched in.
_RealAsyncClient = httpx.AsyncClient


def _client(handler):
    transport = httpx.MockTransport(handler)
    return lambda **kw: _RealAsyncClient(transport=transport, **kw)


async def _call(handler, **kw):
    manager = MagicMock()
    manager.resolve_running_container = AsyncMock(return_value=(CONTAINER, "10.0.0.9"))
    with (
        patch("core.services.container_rpc.get_ecs_manager", return_value=manager),
        patch("core.services.container_rpc.httpx.AsyncClient", _client(handler)),
    ):
        return await container_rpc.call("owner_1", "agents.list", {"a": 1}, **kw)


@pytest.mark.asyncio
async def test_ok_returns_the_payload_and_posts_the_contract():
    seen = {}

    def handler(request):
        seen["url"], seen["auth"], seen["body"] = (
            str(request.url),
            request.headers["authorization"],
            json.loads(request.content),
        )
        return httpx.Response(200, json={"ok": True, "payload": {"agents": []}})

    assert await _call(handler, timeout_s=7) == {"agents": []}
    assert seen == {
        "url": "http://10.0.0.9:18789/plugins/lightbulb-channel/rpc",
        "auth": "Bearer gw",
        "body": {"method": "agents.list", "params": {"a": 1}, "timeoutMs": 7000},
    }


@pytest.mark.asyncio
async def test_gateway_error_is_a_runtime_error_with_the_error_json():
    error = {"code": "NOT_FOUND", "message": "no such agent"}
    with pytest.raises(RuntimeError) as exc:
        await _call(lambda r: httpx.Response(200, json={"ok": False, "error": error}))
    assert json.loads(str(exc.value)) == error


@pytest.mark.asyncio
async def test_504_is_a_timeout_error():
    with pytest.raises(asyncio.TimeoutError):
        await _call(
            lambda r: httpx.Response(504, json={"ok": False, "error": {"code": "AGENT_TIMEOUT", "message": "slow"}})
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), lambda r: httpx.Response(502, text="bad gateway")],
)
async def test_transport_and_non_contract_answers_are_runtime_unavailable(handler):
    with pytest.raises(ApiProblem) as exc:
        await _call(handler)
    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"
    assert exc.value.error.retryable is True


@pytest.mark.asyncio
async def test_no_running_container_is_runtime_unavailable_before_any_http():
    manager = MagicMock()
    manager.resolve_running_container = AsyncMock(return_value=(None, None))
    with (
        patch("core.services.container_rpc.get_ecs_manager", return_value=manager),
        patch(
            "core.services.container_rpc.container_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={"status": "stopped"},
        ),
        patch("core.services.container_wake.request_wake_silent", new_callable=AsyncMock) as wake,
        pytest.raises(ApiProblem) as exc,
    ):
        await container_rpc.call("owner_1", "health")
    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"
    wake.assert_awaited_once_with("owner_1")


@pytest.mark.asyncio
async def test_wake_false_never_dispatches_a_wake_for_a_stopped_container():
    manager = MagicMock()
    manager.resolve_running_container = AsyncMock(return_value=(None, None))
    with (
        patch("core.services.container_rpc.get_ecs_manager", return_value=manager),
        patch(
            "core.services.container_rpc.container_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={"status": "stopped"},
        ),
        patch("core.services.container_wake.request_wake_silent", new_callable=AsyncMock) as wake,
        pytest.raises(ApiProblem) as exc,
    ):
        await container_rpc.call("owner_1", "health", wake=False)
    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"
    wake.assert_not_awaited()
