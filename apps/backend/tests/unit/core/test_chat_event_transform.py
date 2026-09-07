# backend/tests/unit/core/test_chat_event_transform.py
"""Unit tests for GatewayConnection.

Covers _transform_agent_event(), _handle_message routing, and is_connected property.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, call

from core.gateway.connection_pool import GatewayConnection


async def _handle_and_flush(connection: GatewayConnection, msg: dict) -> None:
    """Route *msg* then wait for the FrameSender drainer to hand every queued
    frame to the management API — delivery is async (per-connection worker
    tasks), so tests must flush before asserting on send_message calls."""
    await connection._handle_message(msg)
    await connection._sender.flush()


class TestIsConnected:
    """Tests for is_connected property (websockets v16 compat)."""

    def _make_conn(self):

        return GatewayConnection(
            user_id="test-user",
            ip="10.0.0.1",
            token="t",
            management_api=MagicMock(),
            frontend_connections=set(),
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

    OpenClaw's gateway coalesces assistant/thinking streams at 150ms per
    run; each event carries the accumulated ``text`` plus the incremental
    ``delta``. The transform forwards the delta (``append: True``) with the
    full text stashed in ``_full_content`` for the sender's per-connection
    resync; events with no usable delta (or ``replace: true``) fall back to
    the legacy full-text frame.
    """

    def test_assistant_stream_with_delta(self):
        result = GatewayConnection._transform_agent_event(
            {"stream": "assistant", "data": {"text": "Hello", "delta": "o"}, "runId": "run-1"}
        )
        assert result == {
            "type": "chunk",
            "content": "o",
            "append": True,
            "_full_content": "Hello",
            "runId": "run-1",
        }

    def test_assistant_stream_successive_deltas(self):
        """Successive events forward their deltas; _full_content tracks the
        progressively longer accumulated text."""
        r1 = GatewayConnection._transform_agent_event(
            {"stream": "assistant", "data": {"text": "He", "delta": "He"}, "runId": "run-1"}
        )
        assert r1 == {
            "type": "chunk",
            "content": "He",
            "append": True,
            "_full_content": "He",
            "runId": "run-1",
        }

        r2 = GatewayConnection._transform_agent_event(
            {"stream": "assistant", "data": {"text": "Hello world", "delta": "llo world"}, "runId": "run-1"}
        )
        assert r2 == {
            "type": "chunk",
            "content": "llo world",
            "append": True,
            "_full_content": "Hello world",
            "runId": "run-1",
        }

    def test_assistant_stream_replace_falls_back_to_full_text(self):
        """replace: true means the new text is NOT a prefix-extension of what
        was streamed — the client must swap its buffer for the full text, so
        the transform emits the legacy cumulative (non-append) frame."""
        result = GatewayConnection._transform_agent_event(
            {
                "stream": "assistant",
                "data": {"text": "Rewritten", "delta": "", "replace": True},
                "runId": "run-1",
            }
        )
        assert result == {"type": "chunk", "content": "Rewritten", "runId": "run-1"}

    def test_assistant_stream_no_delta_falls_back_to_full_text(self):
        """Events without a delta field (provider sent only full text) keep
        the legacy cumulative-replacement contract."""
        result = GatewayConnection._transform_agent_event(
            {"stream": "assistant", "data": {"text": "Hello"}, "runId": "run-1"}
        )
        assert result == {"type": "chunk", "content": "Hello", "runId": "run-1"}

    def test_assistant_stream_delta_only_uses_delta_as_full(self):
        """A delta-only event (no accumulated text) must never produce an
        empty _full_content — the resync form would ship an empty message."""
        result = GatewayConnection._transform_agent_event(
            {"stream": "assistant", "data": {"text": "", "delta": "Hi"}, "runId": "run-1"}
        )
        assert result == {
            "type": "chunk",
            "content": "Hi",
            "append": True,
            "_full_content": "Hi",
            "runId": "run-1",
        }

    def test_lifecycle_stream_still_ignored(self):
        """lifecycle is consumed by _handle_message (run-active + billing) and
        is not a user-visible chat surface — the transform never forwards it."""
        assert GatewayConnection._transform_agent_event({"stream": "lifecycle", "data": {"phase": "start"}}) is None
        assert GatewayConnection._transform_agent_event({"stream": "lifecycle", "data": {"phase": "end"}}) is None

    def test_approval_stream_still_ignored(self):
        """approval is consumed by _handle_message (the awaiting-approval
        run-active window logic). The transform must NOT forward it — doing so
        would double-handle the approval bracket and conflict with the
        run-active state machine."""
        assert (
            GatewayConnection._transform_agent_event(
                {
                    "stream": "approval",
                    "data": {"phase": "requested", "kind": "exec", "status": "pending"},
                    "runId": "r",
                }
            )
            is None
        )

    def test_command_output_delta_forwarded(self):
        result = GatewayConnection._transform_agent_event(
            {
                "stream": "command_output",
                "data": {
                    "toolCallId": "t1",
                    "phase": "delta",
                    "name": "bash",
                    "output": "compiling...\n",
                    "title": "bash",
                },
                "runId": "run-1",
            }
        )
        assert result == {
            "type": "command_output",
            "toolCallId": "t1",
            "output": "compiling...\n",
            "runId": "run-1",
        }

    def test_command_output_without_toolcallid_ignored(self):
        assert (
            GatewayConnection._transform_agent_event(
                {"stream": "command_output", "data": {"output": "x"}, "runId": "r"}
            )
            is None
        )

    def test_compaction_stream_forwarded_as_a_run_state(self):
        """Compaction now reaches the client — as a run STATE, not a transcript entry.

        This used to assert the opposite, deferring compaction because
        rendering it *in the transcript* needs a mid-run live-turn boundary
        plus a willRetry stream-offset reset. That reasoning still holds and
        that work is still not done: the frontend routes these frames to the
        run-status strip's phase label and returns before any pill, live-turn
        or offset logic, so none of it is involved. What the owner gets is a
        named reason for a stall of tens of seconds — and, when
        ``completed`` is false, a warning that the reply which follows may
        have lost context.
        """
        end = GatewayConnection._transform_agent_event(
            {"stream": "compaction", "data": {"phase": "end", "completed": True}, "runId": "run-1"}
        )
        assert end == {
            "type": "agent_activity",
            "activity": "compaction",
            "title": "end",
            "detail": {"phase": "end", "completed": True},
            "runId": "run-1",
        }
        start = GatewayConnection._transform_agent_event(
            {"stream": "compaction", "data": {"phase": "start"}, "runId": "r"}
        )
        assert start is not None
        assert start["activity"] == "compaction"
        assert start["title"] == "start"
        # A phase-less compaction event has nothing to say — still dropped.
        assert GatewayConnection._transform_agent_event({"stream": "compaction", "data": {}, "runId": "r"}) is None

    def test_plan_stream_forwarded_as_activity(self):
        result = GatewayConnection._transform_agent_event(
            {
                "stream": "plan",
                "data": {"phase": "update", "title": "Assistant proposed a plan", "steps": ["a", "b"]},
                "runId": "run-1",
            }
        )
        assert result == {
            "type": "agent_activity",
            "activity": "plan",
            "title": "Assistant proposed a plan",
            "detail": {"phase": "update", "title": "Assistant proposed a plan", "steps": ["a", "b"]},
            "runId": "run-1",
        }

    def test_patch_stream_forwarded_as_activity(self):
        result = GatewayConnection._transform_agent_event(
            {
                "stream": "patch",
                "data": {
                    "phase": "end",
                    "title": "Edited 2 files",
                    "added": ["x"],
                    "modified": [],
                    "deleted": [],
                    "summary": "…",
                    "toolCallId": "t9",
                },
                "runId": "run-1",
            }
        )
        assert result["type"] == "agent_activity"
        assert result["activity"] == "patch"
        assert result["title"] == "Edited 2 files"
        assert result["runId"] == "run-1"

    def test_item_stream_forwarded_as_activity(self):
        result = GatewayConnection._transform_agent_event(
            {
                "stream": "item",
                "data": {"itemId": "i1", "phase": "start", "kind": "search", "title": "Searching", "status": "running"},
                "runId": "run-1",
            }
        )
        assert result["type"] == "agent_activity"
        assert result["activity"] == "item"
        assert result["title"] == "Searching"

    def test_acp_stream_forwarded_as_activity(self):
        result = GatewayConnection._transform_agent_event(
            {"stream": "acp", "data": {"title": "ACP step"}, "runId": "run-1"}
        )
        assert result["type"] == "agent_activity"
        assert result["activity"] == "acp"

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

    def test_tool_stream_update_phase_emits_progress(self):
        """phase:"update" carries partialResult — forward it as tool_progress
        so the live tool pill shows incremental output (upstream renders this
        via formatToolOutput(data.partialResult)). Requires a toolCallId to
        address the live pill."""
        result = GatewayConnection._transform_agent_event(
            {
                "stream": "tool",
                "data": {
                    "name": "bash",
                    "phase": "update",
                    "toolCallId": "abc-123",
                    "partialResult": [{"type": "text", "text": "line 1\n"}],
                },
                "runId": "run-1",
            }
        )
        assert result == {
            "type": "tool_progress",
            "tool": "bash",
            "toolCallId": "abc-123",
            "partialResult": [{"type": "text", "text": "line 1\n"}],
            "runId": "run-1",
        }

    def test_tool_stream_update_without_name_still_emits_progress(self):
        """phase:"update" attaches to an existing pill by toolCallId, so name is
        optional — the update keys by toolCallId alone (upstream's tool-event
        ``name`` is optional in the contract). An unnamed update with a
        toolCallId must still forward live progress; dropping it loses output."""
        result = GatewayConnection._transform_agent_event(
            {
                "stream": "tool",
                "data": {
                    "phase": "update",
                    "toolCallId": "abc-123",
                    "partialResult": [{"type": "text", "text": "line 1\n"}],
                },
                "runId": "run-1",
            }
        )
        assert result == {
            "type": "tool_progress",
            "toolCallId": "abc-123",
            "partialResult": [{"type": "text", "text": "line 1\n"}],
            "runId": "run-1",
        }

    def test_tool_stream_update_without_toolcallid_ignored(self):
        """No toolCallId means no live pill to address — drop it."""
        assert (
            GatewayConnection._transform_agent_event(
                {"stream": "tool", "data": {"name": "bash", "phase": "update"}, "runId": "r"}
            )
            is None
        )

    def test_tool_stream_missing_name_ignored(self):
        result = GatewayConnection._transform_agent_event(
            {"stream": "tool", "data": {"phase": "start", "toolCallId": "abc-123"}}
        )
        assert result is None

    def test_thinking_stream_with_delta(self):
        result = GatewayConnection._transform_agent_event(
            {"stream": "thinking", "data": {"text": "Let me think...", "delta": "..."}, "runId": "run-1"}
        )
        assert result == {
            "type": "thinking",
            "content": "...",
            "append": True,
            "_full_content": "Let me think...",
            "runId": "run-1",
        }

    def test_reasoning_stream_with_delta(self):
        """OpenClaw may emit reasoning events under either stream name."""
        result = GatewayConnection._transform_agent_event(
            {"stream": "reasoning", "data": {"text": "Considering options", "delta": "s"}, "runId": "run-1"}
        )
        assert result == {
            "type": "thinking",
            "content": "s",
            "append": True,
            "_full_content": "Considering options",
            "runId": "run-1",
        }

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
        )
        # Owner-keyed fan-out reads recipients from the by-owner-id GSI. These
        # routing tests are personal (no target member), so a single chat row
        # is the recipient.
        conn._conn_svc.query_by_owner = AsyncMock(return_value=[("conn-1", None, "chat")])
        return conn

    # -- Agent events (unthrottled streaming) --

    @pytest.mark.asyncio
    async def test_agent_assistant_event_forwarded_as_chunk(self, connection, mock_management_api):
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "agent",
                "payload": {"stream": "assistant", "data": {"text": "Hello", "delta": "o"}, "runId": "run-1"},
            },
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "chunk", "content": "Hello", "runId": "run-1"}
        )

    @pytest.mark.asyncio
    async def test_agent_non_assistant_event_skipped(self, connection, mock_management_api):
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "agent",
                "payload": {"stream": "lifecycle", "data": {"phase": "start"}},
            },
        )
        mock_management_api.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_tool_start_forwarded(self, connection, mock_management_api):
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "tool",
                    "data": {"name": "web_search", "phase": "start", "toolCallId": "t1"},
                    "runId": "run-1",
                },
            },
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "tool_start", "tool": "web_search", "toolCallId": "t1", "runId": "run-1"}
        )

    @pytest.mark.asyncio
    async def test_agent_tool_end_forwarded(self, connection, mock_management_api):
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "tool",
                    "data": {"name": "web_search", "phase": "result", "toolCallId": "t1"},
                    "runId": "run-1",
                },
            },
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "tool_end", "tool": "web_search", "toolCallId": "t1", "runId": "run-1"}
        )

    @pytest.mark.asyncio
    async def test_agent_events_tagged_with_agent_id_from_session_key(self, connection, mock_management_api):
        """sessionKey in the payload populates agent_id so the frontend can filter cross-agent messages."""
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "stream": "assistant",
                    "data": {"text": "Hello"},
                    "sessionKey": "agent:my-agent-id:main",
                    "runId": "run-1",
                },
            },
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "chunk", "content": "Hello", "agent_id": "my-agent-id", "runId": "run-1"}
        )

    @pytest.mark.asyncio
    async def test_chat_final_tagged_with_agent_id(self, connection, mock_management_api):
        """chat.final chunk + done signals both carry agent_id so the frontend can route them."""
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "final",
                    "runId": "run-1",
                    "sessionKey": "agent:my-agent-id:main",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
                },
            },
        )
        assert mock_management_api.send_message.call_args_list == [
            call("conn-1", {"type": "chunk", "content": "Done.", "agent_id": "my-agent-id", "runId": "run-1"}),
            call("conn-1", {"type": "done", "agent_id": "my-agent-id", "runId": "run-1"}),
            # Authoritative run-ended frame trails the done frame (true terminal).
            call("conn-1", {"type": "run_ended", "runId": "run-1", "outcome": "done", "agent_id": "my-agent-id"}),
        ]

    # -- Chat events (terminal states only) --

    @pytest.mark.asyncio
    async def test_chat_delta_skipped(self, connection, mock_management_api):
        """Chat delta events are ignored — agent events handle streaming."""
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "delta",
                    "message": {"content": [{"type": "text", "text": "Hi!"}]},
                },
            },
        )
        mock_management_api.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_chat_final_sends_chunk_then_done(self, connection, mock_management_api):
        """Final emits the assistant text as a chunk (cumulative replace, no-op for
        agent runs that already streamed it) then the done signal."""
        await _handle_and_flush(
            connection,
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
            },
        )
        assert mock_management_api.send_message.call_args_list == [
            call("conn-1", {"type": "chunk", "content": "Complete response here.", "runId": "run-1"}),
            call("conn-1", {"type": "done", "runId": "run-1"}),
        ]

    @pytest.mark.asyncio
    async def test_chat_final_with_thinking_sends_thinking_chunk_then_done(self, connection, mock_management_api):
        """Thinking blocks in the final message are forwarded ahead of the text chunk."""
        await _handle_and_flush(
            connection,
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
            },
        )
        assert mock_management_api.send_message.call_args_list == [
            call("conn-1", {"type": "thinking", "content": "Let me reason about this...", "runId": "run-1"}),
            call("conn-1", {"type": "chunk", "content": "Answer", "runId": "run-1"}),
            call("conn-1", {"type": "done", "runId": "run-1"}),
        ]

    @pytest.mark.asyncio
    async def test_chat_final_without_text_sends_only_done(self, connection, mock_management_api):
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "final", "runId": "run-1"},
            },
        )
        mock_management_api.send_message.assert_called_once_with("conn-1", {"type": "done", "runId": "run-1"})

    @pytest.mark.asyncio
    async def test_chat_final_emits_chunk_for_slash_command_response(self, connection, mock_management_api):
        """Slash commands (/help, /whoami, /status, etc.) reply via chat.final
        without any preceding agent stream — the response text lives only in
        message.content. The connection pool must forward that text as a chunk
        so the frontend has something to render. Without this, slash-command
        replies are silently dropped on their way to the browser.
        """
        await _handle_and_flush(
            connection,
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
            },
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
        # The done frame is forwarded; the authoritative run_ended frame trails it.
        assert call("conn-1", {"type": "done", "agent_id": "agent-1", "runId": "run-cmd"}) in calls
        assert calls[-1] == call(
            "conn-1", {"type": "run_ended", "runId": "run-cmd", "outcome": "done", "agent_id": "agent-1"}
        )

    @pytest.mark.asyncio
    async def test_chat_error_carries_upstream_errorMessage(self, connection, mock_management_api):
        """Upstream OpenClaw emits `errorMessage` (string) on chat-state-error events.

        See src/gateway/protocol/schema/logs-chat.ts ChatEventSchema in the
        OpenClaw repo — the wire shape is `{ state: "error", errorMessage,
        errorKind? }`, not `{ error: ... }`. The backend must forward the
        upstream message verbatim so the user sees the real failure reason
        instead of a generic "Agent run failed" fallback.
        """
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "error",
                    "runId": "run-1",
                    "errorMessage": "Provider returned 429: ChatGPT Plus usage limit reached.",
                },
            },
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1",
            {
                "type": "error",
                "message": "Provider returned 429: ChatGPT Plus usage limit reached.",
                "runId": "run-1",
            },
        )

    @pytest.mark.asyncio
    async def test_chat_error_forwards_errorKind_as_code(self, connection, mock_management_api, monkeypatch):
        """`errorKind` (refusal | timeout | rate_limit | context_length | unknown)
        is forwarded as `code` so the frontend can render a typed error card.

        `errorKind: "rate_limit"` drives this frame through
        `GatewayConnection._budget_error_code`'s mid-run reclassification
        check (see connection_pool.py), which does a real `billing_repo`
        read. Stubbed here to return None (no billing row) so this test
        exercises that branch deterministically instead of hitting DynamoDB —
        without the stub the call falls through to a live GetItem that fails
        with ResourceNotFoundException in this environment and is swallowed
        by `_budget_error_code`'s blanket except, which happens to produce
        the same "leave errorKind alone" result but via an unmocked network
        call and a logged warning+traceback per run.
        """

        async def fake_no_billing_row(owner_id):
            return None

        monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_no_billing_row)

        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "state": "error",
                    "runId": "run-1",
                    "errorMessage": "Rate limit reached. Try again later.",
                    "errorKind": "rate_limit",
                },
            },
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

    @pytest.mark.asyncio
    async def test_chat_error_legacy_error_dict_fallback(self, connection, mock_management_api):
        """Defensive fallback: if some path emits the older `error: {message:...}`
        shape, we still extract the message rather than dropping it.
        """
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "error", "runId": "run-1", "error": {"message": "Legacy"}},
            },
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "error", "message": "Legacy", "runId": "run-1"}
        )

    @pytest.mark.asyncio
    async def test_chat_error_legacy_error_string_fallback(self, connection, mock_management_api):
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "error", "runId": "run-1", "error": "Something broke"},
            },
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "error", "message": "Something broke", "runId": "run-1"}
        )

    @pytest.mark.asyncio
    async def test_chat_error_no_message_uses_generic_fallback(self, connection, mock_management_api):
        """If neither field is present we still need to ship *something* so the
        frontend can finalize the bubble — but log it via the generic message."""
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "error", "runId": "run-1"},
            },
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "error", "message": "Agent run failed", "runId": "run-1"}
        )

    @pytest.mark.asyncio
    async def test_chat_aborted(self, connection, mock_management_api):
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "aborted", "runId": "run-1"},
            },
        )
        mock_management_api.send_message.assert_called_once_with(
            "conn-1", {"type": "error", "message": "Agent run was cancelled", "runId": "run-1"}
        )

    @pytest.mark.asyncio
    async def test_chat_unknown_state_skipped(self, connection, mock_management_api):
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "chat",
                "payload": {"state": "unknown"},
            },
        )
        mock_management_api.send_message.assert_not_called()

    # -- Other events (forwarded as-is) --

    @pytest.mark.asyncio
    async def test_non_chat_non_agent_event_forwarded_as_is(self, connection, mock_management_api):
        raw_event = {"type": "event", "event": "sessions.updated", "payload": {"status": "ok"}}
        await _handle_and_flush(connection, raw_event)
        mock_management_api.send_message.assert_called_once_with("conn-1", raw_event)

    @pytest.mark.asyncio
    async def test_health_tick_events_dropped(self, connection, mock_management_api):
        """Health/tick heartbeat events should never reach frontends."""
        await _handle_and_flush(connection, {"type": "event", "event": "health", "payload": {}})
        await _handle_and_flush(connection, {"type": "event", "event": "tick", "payload": {}})
        mock_management_api.send_message.assert_not_called()

    # -- Multi-connection forwarding --

    @pytest.mark.asyncio
    async def test_events_sent_to_all_frontend_connections(self, connection, mock_management_api):
        # Owner has two chat connections in the by-owner-id GSI — both receive
        # the broadcast event.
        connection._conn_svc.query_by_owner = AsyncMock(
            return_value=[("conn-1", None, "chat"), ("conn-2", None, "chat")]
        )
        await _handle_and_flush(
            connection,
            {
                "type": "event",
                "event": "agent",
                "payload": {"stream": "assistant", "data": {"text": "Hi"}, "runId": "run-1"},
            },
        )
        assert mock_management_api.send_message.call_count == 2
