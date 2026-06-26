"""Gateway forwarding for `/btw` side-channel answers (`chat.side_result`).

OpenClaw emits a `/btw` aside on a distinct `chat.side_result` event channel
(chat.ts), separate from the primary run's streaming. Isol8's gateway used to
fall through to the raw-forward `else`, so the frame never reached the chat
renderer. These tests pin the translation to a `btw_result` frontend frame.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import MagicMock

import pytest

from core.gateway.connection_pool import GatewayConnection


@pytest.fixture
def gateway_conn():
    """A GatewayConnection whose `_forward_to_frontends` is replaced by a stub
    that records `(message, target_member_id)` pairs — so we assert on the
    frames `_handle_message` produces without touching the DDB fan-out path."""
    conn = GatewayConnection(
        frontend_connections=set(),
        user_id="org_1",
        ip="10.0.0.1",
        token="tok",
        management_api=MagicMock(),
        owner_id="org_1",
    )
    captured: list[tuple[dict, str | None]] = []

    async def _stub_forward(message, target_member_id=None):
        captured.append((message, target_member_id))

    conn._forward_to_frontends = _stub_forward  # type: ignore[assignment]
    conn._captured = captured  # type: ignore[attr-defined]
    return conn


@pytest.fixture
def captured_frontend_frames(gateway_conn):
    """The list of frames forwarded to the frontend by `gateway_conn`."""
    return [msg for msg, _target in gateway_conn._captured]


@pytest.mark.asyncio
async def test_side_result_forwarded_as_btw_result(gateway_conn):
    await gateway_conn._handle_message(
        {
            "type": "event",
            "event": "chat.side_result",
            "payload": {
                "kind": "btw",
                "runId": "run_1",
                "sessionKey": "agent:a1:user_1",
                "agentId": "a1",
                "question": "what time is it?",
                "text": "It is 3pm.",
                "ts": 1,
            },
        }
    )

    frames = [m for m, _t in gateway_conn._captured]
    btw = [f for f in frames if f.get("type") == "btw_result"]
    assert len(btw) == 1
    assert btw[0]["text"] == "It is 3pm."
    assert btw[0]["question"] == "what time is it?"
    assert btw[0]["runId"] == "run_1"
    # agent_id is resolved from the sessionKey (`agent:a1:user_1` → "a1").
    assert btw[0]["agent_id"] == "a1"
    # isError is omitted when not truthy.
    assert "isError" not in btw[0]
    # Routed to the member who asked, mirroring the chat branch.
    target = next(t for m, t in gateway_conn._captured if m.get("type") == "btw_result")
    assert target == "user_1"


@pytest.mark.asyncio
async def test_side_result_error_sets_is_error(gateway_conn):
    await gateway_conn._handle_message(
        {
            "type": "event",
            "event": "chat.side_result",
            "payload": {
                "kind": "btw",
                "runId": "run_2",
                "sessionKey": "agent:a1:user_1",
                "question": "boom?",
                "text": "side run failed",
                "isError": True,
                "ts": 2,
            },
        }
    )

    btw = [m for m, _t in gateway_conn._captured if m.get("type") == "btw_result"]
    assert len(btw) == 1
    assert btw[0]["isError"] is True


@pytest.mark.asyncio
async def test_side_result_dropped_for_isolated_session(gateway_conn):
    """Paperclip/channel/cron side results never reach the web chat — same
    gate the chat branch uses."""
    await gateway_conn._handle_message(
        {
            "type": "event",
            "event": "chat.side_result",
            "payload": {
                "kind": "btw",
                "runId": "run_3",
                "sessionKey": "agent:a1:paperclip",
                "question": "q",
                "text": "should not surface",
                "ts": 3,
            },
        }
    )

    btw = [m for m, _t in gateway_conn._captured if m.get("type") == "btw_result"]
    assert btw == []
