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
