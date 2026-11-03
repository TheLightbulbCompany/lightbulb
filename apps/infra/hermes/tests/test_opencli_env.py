"""OPENCLI_CDP_ENDPOINT rides each run's shell env; a terminal opencli call acquires the browser."""
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from bridge import browser_control, turn

CDP = "ws://127.0.0.1:9222/devtools/browser/key"


def _stub_scopes(monkeypatch, seen):
    @contextmanager
    def secret_scope(agent_id, overlay=None):
        seen["overlay"] = dict(overlay or {})
        yield

    monkeypatch.setattr(turn, "_secret_scope", secret_scope)
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _a: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _a: None)
    monkeypatch.setattr(turn.browser_control, "begin_turn", lambda *a, **k: {})
    monkeypatch.setattr(turn.browser_control, "release", lambda _l: None)
    monkeypatch.setattr(turn.browser_control, "cdp_url", lambda: CDP)
    monkeypatch.setattr(turn, "_desktop_active_turn", lambda *a: nullcontext())


def test_room_turn_overlay_carries_opencli_endpoint(monkeypatch):
    seen = {}
    _stub_scopes(monkeypatch, seen)
    with turn._room_turn_env("agent-1", "k", "k", "run-1", {}, None, None):
        pass
    assert seen["overlay"]["OPENCLI_CDP_ENDPOINT"] == CDP


def test_paperclip_run_overlay_carries_it(home, monkeypatch):
    (home / "profiles" / "agent-1").mkdir(parents=True)
    seen = {}
    _stub_scopes(monkeypatch, seen)
    monkeypatch.setattr(turn, "_construct_agent", lambda *a: SimpleNamespace(
        run_conversation=lambda **k: {"api_calls": 1, "final_response": "ok"}))
    identity = {"runId": "r1", "companyId": "c1", "agentId": "agent-1", "sessionId": "s1",
                "paperclipApiUrl": "http://p"}
    turn._run_native_paperclip(identity, {"input": "x", "instructions": "y"})
    assert seen["overlay"]["OPENCLI_CDP_ENDPOINT"] == CDP


@pytest.fixture
def refreshed(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_control, "_refresh", lambda state, deadline=None: calls.append(state) or False)
    yield calls
    browser_control._TURN.set(None)


def test_terminal_opencli_call_refreshes_upstream(refreshed):
    state = {"runId": "r"}
    browser_control._TURN.set(state)
    cmd = {"command": "opencli browser --session t open https://x"}
    assert browser_control.pre_tool_call("terminal", cmd) is None
    assert refreshed == [state]


def test_terminal_opencli_without_state_does_not_block(refreshed):
    browser_control._TURN.set(None)
    assert browser_control.pre_tool_call("terminal", {"command": "opencli doctor"}) is None
    assert refreshed == []


def test_plain_terminal_call_is_untouched(refreshed):
    browser_control._TURN.set({"runId": "r"})
    assert browser_control.pre_tool_call("terminal", {"command": "ls"}) is None
    assert refreshed == []


def test_room_turn_overlay_carries_run_id_only_when_there_is_one(monkeypatch):
    seen = {}
    _stub_scopes(monkeypatch, seen)
    with turn._room_turn_env("agent-1", "k", "k", "run-1", {}, None, None):
        pass
    assert seen["overlay"]["LIGHTBULB_RUN_ID"] == "run-1"
    with turn._room_turn_env("agent-1", "k", "k", None, {}, None, None):
        pass
    assert "LIGHTBULB_RUN_ID" not in seen["overlay"]


def test_concurrent_room_turns_keep_their_own_run_id(monkeypatch):
    import threading

    overlays, ready = [], threading.Barrier(2)
    _stub_scopes(monkeypatch, {})

    @contextmanager
    def secret_scope(agent_id, overlay=None):
        overlays.append(overlay)
        yield

    monkeypatch.setattr(turn, "_secret_scope", secret_scope)

    def run(rid):
        ready.wait()
        with turn._room_turn_env("agent-1", "k", "k", rid, {}, None, None):
            pass

    ts = [threading.Thread(target=run, args=(r,)) for r in ("A", "B")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(o["LIGHTBULB_RUN_ID"] for o in overlays) == ["A", "B"]
