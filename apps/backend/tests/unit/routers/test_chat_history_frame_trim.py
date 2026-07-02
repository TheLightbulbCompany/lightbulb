"""chat.history frame trim: keep the res frame under the Management API 413 limit.

API Gateway's PostToConnection rejects payloads over 128KiB. An agent with a
huge session (e.g. a tool-heavy overflow session) returns a chat.history body
well past that, the push 413s, and the browser renders a blank transcript.
The backend must drop the OLDEST messages until the frame fits.
"""

import json
import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from routers.websocket_chat import (  # noqa: E402
    _WS_FRAME_BUDGET_BYTES,
    _fit_rpc_result_for_frame,
    _trim_chat_history_for_frame,
)


def _payload_size(result) -> int:
    return len(json.dumps(result, separators=(",", ":"), default=str).encode())


def _history(n: int, chars_per_message: int) -> dict:
    return {
        "sessionKey": "agent:a1:user_1",
        "messages": [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": [{"type": "text", "text": f"m{i}:" + "x" * chars_per_message}],
            }
            for i in range(n)
        ],
    }


def test_small_payload_passes_through_untouched():
    result = _history(10, 100)
    out = _trim_chat_history_for_frame(result)
    assert out is result
    assert "truncated" not in out


def test_oversized_payload_drops_oldest_until_it_fits():
    # ~200 messages x ~2KB ≈ 400KB — far over the 128KiB frame limit.
    result = _history(200, 2000)
    assert _payload_size(result) > _WS_FRAME_BUDGET_BYTES

    out = _trim_chat_history_for_frame(result)

    assert _payload_size(out) <= _WS_FRAME_BUDGET_BYTES
    assert out["truncated"] is True
    # Newest messages survive; oldest are gone.
    texts = [m["content"][0]["text"] for m in out["messages"]]
    assert texts[-1].startswith("m199:")
    assert not any(t.startswith("m0:") for t in texts)
    # It trims no more than necessary: one more message would overflow.
    kept = len(out["messages"])
    assert kept > 0
    one_more = dict(out)
    one_more["messages"] = result["messages"][200 - kept - 1 :]
    assert _payload_size(one_more) > _WS_FRAME_BUDGET_BYTES


def test_unrecognized_shapes_pass_through():
    assert _trim_chat_history_for_frame(None) is None
    assert _trim_chat_history_for_frame({"ok": True}) == {"ok": True}
    not_a_list = {"messages": "nope"}
    assert _trim_chat_history_for_frame(not_a_list) is not_a_list


def test_single_giant_message_still_yields_a_deliverable_frame():
    # Degenerate: even the newest message alone exceeds the budget. The frame
    # must still fit (empty messages + truncated flag) rather than 413ing.
    result = _history(3, 300_000)
    out = _trim_chat_history_for_frame(result)
    assert _payload_size(out) <= _WS_FRAME_BUDGET_BYTES
    assert out["messages"] == []
    assert out["truncated"] is True


def test_fit_replaces_oversized_discarded_method_payload_with_marker():
    # sessions.patch echoes a huge entry+catalog payload the frontend discards;
    # pushing it would 413 and report failure for a persisted operation.
    huge = {"entry": {"blob": "x" * 200_000}}
    out = _fit_rpc_result_for_frame("sessions.patch", huge)
    assert out == {"payloadOmitted": True}
    assert _payload_size(out) <= _WS_FRAME_BUDGET_BYTES


def test_fit_passes_small_payloads_and_unknown_oversized_methods_through():
    small = {"ok": True}
    assert _fit_rpc_result_for_frame("sessions.patch", small) is small
    unknown_huge = {"blob": "x" * 200_000}
    assert _fit_rpc_result_for_frame("agents.list", unknown_huge) is unknown_huge


def test_fit_delegates_chat_history_to_the_trim():
    result = _history(200, 2000)
    out = _fit_rpc_result_for_frame("chat.history", result)
    assert out["truncated"] is True
    assert _payload_size(out) <= _WS_FRAME_BUDGET_BYTES
