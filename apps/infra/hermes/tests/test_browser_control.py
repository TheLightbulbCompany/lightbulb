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
        ("acquire", {"runId": "run-1", "agentId": "agent-1"}, 60.0),
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


def test_signed_url_stays_stable_until_refresh_and_then_requires_snapshot(monkeypatch):
    monkeypatch.setenv('AGENT_BROWSER_CDP', 'http://127.0.0.1:8080')
    clock = [1000.0]
    monkeypatch.setattr(browser_control.time, 'time', lambda: clock[0])
    urls = iter(('wss://aws.example/first', 'wss://aws.example/second', 'wss://aws.example/third'))
    with patch.object(browser_control, '_post', side_effect=lambda *a, **k: {'generation': 'stable', 'cdpUrl': next(urls)}), patch.object(browser_control, '_disconnect', return_value=True) as disconnect:
        state = browser_control.begin_turn('url-cache-run', 'agent')
        assert browser_control._refresh(state, 'task')
        state['snapshotGeneration'] = 'stable'
        clock[0] += 5
        assert browser_control._refresh(state, 'task')
        assert browser_control.discovery_payload()['webSocketDebuggerUrl'] == 'wss://aws.example/first'
        assert state['snapshotGeneration'] == 'stable'
        clock[0] += 240
        assert browser_control._refresh(state, 'task')
        assert browser_control.discovery_payload()['webSocketDebuggerUrl'] == 'wss://aws.example/third'
        assert state['snapshotGeneration'] is None
        disconnect.assert_called_once_with('task')
    browser_control._TURN.set(None)
    browser_control._CURRENT.clear()


def test_a_lease_held_by_another_run_says_so(monkeypatch):
    """A 409 from acquire means another agent run holds the browser -- a person never does."""
    monkeypatch.setenv("AGENT_BROWSER_CDP", "http://127.0.0.1:8080")
    with patch.object(browser_control, "_post", return_value={"conflict": True}):
        browser_control.begin_turn("run-1", "agent-1")
        result = browser_control.pre_tool_call("browser_navigate", {})
    assert result["action"] == "block"
    assert "Another agent run" in result["message"]


def test_page_scripts_are_refused_before_any_lease(monkeypatch):
    """People sign in in this browser: no tool may run page JS that could read a typed password."""
    monkeypatch.setenv("AGENT_BROWSER_CDP", "http://127.0.0.1:8080")
    with patch.object(browser_control, "_post") as post:
        browser_control.begin_turn("run-1", "agent-1")
        for name, args in (
            ("browser_console", {"expression": "document.querySelector('input[type=password]').value"}),
            ("browser_cdp", {"method": "Runtime.evaluate"}),
        ):
            result = browser_control.pre_tool_call(name, args)
            assert result["action"] == "block" and "scripts" in result["message"]
        post.assert_not_called()
        post.return_value = {"generation": "g1", "cdpUrl": "wss://aws.example/automation"}
        # Reading console messages (no expression) is not script evaluation.
        assert browser_control.pre_tool_call("browser_console", {}, tool_call_id="c") is not None  # needs a snapshot
    browser_control._TURN.set(None)


def test_agent_yields_until_the_person_is_quiet_then_resnapshots(monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_CDP", "http://127.0.0.1:8080")
    clock = [1000.0]
    monkeypatch.setattr(browser_control.time, "time", lambda: clock[0])
    monkeypatch.setattr(browser_control.time, "monotonic", lambda: clock[0])
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(browser_control.time, "sleep", sleep)
    person = {"at": 0.0}
    def acquire(*_a, **_k):
        at = clock[0] if person.get("typing") else person["at"]
        return {"generation": "g1", "cdpUrl": "wss://aws.example/automation", "humanInputAt": at}

    with patch.object(browser_control, "_post", side_effect=acquire), patch.object(browser_control, "_disconnect", return_value=True):
        browser_control.begin_turn("run-1", "agent-1")
        assert browser_control.pre_tool_call("browser_snapshot", {}, tool_call_id="s1") is None
        browser_control.post_tool_call("browser_snapshot", status="ok", tool_call_id="s1")
        assert browser_control.pre_tool_call("browser_click", {}) is None  # nobody there: no wait
        assert slept == []

        clock[0] += 10
        person["at"] = clock[0] - 1  # a person clicked a second ago
        result = browser_control.pre_tool_call("browser_click", {})
        assert slept == [2.0]  # waited for 3 s of quiet
        assert result["action"] == "block" and "fresh browser_snapshot" in result["message"]
        assert browser_control.pre_tool_call("browser_snapshot", {}, tool_call_id="s2") is None
        browser_control.post_tool_call("browser_snapshot", status="ok", tool_call_id="s2")
        assert browser_control.pre_tool_call("browser_click", {}) is None

        person["typing"] = True  # a person who never stops typing holds a turn at most YIELD_MAX_S
        slept.clear()
        browser_control.pre_tool_call("browser_click", {})
        assert browser_control.YIELD_MAX_S - 3 <= sum(slept) <= browser_control.YIELD_MAX_S
    browser_control._TURN.set(None)
    browser_control._CURRENT.clear()


def test_post_maps_http_409_to_conflict(monkeypatch):
    import urllib.error

    def raise_409(*_a, **_k):
        raise urllib.error.HTTPError("u", 409, "Conflict", None, None)

    monkeypatch.setattr(browser_control.urllib.request, "urlopen", raise_409)
    assert browser_control._post("acquire", {}) == {"conflict": True}

    def raise_500(*_a, **_k):
        raise urllib.error.HTTPError("u", 500, "err", None, None)

    monkeypatch.setattr(browser_control.urllib.request, "urlopen", raise_500)
    assert browser_control._post("acquire", {}) is None
