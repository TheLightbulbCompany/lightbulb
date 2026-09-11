"""Room-run forwarding for `/btw` side-channel answers (`chat.side_result`).

OpenClaw emits a `/btw` aside on a distinct `chat.side_result` event channel
(chat.ts), separate from the primary run's streaming. Isol8's gateway used to
fall through to the raw-forward `else`, so the frame never reached the chat
renderer. These tests pin the translation to a `btw_result` frontend frame.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest

from core.gateway.connection_pool import GatewayConnection

AGENT_ID = "11111111-1111-4111-8111-111111111111"
ROOM_ID = "22222222-2222-4222-8222-222222222222"
SESSION_KEY = f"agent:{AGENT_ID}:lightbulb:channel:{ROOM_ID}"


@pytest.fixture
def gateway_conn():
    """Capture the native room frames `_handle_message` publishes."""
    conn = GatewayConnection(
        user_id="org_1",
        ip="10.0.0.1",
        token="tok",
        owner_id="org_1",
    )
    captured: list[dict] = []

    async def _stub_forward(_session_key, frame):
        captured.append(frame)

    conn._publish_room_frame = _stub_forward  # type: ignore[assignment]
    conn._captured = captured  # type: ignore[attr-defined]
    return conn


@pytest.fixture
def captured_frontend_frames(gateway_conn):
    """The list of frames forwarded to the frontend by `gateway_conn`."""
    return gateway_conn._captured


@pytest.mark.asyncio
async def test_side_result_forwarded_as_btw_result(gateway_conn):
    await gateway_conn._handle_message(
        {
            "type": "event",
            "event": "chat.side_result",
            "payload": {
                "kind": "btw",
                "runId": "run_1",
                "sessionKey": SESSION_KEY,
                "agentId": AGENT_ID,
                "question": "what time is it?",
                "text": "It is 3pm.",
                "ts": 1,
            },
        }
    )

    frames = gateway_conn._captured
    btw = [f for f in frames if f.get("type") == "btw_result"]
    assert len(btw) == 1
    assert btw[0]["text"] == "It is 3pm."
    assert btw[0]["question"] == "what time is it?"
    assert btw[0]["runId"] == "run_1"
    assert btw[0]["agent_id"] == AGENT_ID
    assert btw[0]["isError"] is False


@pytest.mark.asyncio
async def test_side_result_error_sets_is_error(gateway_conn):
    await gateway_conn._handle_message(
        {
            "type": "event",
            "event": "chat.side_result",
            "payload": {
                "kind": "btw",
                "runId": "run_2",
                "sessionKey": SESSION_KEY,
                "question": "boom?",
                "text": "side run failed",
                "isError": True,
                "ts": 2,
            },
        }
    )

    btw = [m for m in gateway_conn._captured if m.get("type") == "btw_result"]
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

    btw = [m for m in gateway_conn._captured if m.get("type") == "btw_result"]
    assert btw == []
