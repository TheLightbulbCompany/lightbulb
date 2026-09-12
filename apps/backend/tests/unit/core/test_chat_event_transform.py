# backend/tests/unit/core/test_chat_event_transform.py
"""Unit tests for GatewayConnection's ``is_connected`` property.

The agent-event transform this file used to cover went with the room-frame
relay (turns are asynchronous now — the container publishes its own frames).
``_handle_message``'s surviving event routing is exercised in
tests/unit/gateway/.
"""

from unittest.mock import MagicMock

from core.gateway.connection_pool import GatewayConnection


class TestIsConnected:
    """Tests for is_connected property (websockets v16 compat)."""

    def _make_conn(self):

        return GatewayConnection(
            user_id="test-user",
            ip="10.0.0.1",
            token="t",
        )

    def test_no_ws_returns_false(self):
        conn = self._make_conn()
        assert conn.is_connected is False

    def test_ws_with_state_open(self):
        from websockets.protocol import State

        conn = self._make_conn()
        ws = MagicMock()
        ws.state = State.OPEN
        conn._ws = ws
        assert conn.is_connected is True

    def test_ws_with_state_closed(self):
        from websockets.protocol import State

        conn = self._make_conn()
        ws = MagicMock()
        ws.state = State.CLOSED
        conn._ws = ws
        assert conn.is_connected is False

    def test_ws_without_state_uses_closed_fallback(self):
        """Fallback for older websockets versions that have .closed attribute."""
        conn = self._make_conn()
        ws = MagicMock(spec=[])  # No attributes by default
        ws.closed = False
        del ws.state  # Ensure no .state
        conn._ws = ws
        assert conn.is_connected is True
