from unittest.mock import AsyncMock, patch

import pytest

from core.services import runtime_rpc as rpc


async def _run(method, params=None, *, call=None):
    with (
        patch(
            "core.services.runtime_rpc.container_rpc.call", new_callable=AsyncMock, return_value={"ok": True}
        ) as rpc_call,
        patch("core.services.runtime_rpc.publish_owner_users", new_callable=AsyncMock) as publish,
        # This suite is about the invalidation-publish behaviour, exercised over the ECS
        # (forward-to-container) path — an AgentCore row's config.get/config.patch are served
        # from S3 instead (see test_runtime_rpc_hermes_config.py, Task 13a).
        patch("core.services.runtime_rpc._is_agentcore", new_callable=AsyncMock, return_value=False),
    ):
        if call is not None:
            rpc_call.side_effect = call
        result = await rpc.execute_sync(
            owner_id="org_1", user_id="user_1", request_id="r", method=method, params=params or {}
        )
    return result, rpc_call, publish


@pytest.mark.asyncio
async def test_mutating_config_call_publishes_invalidation():
    result, call, publish = await _run("config.patch", {"raw": "{}"})
    assert result == {"ok": True}
    call.assert_awaited_once_with("org_1", "config.patch", {"raw": "{}"}, timeout_s=rpc._RPC_TIMEOUT_S)
    event = publish.await_args.args[1]
    assert (publish.await_args.args[0], event["type"], event["payload"]) == (
        "org_1",
        "runtime.resource.invalidated",
        {"resource": "config"},
    )


@pytest.mark.asyncio
async def test_read_call_publishes_nothing():
    _, _, publish = await _run("config.get")
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_failure_does_not_fail_the_rpc():
    with patch(
        "core.services.runtime_rpc.publish_owner_users", new_callable=AsyncMock, side_effect=RuntimeError("appsync")
    ):
        with patch("core.services.runtime_rpc.container_rpc.call", new_callable=AsyncMock, return_value=1):
            assert (
                await rpc.execute_sync(
                    owner_id="org_1", user_id="user_1", request_id="r", method="skills.install", params={"name": "x"}
                )
                == 1
            )
