from __future__ import annotations

from unittest.mock import call, patch

from bridge import browser_control, server


def test_pre_tool_call_fails_closed_without_lease():
    assert browser_control.pre_tool_call("browser_navigate", {})["action"] == "block"
    assert browser_control.pre_tool_call("terminal", {}) is None


def test_lazy_acquire_refresh_and_release_use_run_fence(monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_CDP", "http://127.0.0.1:8080")
    calls = []

    def post(path, body, timeout=20.0):
        calls.append((path, body, timeout))
        return {"generation": "g1", "cdpUrl": "wss://aws.example/automation"} if path == "acquire" else {"ok": True}

    with patch.object(browser_control, "_disconnect", return_value=True) as disconnect, patch.object(browser_control, "_post", side_effect=post):
        state = browser_control.begin_turn("run-1", "agent-1")
        assert browser_control.pre_tool_call("browser_snapshot", {}, task_id="task-1", tool_call_id="snapshot-1") is None
        browser_control.post_tool_call("browser_snapshot", status="ok", tool_call_id="snapshot-1")
        browser_control.release(state)
    assert calls == [
        ("acquire", {"runId": "run-1", "agentId": "agent-1"}, 10.0),
        ("release", {"runId": "run-1", "generation": "g1"}, 190.0),
    ]
    disconnect.assert_called_once_with("task-1")
    assert browser_control.pre_tool_call("browser_navigate", {})["action"] == "block"


def test_generation_change_requires_fresh_snapshot(monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_CDP", "http://127.0.0.1:8080")
    generations = iter(("g1", "g2"))
    with patch.object(browser_control, "_disconnect", return_value=True) as disconnect, patch.object(browser_control, "_post", side_effect=lambda path, body, timeout=20.0: (
        {"generation": next(generations), "cdpUrl": "wss://aws.example/automation"}
        if path == "acquire" else {"ok": True}
    )):
        state = browser_control.begin_turn("run-1", "agent-1")
        assert browser_control.pre_tool_call("browser_snapshot", {}, tool_call_id="snapshot-1") is None
        browser_control.post_tool_call("browser_snapshot", status="ok", tool_call_id="snapshot-1")
        assert browser_control.pre_tool_call("browser_click", {}, task_id="task-1")["action"] == "block"
        browser_control.release(state)
    assert disconnect.call_args_list == [call("task-1"), call("task-1")]


def test_snapshot_is_fresh_only_after_success(monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_CDP", "http://127.0.0.1:8080")
    with patch.object(browser_control, "_post", return_value={
        "generation": "g1", "cdpUrl": "wss://aws.example/automation"
    }):
        state = browser_control.begin_turn("run-1", "agent-1")
        assert browser_control.pre_tool_call("browser_snapshot", {}, tool_call_id="snapshot-1") is None
        browser_control.post_tool_call("browser_snapshot", status="error", tool_call_id="snapshot-1")
        assert browser_control.pre_tool_call("browser_click", {})["action"] == "block"
        assert browser_control.pre_tool_call("browser_snapshot", {}, tool_call_id="snapshot-2") is None
        browser_control.post_tool_call("browser_snapshot", status="ok", tool_call_id="snapshot-2")
        assert browser_control.pre_tool_call("browser_click", {}) is None
        browser_control.release(state)


def test_release_disconnects_turn_client_before_checkpoint(monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_CDP", "http://127.0.0.1:8080")
    events = []

    def post(path, body, timeout=20.0):
        events.append(path)
        return {"generation": "g1", "cdpUrl": "wss://aws.example/automation"}

    with patch.object(browser_control, "_post", side_effect=post), patch.object(
        browser_control, "_disconnect", side_effect=lambda task_id: events.append(f"disconnect:{task_id}") or True
    ):
        state = browser_control.begin_turn("run-1", "agent-1")
        assert browser_control.pre_tool_call("browser_snapshot", {}, task_id="task-1") is None
        browser_control.release(state)

    assert events == ["acquire", "disconnect:task-1", "release"]


def test_runless_turns_never_share_a_controller_identity():
    first = browser_control.begin_turn("", "ceo")
    second = browser_control.begin_turn("", "ceo")
    assert first["runId"] != second["runId"]
    browser_control.release(second)
