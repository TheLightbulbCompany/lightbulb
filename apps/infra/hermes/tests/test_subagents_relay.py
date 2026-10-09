"""Subagent event relay (spec 2026-10-09 background subagents, Bridge)."""
import pytest

import factories as f
from bridge import subagents, turn

KEY = turn.lightbulb_session_key("ceo", f.ROOM)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    subagents._pending.clear()
    subagents.OWNER_OF.clear()
    monkeypatch.setattr(subagents, "_ensure_started", lambda: None)
    yield
    subagents._pending.clear()
    subagents.OWNER_OF.clear()


def _kw(**over):
    return {"subagent_id": "sa-1", "goal": "Map Paperclip", "task_index": 0, "task_count": 3,
            "delegation_id": "d-1", "model": "gpt-5.6-sol", "tool_count": 2, **over}


def test_payload_matches_upstream_progress_subagent():
    p = subagents.payload("subagent.tool", "web_search", "paperclip artifacts", _kw())
    assert p["goal"] == "Map Paperclip" and p["task_count"] == 3 and p["task_index"] == 0
    assert p["subagent_id"] == "sa-1" and p["delegation_id"] == "d-1" and p["tool_count"] == 2
    assert p["tool_name"] == "web_search"
    assert p["tool_preview"] == "paperclip artifacts" and p["text"] == "paperclip artifacts"


def test_text_events_are_never_sent():
    subagents.relay(KEY, "subagent.text", None, "tok", _kw())
    assert subagents._pending == {}


def test_thinking_coalesces_per_subagent_but_tools_do_not():
    subagents.relay(KEY, "subagent.thinking", None, "a", _kw())
    subagents.relay(KEY, "subagent.tool", "read_file", "x", _kw())
    subagents.relay(KEY, "subagent.thinking", None, "b", _kw())
    subagents.relay(KEY, "subagent.tool", "read_file", "y", _kw())
    types = [(e["type"], e["payload"].get("text")) for e in subagents._pending[KEY]]
    assert types == [("subagent.tool", "x"), ("subagent.thinking", "b"), ("subagent.tool", "y")]


def test_relay_records_ownership_and_complete_releases_it():
    subagents.relay(KEY, "subagent.start", None, "Map Paperclip", _kw())
    assert subagents.OWNER_OF["sa-1"] == KEY
    subagents.relay(KEY, "subagent.complete", None, "done", _kw(status="completed"))
    assert "sa-1" not in subagents.OWNER_OF


def test_no_session_key_is_a_noop():
    subagents.relay(None, "subagent.start", None, "x", _kw())
    assert subagents._pending == {} and subagents.OWNER_OF == {}


def test_flush_posts_batches_of_25_in_order(monkeypatch):
    posts = []
    monkeypatch.setattr(turn, "_post_backend", lambda path, body: posts.append((path, body)) or 200)
    for i in range(30):
        subagents.relay(KEY, "subagent.tool", "t", str(i), _kw())
    subagents.flush_once()
    assert [p[0] for p in posts] == ["subagents/org_test/events"] * 2
    assert [len(p[1]["events"]) for p in posts] == [25, 5]
    seqs = [e["seq"] for p in posts for e in p[1]["events"]]
    assert seqs == sorted(seqs) and posts[0][1]["sessionKey"] == KEY
    assert subagents._pending == {}


def test_subagent_events_after_finish_go_to_relay_not_frames(monkeypatch):
    sent, relayed = [], []
    pub = turn.FramePublisher("https://appsync.invalid/event", "/ws/w/rooms/r/k", f.ROOM, "ceo", "run-1", None,
                              publish=lambda *a: sent.append(a) or 200, session_key=KEY)
    monkeypatch.setattr(subagents, "relay", lambda *a: relayed.append(a))
    pub.finished("completed", "", None)
    sent.clear()
    pub.activity("subagent.tool", "web_search", "q", None, **_kw())
    assert relayed and relayed[0][0] == KEY and relayed[0][1] == "subagent.tool"
    assert sent == []  # the closed run's channel gets nothing


def test_malformed_numbers_never_raise_and_are_skipped():
    bad = _kw(task_count="2/3", task_index="x", depth="x", duration_seconds="?")
    p = subagents.payload("subagent.start", None, "g", bad)
    assert p["task_count"] == 1 and p["task_index"] == 0
    assert "depth" not in p and "duration_seconds" not in p
    subagents.relay(KEY, "subagent.start", None, "g", bad)
    assert len(subagents._pending[KEY]) == 1


def test_relay_swallows_an_unexpected_payload_failure(monkeypatch):
    monkeypatch.setattr(subagents, "payload", lambda *a: 1 / 0)
    subagents.relay(KEY, "subagent.start", None, "g", _kw())
    assert subagents._pending == {}
