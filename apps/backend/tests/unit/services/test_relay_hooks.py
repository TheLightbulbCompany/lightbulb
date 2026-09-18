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
