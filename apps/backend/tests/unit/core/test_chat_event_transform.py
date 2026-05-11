# backend/tests/unit/core/test_chat_event_transform.py
"""Unit tests for GatewayConnection.

Covers _transform_agent_event(), _handle_message routing, and is_connected property.
"""

import pytest
from unittest.mock import MagicMock, call

from core.gateway.connection_pool import GatewayConnection


class TestIsConnected:
    """Tests for is_connected property (websockets v16 compat)."""

    def _make_conn(self):

        return GatewayConnection(
            user_id="test-user",
            ip="10.0.0.1",
            token="t",
            management_api=MagicMock(),
            frontend_connections=set(),
            conn_member_map={},
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


class TestTransformAgentEvent:
    """Tests for _transform_agent_event.

    OpenClaw agent events fire for every LLM token (unthrottled).
    Only assistant-stream events with cumulative text are forwarded.
    """

    def test_assistant_stream_with_text(self):
        result = GatewayConnection._transform_agent_event(
            {"stream": "assistant", "data": {"text": "Hello", "delta": "o"}, "runId": "run-1"}
        )
        assert result == {"type": "chunk", "content": "Hello", "runId": "run-1"}

    def test_assistant_stream_cumulative(self):
        """Successive events carry progressively longer cumulative text."""
        r1 = GatewayConnection._transform_agent_event(
            {"stream": "assistant", "data": {"text": "He", "delta": "He"}, "runId": "run-1"}
        )
        assert r1 == {"type": "chunk", "content": "He", "runId": "run-1"}

        r2 = GatewayConnection._transform_agent_event(
            {"stream": "assistant", "data": {"text": "Hello world", "delta": "llo world"}, "runId": "run-1"}
        )
        assert r2 == {"type": "chunk", "content": "Hello world", "runId": "run-1"}

    def test_non_assistant_non_tool_stream_ignored(self):
        assert GatewayConnection._transform_agent_event({"stream": "lifecycle", "data": {"phase": "start"}}) is None
        assert GatewayConnection._transform_agent_event({"stream": "compaction", "data": {"phase": "start"}}) is None

    def test_tool_stream_start_phase(self):
        result = GatewayConnection._transform_agent_event(
            {
                "stream": "tool",
                "data": {
                    "name": "web_search",
                    "phase": "start",
                    "toolCallId": "abc-123",
                    "args": {"query": "weather in Berwyn"},
                },
                "runId": "run-1",
            }
        )
        assert result == {
            "type": "tool_start",
            "tool": "web_search",
            "toolCallId": "abc-123",
            "args": {"query": "weather in Berwyn"},
            "runId": "run-1",
        }

    def test_tool_stream_start_without_args(self):
        """args key missing — output omits it rather than sending null."""
        result = GatewayConnection._transform_agent_event(
            {
                "stream": "tool",
                "data": {"name": "web_search", "phase": "start", "toolCallId": "abc-123"},
                "runId": "run-1",
            }
        )
        assert result == {"type": "tool_start", "tool": "web_search", "toolCallId": "abc-123", "runId": "run-1"}

    def test_tool_stream_result_phase(self):
        result = GatewayConnection._transform_agent_event(
            {
                "stream": "tool",
                "data": {
                    "name": "web_search",
                    "phase": "result",
                    "toolCallId": "abc-123",
                    "result": [{"type": "text", "text": "72°F"}],
                    "meta": "Berwyn, PA",
                },
                "runId": "run-1",
            }
        )
        assert result == {
            "type": "tool_end",
            "tool": "web_search",
            "toolCallId": "abc-123",
            "result": [{"type": "text", "text": "72°F"}],
            "meta": "Berwyn, PA",
            "runId": "run-1",
        }

    def test_tool_stream_result_with_is_error(self):
        """OpenClaw signals tool errors via isError on the result phase."""
        result = GatewayConnection._transform_agent_event(
            {
                "stream": "tool",
                "data": {
                    "name": "web_search",
                    "phase": "result",
                    "toolCallId": "abc-123",
                    "isError": True,
                    "result": [{"type": "text", "text": "Perplexity API 404"}],
                },
                "runId": "run-1",
            }
        )
        assert result == {
            "type": "tool_error",
            "tool": "web_search",
            "toolCallId": "abc-123",
            "result": [{"type": "text", "text": "Perplexity API 404"}],
            "runId": "run-1",
        }

    def test_tool_stream_update_phase_ignored(self):
        """Intermediate tool updates are not forwarded."""
        result = GatewayConnection._transform_agent_event(
            {"stream": "tool", "data": {"name": "web_search", "phase": "update", "toolCallId": "abc-123"}}
        )
        assert result is None

    def test_tool_stream_missing_name_ignored(self):
        result = GatewayConnection._transform_agent_event(
            {"stream": "tool", "data": {"phase": "start", "toolCallId": "abc-123"}}
        )
        assert result is None

    def test_thinking_stream_with_text(self):
        result = GatewayConnection._transform_agent_event(
            {"stream": "thinking", "data": {"text": "Let me think...", "delta": "..."}, "runId": "run-1"}
        )
        assert result == {"type": "thinking", "content": "Let me think...", "runId": "run-1"}

    def test_reasoning_stream_with_text(self):
        """OpenClaw may emit reasoning events under either stream name."""
        result = GatewayConnection._transform_agent_event(
            {"stream": "reasoning", "data": {"text": "Considering options", "delta": "s"}, "runId": "run-1"}
        )
        assert result == {"type": "thinking", "content": "Considering options", "runId": "run-1"}

    def test_thinking_stream_empty_text_ignored(self):
        assert GatewayConnection._transform_agent_event({"stream": "thinking", "data": {"text": ""}}) is None

    def test_missing_stream_ignored(self):
        assert GatewayConnection._transform_agent_event({"data": {"text": "Hi"}}) is None

    def test_missing_data_ignored(self):
        assert GatewayConnection._transform_agent_event({"stream": "assistant"}) is None

    def test_non_dict_data_ignored(self):
        assert GatewayConnection._transform_agent_event({"stream": "assistant", "data": "string"}) is None

    def test_empty_text_ignored(self):
        assert (
            GatewayConnection._transform_agent_event({"stream": "assistant", "data": {"text": "", "delta": ""}}) is None
        )


class TestHandleMessage:
    """Tests that _handle_message routes events correctly.

    - Agent events → smooth streaming via _transform_agent_event
    - Chat events → only terminal states (final sends complete text + done)
    - Chat delta → skipped (agent events handle streaming)
    - Other events → forwarded as-is
    """

    @pytest.fixture
    def mock_management_api(self):
        client = MagicMock()
        client.send_message = MagicMock(return_value=True)
        return client

    @pytest.fixture
    def connection(self, mock_management_api):

        conn = GatewayConnection(
            user_id="test-user",
            ip="10.0.0.1",
            token="test-token",
            management_api=mock_management_api,
            frontend_connections=set(),
            conn_member_map={},
        )
        conn._frontend_connections.add("conn-1")
        return conn

    # -- Agent events (unthrottled streaming) --

    def test_agent_assistant_event_forwarded_as_chunk(self, connection, mock_management_api):
        connection._handle_message(
            {
                "type": "event",
                "event": "agent",
                "payload": {"stream": "assistant", "data": {"text": "Hello", "delta": "o"}, "runId": "run-1"},
            }
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "chunk", "content": "Hello", "runId": "run-1"}
        )

    def test_agent_non_assistant_event_skipped(self, connection, mock_management_api):
        connection._handle_message(
            {
                "type": "event",
                "event": "agent",
                "payload": {"stream": "lifecycle", "data": {"phase": "start"}},
            }
        )
        mock_management_api.send_message.assert_not_called()

    def test_agent_tool_start_forwarded(self, connection, mock_management_api):
        connection._handle_message(
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "tool",
                    "data": {"name": "web_search", "phase": "start", "toolCallId": "t1"},
                    "runId": "run-1",
                },
            }
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "tool_start", "tool": "web_search", "toolCallId": "t1", "runId": "run-1"}
        )

    def test_agent_tool_end_forwarded(self, connection, mock_management_api):
        connection._handle_message(
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "tool",
                    "data": {"name": "web_search", "phase": "result", "toolCallId": "t1"},
                    "runId": "run-1",
                },
            }
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "tool_end", "tool": "web_search", "toolCallId": "t1", "runId": "run-1"}
        )

    def test_agent_events_tagged_with_agent_id_from_session_key(self, connection, mock_management_api):
        """sessionKey in the payload populates agent_id so the frontend can filter cross-agent messages."""
        connection._handle_message(
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "assistant",
                    "data": {"text": "Hello"},
                    "sessionKey": "agent:my-agent-id:main",
                    "runId": "run-1",
                },
            }
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "chunk", "content": "Hello", "agent_id": "my-agent-id", "runId": "run-1"}
        )

    def test_chat_final_tagged_with_agent_id(self, connection, mock_management_api):
        """chat.final chunk + done signals both carry agent_id so the frontend can route them."""
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "runId": "run-1",
                    "sessionKey": "agent:my-agent-id:main",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
                },
            }
        )
        assert mock_management_api.send_message.call_args_list == [
            call("conn-1", {"type": "chunk", "content": "Done.", "agent_id": "my-agent-id", "runId": "run-1"}),
            call("conn-1", {"type": "done", "agent_id": "my-agent-id", "runId": "run-1"}),
        ]

    # -- Chat events (terminal states only) --

    def test_chat_delta_skipped(self, connection, mock_management_api):
        """Chat delta events are ignored — agent events handle streaming."""
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "delta",
                    "message": {"content": [{"type": "text", "text": "Hi!"}]},
                },
            }
        )
        mock_management_api.send_message.assert_not_called()

    def test_chat_final_sends_chunk_then_done(self, connection, mock_management_api):
        """Final emits the assistant text as a chunk (cumulative replace, no-op for
        agent runs that already streamed it) then the done signal."""
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "runId": "run-1",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Complete response here."}],
                    },
                },
            }
        )
        assert mock_management_api.send_message.call_args_list == [
            call("conn-1", {"type": "chunk", "content": "Complete response here.", "runId": "run-1"}),
            call("conn-1", {"type": "done", "runId": "run-1"}),
        ]

    def test_chat_final_with_thinking_sends_thinking_chunk_then_done(self, connection, mock_management_api):
        """Thinking blocks in the final message are forwarded ahead of the text chunk."""
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "runId": "run-1",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "Let me reason about this..."},
                            {"type": "text", "text": "Answer"},
                        ],
                    },
                },
            }
        )
        assert mock_management_api.send_message.call_args_list == [
            call("conn-1", {"type": "thinking", "content": "Let me reason about this...", "runId": "run-1"}),
            call("conn-1", {"type": "chunk", "content": "Answer", "runId": "run-1"}),
            call("conn-1", {"type": "done", "runId": "run-1"}),
        ]

    def test_chat_final_without_text_sends_only_done(self, connection, mock_management_api):
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "final", "runId": "run-1"},
            }
        )
        mock_management_api.send_message.assert_called_once_with("conn-1", {"type": "done", "runId": "run-1"})

    def test_chat_final_emits_chunk_for_slash_command_response(self, connection, mock_management_api):
        """Slash commands (/help, /whoami, /status, etc.) reply via chat.final
        without any preceding agent stream — the response text lives only in
        message.content. The connection pool must forward that text as a chunk
        so the frontend has something to render. Without this, slash-command
        replies are silently dropped on their way to the browser.
        """
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "runId": "run-cmd",
                    "sessionKey": "agent:agent-1:main",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "OpenClaw — built-in commands ..."}],
                    },
                },
            }
        )
        calls = mock_management_api.send_message.call_args_list
        assert calls[0] == call(
            "conn-1",
            {
                "type": "chunk",
                "content": "OpenClaw — built-in commands ...",
                "agent_id": "agent-1",
                "runId": "run-cmd",
            },
        )
        assert calls[-1] == call("conn-1", {"type": "done", "agent_id": "agent-1", "runId": "run-cmd"})

    def test_chat_error_carries_upstream_errorMessage(self, connection, mock_management_api):
        """Upstream OpenClaw emits `errorMessage` (string) on chat-state-error events.

        See src/gateway/protocol/schema/logs-chat.ts ChatEventSchema in the
        OpenClaw repo — the wire shape is `{ state: "error", errorMessage,
        errorKind? }`, not `{ error: ... }`. The backend must forward the
        upstream message verbatim so the user sees the real failure reason
        instead of a generic "Agent run failed" fallback.
        """
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "error",
                    "runId": "run-1",
                    "errorMessage": "Provider returned 429: ChatGPT Plus usage limit reached.",
                },
            }
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1",
            {
                "type": "error",
                "message": "Provider returned 429: ChatGPT Plus usage limit reached.",
                "runId": "run-1",
            },
        )

    def test_chat_error_forwards_errorKind_as_code(self, connection, mock_management_api):
        """`errorKind` (refusal | timeout | rate_limit | context_length | unknown)
        is forwarded as `code` so the frontend can render a typed error card.
        """
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "error",
                    "runId": "run-1",
                    "errorMessage": "Rate limit reached. Try again later.",
                    "errorKind": "rate_limit",
                },
            }
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1",
            {
                "type": "error",
                "message": "Rate limit reached. Try again later.",
                "code": "rate_limit",
                "runId": "run-1",
            },
        )

    def test_chat_error_legacy_error_dict_fallback(self, connection, mock_management_api):
        """Defensive fallback: if some path emits the older `error: {message:...}`
        shape, we still extract the message rather than dropping it.
        """
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "error", "runId": "run-1", "error": {"message": "Legacy"}},
            }
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "error", "message": "Legacy", "runId": "run-1"}
        )

    def test_chat_error_legacy_error_string_fallback(self, connection, mock_management_api):
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "error", "runId": "run-1", "error": "Something broke"},
            }
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "error", "message": "Something broke", "runId": "run-1"}
        )

    def test_chat_error_no_message_uses_generic_fallback(self, connection, mock_management_api):
        """If neither field is present we still need to ship *something* so the
        frontend can finalize the bubble — but log it via the generic message."""
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "error", "runId": "run-1"},
            }
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "error", "message": "Agent run failed", "runId": "run-1"}
        )

    def test_chat_aborted(self, connection, mock_management_api):
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "aborted", "runId": "run-1"},
            }
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "error", "message": "Agent run was cancelled", "runId": "run-1"}
        )

    def test_chat_unknown_state_skipped(self, connection, mock_management_api):
        connection._handle_message(
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "unknown"},
            }
        )
        mock_management_api.send_message.assert_not_called()

    # -- Other events (forwarded as-is) --

    def test_non_chat_non_agent_event_forwarded_as_is(self, connection, mock_management_api):
        raw_event = {"type": "event", "event": "sessions.updated", "payload": {"status": "ok"}}
        connection._handle_message(raw_event)
        mock_management_api.send_message.assert_called_once_with("conn-1", raw_event)

    def test_health_tick_events_dropped(self, connection, mock_management_api):
        """Health/tick heartbeat events should never reach frontends."""
        connection._handle_message({"type": "event", "event": "health", "payload": {}})
        connection._handle_message({"type": "event", "event": "tick", "payload": {}})
        mock_management_api.send_message.assert_not_called()

    # -- Multi-connection forwarding --

    def test_events_sent_to_all_frontend_connections(self, connection, mock_management_api):
        connection._frontend_connections.add("conn-2")
        connection._handle_message(
            {
                "type": "event",
                "event": "agent",
                "payload": {"stream": "assistant", "data": {"text": "Hi"}, "runId": "run-1"},
            }
        )
        assert mock_management_api.send_message.call_count == 2
