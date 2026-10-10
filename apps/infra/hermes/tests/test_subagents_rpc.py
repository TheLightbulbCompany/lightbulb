"""Subagent RPCs: ports of Hermes tui_gateway/methods_subagents.py, owned by room session key."""
import sys
import threading
import types

import pytest

import factories as f
from bridge import server, subagents, turn

KEY = turn.lightbulb_session_key("ceo", f.ROOM)
OTHER = turn.lightbulb_session_key("finance", f.ROOM)


class _Agent:
    def __init__(self, path=None):
        self._live_transcript_path = path
        self.steered, self.interrupted = [], False


@pytest.fixture
def registry(monkeypatch, tmp_path):
    transcript = tmp_path / "child.log"
    transcript.write_bytes(b"x" * 20000 + b"\nlast line")
    mine, theirs = _Agent(str(transcript)), _Agent()
    reg = types.ModuleType("tools.delegate_tool_registry")
    reg._active_subagents_lock = threading.Lock()
    reg._active_subagents = {
        "sa-1": {"subagent_id": "sa-1", "goal": "Map Paperclip", "status": "running", "depth": 0,
                 "started_at": 1760000000.0, "tool_count": 4, "last_tool": "web_search",
                 "accepting_steer": True, "agent": mine},
        "sa-2": {"subagent_id": "sa-2", "goal": "Other room", "status": "running", "agent": theirs},
    }
    reg.steer_subagent = lambda sid, text: reg._active_subagents[sid]["agent"].steered.append(text) or True
    reg.interrupt_subagent = lambda sid: setattr(reg._active_subagents[sid]["agent"], "interrupted", True) or True
    monkeypatch.setitem(sys.modules, "tools.delegate_tool_registry", reg)
    subagents.OWNER_OF.clear()
    subagents.OWNER_OF.update({"sa-1": KEY, "sa-2": OTHER})
    yield reg, mine, theirs
    subagents.OWNER_OF.clear()


def _call(method, params):
    return server.RPC_METHODS[method](params, timeout_ms=1000)


def test_list_only_returns_subagents_of_the_given_keys(registry):
    out = _call("subagent.list", {"sessionKeys": [KEY]})
    assert out["ok"] is True
    [row] = out["payload"]["subagents"]
    assert row["subagent_id"] == "sa-1" and row["session_key"] == KEY and row["last_tool"] == "web_search"
    assert "agent" not in row


def test_list_without_hermes_loaded_is_empty(monkeypatch):
    monkeypatch.delitem(sys.modules, "tools.delegate_tool_registry", raising=False)
    assert _call("subagent.list", {"sessionKeys": [KEY]}) == {"ok": True, "payload": {"subagents": []}}


def test_tail_reads_the_last_16k(registry):
    out = _call("subagent.tail", {"sessionKey": KEY, "subagentId": "sa-1"})["payload"]
    assert out["available"] is True and out["truncated"] is True
    assert len(out["text"].encode()) <= 16384 and out["text"].endswith("last line")


def test_controls_refuse_another_sessions_subagent(registry):
    _reg, _mine, theirs = registry
    assert _call("subagent.steer", {"sessionKey": KEY, "subagentId": "sa-2", "text": "go"})["payload"] == {"queued": False}
    assert _call("subagent.interrupt", {"sessionKey": KEY, "subagentId": "sa-2"})["payload"] == {"found": False}
    assert _call("subagent.tail", {"sessionKey": KEY, "subagentId": "sa-2"})["payload"]["available"] is False
    assert theirs.steered == [] and theirs.interrupted is False


def test_steer_and_interrupt_own_subagent(registry):
    _reg, mine, _theirs = registry
    assert _call("subagent.steer", {"sessionKey": KEY, "subagentId": "sa-1", "text": "focus on Library"})["payload"] == {"queued": True}
    assert mine.steered == ["focus on Library"]
    assert _call("subagent.interrupt", {"sessionKey": KEY, "subagentId": "sa-1"})["payload"] == {"found": True}
    assert mine.interrupted is True


@pytest.mark.parametrize("method,params", [
    ("subagent.list", {}), ("subagent.list", {"sessionKeys": "x"}),
    ("subagent.tail", {"sessionKey": KEY}), ("subagent.steer", {"sessionKey": KEY, "subagentId": "sa-1"}),
])
def test_bad_params_are_invalid(registry, method, params):
    assert _call(method, params)["error"]["code"] == "INVALID"
