"""Tests for relay_hooks: the per-connection side effects moved out of
node_proxy.py / operator_proxy.py / routers/websocket_chat.py for the
gateway relay handler to call."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import relay_hooks


@pytest.mark.asyncio
async def test_operator_wake_gate_refuses_only_gated_methods():
    with patch(
        "core.services.relay_hooks.chat_gate.gate_chat",
        new_callable=AsyncMock,
        return_value={"blocked": True, "code": "TRIAL_EXPIRED", "message": "Trial over"},
    ):
        refusal = await relay_hooks.admit_operator_wake(
            user_id="u",
            owner_id="o",
            body={"type": "req", "id": "7", "method": "agent", "params": {"sessionKey": "agent:main:main"}},
        )
        assert refusal == {
            "type": "res",
            "id": "7",
            "ok": False,
            "error": {
                "code": "TRIAL_EXPIRED",
                "message": "Trial over",
                "details": {"reason": "wake_admission_gate", "code": "TRIAL_EXPIRED"},
            },
        }
        assert (
            await relay_hooks.admit_operator_wake(
                user_id="u", owner_id="o", body={"type": "req", "id": "8", "method": "sessions.list"}
            )
            is None
        )


@pytest.mark.asyncio
async def test_node_ready_and_closed_toggle_the_deny_list_at_the_edges():
    with (
        patch("core.services.relay_hooks.approve_pending_node_surface", new_callable=AsyncMock),
        patch("core.services.relay_hooks.ConnectionService") as svc_cls,
        patch("core.services.relay_hooks.patch_openclaw_config", new_callable=AsyncMock) as patch_cfg,
        patch("core.services.relay_hooks.publish_node_invalidation", new_callable=AsyncMock),
    ):
        svc_cls.return_value.count_relay_nodes = AsyncMock(side_effect=[1, 2, 1, 0])
        await relay_hooks.on_node_ready("o", "u", "d")  # first node → enable node tools
        await relay_hooks.on_node_ready("o", "u", "d")  # second node → no patch
        await relay_hooks.on_node_closed("o", "u")  # one left → no patch
        await relay_hooks.on_node_closed("o", "u")  # none left → deny nodes again
    assert [c.args[1]["tools"]["deny"][-1] for c in patch_cfg.await_args_list] == ["dashboard", "nodes"]


@pytest.mark.asyncio
async def test_on_node_closed_publishes_invalidation_even_when_the_patch_fails():
    """A container-down config patch must not swallow the invalidation
    publish — the UI still needs to know the node's health resource changed."""
    with (
        patch("core.services.relay_hooks.ConnectionService") as svc_cls,
        patch("core.services.relay_hooks.patch_openclaw_config", new_callable=AsyncMock) as patch_cfg,
        patch("core.services.relay_hooks.publish_node_invalidation", new_callable=AsyncMock) as publish,
    ):
        svc_cls.return_value.count_relay_nodes = AsyncMock(return_value=0)
        patch_cfg.side_effect = RuntimeError("container down")
        await relay_hooks.on_node_closed("o", "u")
    patch_cfg.assert_awaited_once()
    publish.assert_awaited_once_with("o", "u")


@pytest.mark.asyncio
async def test_on_node_closed_floors_the_count_when_the_own_row_failed_to_delete():
    """stale_row_undeleted=True: the caller's own presence-row delete failed,
    so count_relay_nodes still counts THIS closing connection -- on_node_closed
    must subtract it (floored at 0) rather than under-detecting "last node"."""
    with (
        patch("core.services.relay_hooks.ConnectionService") as svc_cls,
        patch("core.services.relay_hooks.patch_openclaw_config", new_callable=AsyncMock) as patch_cfg,
        patch("core.services.relay_hooks.publish_node_invalidation", new_callable=AsyncMock),
    ):
        # DB still shows 1 row (this connection's own, undeleted) -- with the
        # floor applied that is the true count of 0, so the repatch must fire.
        svc_cls.return_value.count_relay_nodes = AsyncMock(return_value=1)
        await relay_hooks.on_node_closed("o", "u", stale_row_undeleted=True)
    patch_cfg.assert_awaited_once()

    with (
        patch("core.services.relay_hooks.ConnectionService") as svc_cls,
        patch("core.services.relay_hooks.patch_openclaw_config", new_callable=AsyncMock) as patch_cfg,
        patch("core.services.relay_hooks.publish_node_invalidation", new_callable=AsyncMock),
    ):
        # 0 rows even before the floor -- max(0-1, 0) stays 0, never negative.
        svc_cls.return_value.count_relay_nodes = AsyncMock(return_value=0)
        await relay_hooks.on_node_closed("o", "u", stale_row_undeleted=True)
    patch_cfg.assert_awaited_once()

    with (
        patch("core.services.relay_hooks.ConnectionService") as svc_cls,
        patch("core.services.relay_hooks.patch_openclaw_config", new_callable=AsyncMock) as patch_cfg,
        patch("core.services.relay_hooks.publish_node_invalidation", new_callable=AsyncMock),
    ):
        # 2 rows, one undeleted -> floored count is 1, another node is still
        # live, so the repatch must NOT fire.
        svc_cls.return_value.count_relay_nodes = AsyncMock(return_value=2)
        await relay_hooks.on_node_closed("o", "u", stale_row_undeleted=True)
    patch_cfg.assert_not_awaited()
