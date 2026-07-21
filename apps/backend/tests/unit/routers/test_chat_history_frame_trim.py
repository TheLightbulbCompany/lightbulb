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
    _SESSION_PREVIEW_MAX_CHARS,
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


def test_single_giant_message_is_elided_for_unpaged_requests():
    # Degenerate: even the newest message alone exceeds the budget, AND this
    # is the legacy unpaged shape (no `offset` key). The frame must still
    # ship one elided message rather than dropping to an empty transcript —
    # blanking the page for unpaged callers is the exact bug this guard
    # exists to eliminate.
    result = _history(3, 300_000)
    assert "offset" not in result
    out = _trim_chat_history_for_frame(result)
    assert _payload_size(out) <= _WS_FRAME_BUDGET_BYTES
    assert len(out["messages"]) == 1
    assert out["truncated"] is True
    assert "omitted" in out["messages"][0]["content"][0]["text"]
    # Unpaged: no cursor fields invented.
    assert "nextOffset" not in out
    assert "hasMore" not in out


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


def test_fit_caps_sessions_list_previews_so_many_sessions_stay_frame_sized():
    # 50 rows x 10KB previews = ~500KB unslimmed; the UI renders <=48 chars of
    # each preview, so capping keeps the sidebar read deliverable.
    rows = [
        {"key": f"agent:a1:user_1:0000000{i}", "lastMessage": {"text": "y" * 10_000}, "updatedAt": i} for i in range(50)
    ]
    out = _fit_rpc_result_for_frame("sessions.list", {"sessions": rows})
    assert _payload_size(out) <= _WS_FRAME_BUDGET_BYTES
    assert all(len(r["lastMessage"]["text"]) == _SESSION_PREVIEW_MAX_CHARS for r in out["sessions"])
    # Non-preview fields are untouched.
    assert out["sessions"][7]["updatedAt"] == 7


def test_fit_leaves_short_preview_rows_alone():
    result = {"sessions": [{"key": "agent:a1:user_1", "lastMessage": {"text": "short"}}]}
    assert _fit_rpc_result_for_frame("sessions.list", result) is result


def test_trim_rewrites_next_offset_for_a_paged_response():
    # A page of 4 messages, chronological (index 0 == oldest of the page).
    # Only the newest 2 fit the budget, so the 2 oldest are dropped. Those
    # dropped messages are at the FAR end of the page, so the client must
    # resume at offset + kept, not at the server's original nextOffset.
    big = "x" * 400
    result = {
        "messages": [{"role": "user", "content": big} for _ in range(4)],
        "offset": 10,
        "nextOffset": 14,
        "hasMore": True,
        "totalMessages": 160,
    }
    out = _trim_chat_history_for_frame(result, budget=1000)

    assert out["truncated"] is True
    assert len(out["messages"]) < 4
    assert out["nextOffset"] == 10 + len(out["messages"])
    assert out["hasMore"] is True
    assert out["totalMessages"] == 160


def test_trim_leaves_unpaged_responses_without_a_cursor():
    # No `offset` in the request == the legacy unpaged shape. Trimming must
    # not invent a cursor the client cannot interpret.
    big = "x" * 400
    result = {"messages": [{"role": "user", "content": big} for _ in range(4)]}
    out = _trim_chat_history_for_frame(result, budget=1000)

    assert out["truncated"] is True
    assert "nextOffset" not in out
    assert "hasMore" not in out


def test_a_single_oversized_message_is_elided_so_the_cursor_advances():
    # One message alone blows the whole budget. Dropping it entirely would
    # leave nextOffset == offset and the client would spin on this page.
    huge = {
        "role": "toolResult",
        "content": [{"type": "text", "text": "y" * 64_000}],
    }
    result = {
        "messages": [huge],
        "offset": 0,
        "nextOffset": 1,
        "hasMore": True,
        "totalMessages": 160,
    }
    out = _trim_chat_history_for_frame(result, budget=1000)

    assert len(out["messages"]) == 1
    assert out["truncated"] is True
    # Cursor advanced past the offending message.
    assert out["nextOffset"] == 1
    assert out["hasMore"] is True
    # Payload now fits.
    assert len(json.dumps(out, separators=(",", ":"), default=str).encode()) <= 1000
    # The elision is visible rather than silent.
    assert "omitted" in out["messages"][0]["content"][0]["text"]


def test_many_subthreshold_blocks_that_sum_over_budget_are_hard_truncated():
    # _elide_oversized_message only shrinks a block that ALONE crosses
    # budget // 4. Five blocks under that threshold (27_000 < 27_500) but
    # summing to 135_000 bytes — well over the budget — defeat it: every
    # block passes through unchanged and the payload still doesn't fit. The
    # post-elision size check must catch this and hard-truncate.
    budget = _WS_FRAME_BUDGET_BYTES  # threshold = budget // 4 == 27_500
    blocks = [{"type": "text", "text": "z" * 27_000} for _ in range(5)]
    huge = {"role": "toolResult", "content": blocks}
    result = {
        "messages": [huge],
        "offset": 0,
        "nextOffset": 1,
        "hasMore": True,
        "totalMessages": 160,
    }

    out = _trim_chat_history_for_frame(result, budget=budget)

    assert len(out["messages"]) == 1
    assert _payload_size(out) <= budget
    assert out["nextOffset"] == 1
    assert out["hasMore"] is True


def test_string_content_message_is_hard_truncated_when_oversized():
    # `content` as a plain string (not a block list) is a shape the codebase
    # treats as valid (see test_trim_rewrites_next_offset_for_a_paged_response
    # above). _elide_oversized_message is a no-op on it (`if not
    # isinstance(content, list): return message`), so an oversized message of
    # this shape ships completely unmodified unless the fallback catches it.
    huge = {"role": "toolResult", "content": "s" * 64_000}
    result = {
        "messages": [huge],
        "offset": 0,
        "nextOffset": 1,
        "hasMore": True,
        "totalMessages": 160,
    }

    out = _trim_chat_history_for_frame(result, budget=1000)

    assert len(out["messages"]) == 1
    assert _payload_size(out) <= 1000
    assert out["nextOffset"] == 1
    assert out["hasMore"] is True
