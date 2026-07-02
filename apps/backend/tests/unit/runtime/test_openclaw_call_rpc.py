from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.runtime.base import ContainerNotRunningError
from core.runtime.openclaw.adapter import OpenClawRuntime


def _mock_ecs(container, ip):
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(container, ip))
    return ecs


@pytest.mark.asyncio
async def test_call_rpc_pooled_path():
    rt = OpenClawRuntime()
    pool = MagicMock()
    pool.send_rpc = AsyncMock(return_value={"ok": True})
    container = {"gateway_token": "tok-1"}
    with (
        patch("core.runtime.openclaw.adapter._get_pool", return_value=pool),
        patch(
            "core.runtime.openclaw.adapter._get_ecs",
            return_value=_mock_ecs(container, "10.0.0.5"),
        ),
    ):
        result = await rt.call_rpc("org_1", "channels.status", {"probe": True})
    assert result == {"ok": True}
    kwargs = pool.send_rpc.call_args.kwargs
    assert kwargs["user_id"] == "org_1"
    assert kwargs["method"] == "channels.status"
    assert kwargs["ip"] == "10.0.0.5"
    assert kwargs["token"] == "tok-1"
    assert kwargs["req_id"]  # generated, non-empty


@pytest.mark.asyncio
async def test_call_rpc_fresh_path_uses_oneshot():
    rt = OpenClawRuntime()
    container = {"gateway_token": "tok-1"}
    with (
        patch(
            "core.runtime.openclaw.adapter._get_ecs",
            return_value=_mock_ecs(container, "10.0.0.5"),
        ),
        patch(
            "core.runtime.openclaw.adapter.gateway_rpc_oneshot",
            new=AsyncMock(return_value={"restarted": True}),
        ) as oneshot,
    ):
        result = await rt.call_rpc("org_1", "gateway.restart", fresh=True, timeout=5)
    assert result == {"restarted": True}
    assert oneshot.call_args.kwargs["owner_id"] == "org_1"
    assert oneshot.call_args.kwargs["token"] == "tok-1"
    assert oneshot.call_args.kwargs["open_timeout"] == 5


@pytest.mark.asyncio
async def test_call_rpc_pooled_with_timeout_raises():
    # The guard fires before container resolution, so no mocks are needed.
    rt = OpenClawRuntime()
    with pytest.raises(ValueError, match="fresh=True"):
        await rt.call_rpc("org_1", "channels.status", timeout=5)


@pytest.mark.asyncio
async def test_call_rpc_cold_container_raises():
    rt = OpenClawRuntime()
    with patch("core.runtime.openclaw.adapter._get_ecs", return_value=_mock_ecs(None, None)):
        with pytest.raises(ContainerNotRunningError):
            await rt.call_rpc("org_1", "anything")


@pytest.mark.asyncio
async def test_call_rpc_missing_token_raises_container_not_running():
    # A container row without a gateway token is unreachable — same caller
    # contract as a cold container.
    rt = OpenClawRuntime()
    container = {"service_name": "openclaw-org_1-abc"}  # row exists, token key absent
    with patch(
        "core.runtime.openclaw.adapter._get_ecs",
        return_value=_mock_ecs(container, "10.0.0.5"),
    ):
        with pytest.raises(ContainerNotRunningError):
            await rt.call_rpc("org_1", "anything")


@pytest.mark.asyncio
async def test_call_rpc_none_token_raises_container_not_running():
    rt = OpenClawRuntime()
    container = {"gateway_token": None}
    with patch(
        "core.runtime.openclaw.adapter._get_ecs",
        return_value=_mock_ecs(container, "10.0.0.5"),
    ):
        with pytest.raises(ContainerNotRunningError):
            await rt.call_rpc("org_1", "anything")


@pytest.mark.asyncio
async def test_revoke_provider_never_raises_and_returns_false_when_cold():
    rt = OpenClawRuntime()
    with patch("core.runtime.openclaw.adapter._get_ecs", return_value=_mock_ecs(None, None)):
        assert await rt.revoke_provider("org_1", provider="openai") is False
