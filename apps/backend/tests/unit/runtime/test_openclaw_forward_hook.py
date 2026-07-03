"""``OpenClawRuntime.forward_hook`` — webhook forwarding mechanics.

The adapter owns container resolution, the container-side
``Authorization: Bearer <hook_token>`` attachment, the URL build against the
gateway's ``/hooks`` ingress, and the POST. Edge auth / rate limiting / audit
stay in ``routers/hooks_proxy.py`` — these tests cover only the mechanics.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.runtime.base import ContainerNotRunningError
from core.runtime.openclaw.adapter import OpenClawRuntime, _hook_client_factory


def _mock_ecs(container, ip):
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(container, ip))
    return ecs


def _stub_transport(seen: dict, response: httpx.Response | None = None):
    class StubTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            seen["body"] = request.content
            return response if response is not None else httpx.Response(200, content=b"{}")

    return StubTransport()


@pytest.mark.asyncio
async def test_forward_hook_posts_to_container_hooks_path():
    rt = OpenClawRuntime()
    container = {"gateway_token": "tok", "hook_token": "hook-tok"}
    seen: dict = {}
    with (
        patch(
            "core.runtime.openclaw.adapter._get_ecs",
            return_value=_mock_ecs(container, "10.0.0.9"),
        ),
        patch(
            "core.runtime.openclaw.adapter._hook_client_factory",
            lambda: httpx.AsyncClient(transport=_stub_transport(seen)),
        ),
    ):
        status, headers, body = await rt.forward_hook("org_1", "telegram", {"x-h": "1"}, b'{"a":1}')
    assert status == 200
    assert body == b"{}"
    assert seen["url"] == "http://10.0.0.9:18789/hooks/telegram"
    # Container-side auth is the row's hook_token — NOT the gateway token.
    assert seen["headers"]["authorization"] == "Bearer hook-tok"
    assert seen["headers"]["x-h"] == "1"
    assert seen["body"] == b'{"a":1}'


@pytest.mark.asyncio
async def test_forward_hook_query_string_rides_the_path():
    # Callers splice the raw query into ``path`` — webhook senders carry
    # signature/verification material there, so it must reach the container.
    rt = OpenClawRuntime()
    container = {"hook_token": "hook-tok"}
    seen: dict = {}
    with (
        patch(
            "core.runtime.openclaw.adapter._get_ecs",
            return_value=_mock_ecs(container, "10.0.0.9"),
        ),
        patch(
            "core.runtime.openclaw.adapter._hook_client_factory",
            lambda: httpx.AsyncClient(transport=_stub_transport(seen)),
        ),
    ):
        await rt.forward_hook("org_1", "wake?challenge=abc&signature=xyz", {}, b"{}")
    assert seen["url"] == "http://10.0.0.9:18789/hooks/wake?challenge=abc&signature=xyz"


@pytest.mark.asyncio
async def test_forward_hook_strips_hop_by_hop_response_headers():
    rt = OpenClawRuntime()
    container = {"hook_token": "hook-tok"}
    upstream = httpx.Response(
        201,
        content=b'{"queued":true}',
        headers={"content-type": "application/json", "connection": "keep-alive"},
    )
    with (
        patch(
            "core.runtime.openclaw.adapter._get_ecs",
            return_value=_mock_ecs(container, "10.0.0.9"),
        ),
        patch(
            "core.runtime.openclaw.adapter._hook_client_factory",
            lambda: httpx.AsyncClient(transport=_stub_transport({}, upstream)),
        ),
    ):
        status, headers, body = await rt.forward_hook("org_1", "wake", {}, b"{}")
    assert status == 201
    assert body == b'{"queued":true}'
    assert headers["content-type"] == "application/json"
    lowered = {k.lower() for k in headers}
    assert not lowered & {"content-length", "transfer-encoding", "connection"}


@pytest.mark.asyncio
async def test_forward_hook_cold_container_raises():
    rt = OpenClawRuntime()
    with patch("core.runtime.openclaw.adapter._get_ecs", return_value=_mock_ecs(None, None)):
        with pytest.raises(ContainerNotRunningError):
            await rt.forward_hook("org_1", "wake", {}, b"{}")


@pytest.mark.asyncio
async def test_forward_hook_row_without_task_ip_raises():
    rt = OpenClawRuntime()
    container = {"hook_token": "hook-tok"}
    with patch("core.runtime.openclaw.adapter._get_ecs", return_value=_mock_ecs(container, None)):
        with pytest.raises(ContainerNotRunningError):
            await rt.forward_hook("org_1", "wake", {}, b"{}")


@pytest.mark.asyncio
async def test_forward_hook_missing_hook_token_raises():
    # A row without a hook token is as unforwardable as a cold container.
    rt = OpenClawRuntime()
    container = {"gateway_token": "tok"}
    with patch(
        "core.runtime.openclaw.adapter._get_ecs",
        return_value=_mock_ecs(container, "10.0.0.9"),
    ):
        with pytest.raises(ContainerNotRunningError):
            await rt.forward_hook("org_1", "wake", {}, b"{}")


@pytest.mark.asyncio
async def test_forward_hook_unreachable_upstream_returns_502_tuple():
    rt = OpenClawRuntime()
    container = {"hook_token": "hook-tok"}

    class DeadTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

    with (
        patch(
            "core.runtime.openclaw.adapter._get_ecs",
            return_value=_mock_ecs(container, "10.0.0.9"),
        ),
        patch(
            "core.runtime.openclaw.adapter._hook_client_factory",
            lambda: httpx.AsyncClient(transport=DeadTransport()),
        ),
    ):
        status, headers, body = await rt.forward_hook("org_1", "wake", {}, b"{}")
    assert status == 502
    assert headers == {}
    assert body == b'{"detail":"upstream_unreachable"}'


def test_hook_client_factory_uses_thirty_second_timeout():
    assert _hook_client_factory().timeout == httpx.Timeout(30.0)
