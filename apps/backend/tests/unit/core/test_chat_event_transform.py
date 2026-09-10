# backend/tests/unit/core/test_chat_event_transform.py
"""Unit tests for GatewayConnection.

Covers _transform_agent_event() and is_connected property. _handle_message's
event routing (post native-multiplayer-cutover: publishes via
_publish_room_frame / messaging_repo / live_events, not a direct
management_api.send_message fanout to frontend connections) is exercised in
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
