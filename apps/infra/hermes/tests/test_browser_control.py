from __future__ import annotations

import pytest
from bridge import browser_control

URL = "wss://bedrock-agentcore.us-east-1.amazonaws.com/browser-streams/aws.browser.v1/sessions/01ABC/automation?X-Amz-Signature=s"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("BROWSER_CDP_URL", "ws://127.0.0.1:9222")
    monkeypatch.setattr(browser_control, "_LEASE", {})
    monkeypatch.setattr(browser_control, "_BROWSING", {})
    monkeypatch.setattr(browser_control, "_timer", None)
    mux = {"upstream": [], "closed": [], "disconnects": 0}
    monkeypatch.setattr(browser_control.cdp_mux, "set_upstream", lambda url, gen: mux["upstream"].append((url, gen)))
    monkeypatch.setattr(browser_control.cdp_mux, "close_run", mux["closed"].append)
    monkeypatch.setattr(browser_control.cdp_mux, "disconnect", lambda: mux.__setitem__("disconnects", mux["disconnects"] + 1))
    monkeypatch.setattr(browser_control.cdp_mux, "url_for", lambda run: f"ws://127.0.0.1:9222/devtools/browser/{run}")
    monkeypatch.setattr(browser_control, "_disconnect", lambda task_id: True)
    yield mux
    browser_control._TURN.set(None)
    if browser_control._timer:
        browser_control._timer.cancel()


def _acquire(generation="g1", **extra):
    return lambda path, body, timeout=20.0: {"generation": generation, "cdpUrl": URL, **extra}


def test_pre_tool_call_fails_closed_without_a_turn():
    assert browser_control.pre_tool_call("browser_navigate", {})["action"] == "block"
    assert browser_control.pre_tool_call("computer_use", {})["action"] == "block"
    assert browser_control.pre_tool_call("terminal", {}) is None


def test_concurrent_runs_share_the_bridge_lease_and_release_after_the_last(monkeypatch, clean):
    """No per-run lease: both runs acquire as the bridge; the save waits for the last run."""
    calls = []
    monkeypatch.setattr(browser_control, "_post", lambda path, body, timeout=20.0: (
        calls.append((path, body)) or {"generation": "g1", "cdpUrl": URL}))
    first = browser_control.begin_turn("run-1", "ceo")
    assert browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1") is None
    second = browser_control.begin_turn("run-2", "growth")
    assert browser_control.pre_tool_call("browser_snapshot", {}, task_id="t2") is None
    assert [body["runId"] for _, body in calls] == ["bridge", "bridge"]
    assert browser_control.cdp_url() == "ws://127.0.0.1:9222/devtools/browser/run-2"

    browser_control.release(first)
    assert clean["closed"] == ["run-1"] and browser_control._timer is None  # run-2 still browsing
    browser_control.release(second)
    assert clean["closed"] == ["run-1", "run-2"] and browser_control._timer is not None
    browser_control._timer.cancel()
    browser_control._release_if_idle()
    assert clean["disconnects"] == 1  # the connection goes back before the save needs it
    assert calls[-1] == ("release", {"runId": "bridge", "generation": "g1"})


def test_a_run_that_starts_browsing_cancels_the_pending_release(monkeypatch):
    monkeypatch.setattr(browser_control, "_post", _acquire())
    first = browser_control.begin_turn("run-1", "ceo")
    browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1")
    browser_control.release(first)
    assert browser_control._timer is not None
    browser_control.begin_turn("run-2", "ceo")
    browser_control.pre_tool_call("browser_snapshot", {}, task_id="t2")
    assert browser_control._timer is None


def test_a_turn_that_never_browsed_releases_nothing(clean):
    browser_control.release(browser_control.begin_turn("run-1", "ceo"))
    assert clean["closed"] == [] and browser_control._timer is None


def test_generation_change_reaps_the_runs_clients_and_requires_a_snapshot(monkeypatch, clean):
    generations = iter(("g1", "g2"))
    monkeypatch.setattr(browser_control, "_post", lambda *a, **k: {"generation": next(generations), "cdpUrl": URL})
    reaped = []
    monkeypatch.setattr(browser_control, "_disconnect", lambda task_id: reaped.append(task_id) or True)
    browser_control.begin_turn("run-1", "ceo")
    assert browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1", tool_call_id="s1") is None
    browser_control.post_tool_call("browser_snapshot", status="ok", tool_call_id="s1")
    assert browser_control.pre_tool_call("browser_click", {}, task_id="t1")["action"] == "block"
    assert reaped == ["t1"]
    assert [gen for _url, gen in clean["upstream"]] == ["g1", "g2"]


def test_snapshot_is_fresh_only_after_success(monkeypatch):
    monkeypatch.setattr(browser_control, "_post", _acquire())
    browser_control.begin_turn("run-1", "ceo")
    assert browser_control.pre_tool_call("browser_snapshot", {}, tool_call_id="s1") is None
    browser_control.post_tool_call("browser_snapshot", status="error", tool_call_id="s1")
    assert browser_control.pre_tool_call("browser_click", {})["action"] == "block"
    assert browser_control.pre_tool_call("browser_snapshot", {}, tool_call_id="s2") is None
    browser_control.post_tool_call("browser_snapshot", status="ok", tool_call_id="s2")
    assert browser_control.pre_tool_call("browser_click", {}) is None


def test_cdp_dialog_and_screen_tools_acquire_but_need_no_snapshot(monkeypatch):
    """Owner ruling 2026-09-27: agents control everything in this browser."""
    monkeypatch.setattr(browser_control, "_post", _acquire())
    browser_control.begin_turn("run-1", "ceo")
    for tool in ("browser_cdp", "browser_dialog", "computer_use"):
        assert browser_control.pre_tool_call(tool, {}, task_id="t1") is None
    assert browser_control.pre_tool_call("browser_click", {})["action"] == "block"
    assert browser_control._BROWSING == {"run-1": {"t1", "default"}}


def test_session_ref_is_the_held_session():
    assert browser_control.session_ref() is None
    browser_control._LEASE.update(generation="g1", cdpUrl=URL)
    assert browser_control.session_ref() == {"browserIdentifier": "aws.browser.v1", "sessionId": "01ABC"}


def test_no_browser_profile_means_no_endpoint(monkeypatch):
    monkeypatch.delenv("BROWSER_CDP_URL")
    browser_control.begin_turn("run-1", "ceo")
    assert browser_control.cdp_url() == ""
    assert browser_control.pre_tool_call("browser_navigate", {})["action"] == "block"


def test_install_points_hermes_cdp_resolver_at_the_turn(monkeypatch):
    import sys
    import types

    tools = types.ModuleType("tools")
    cdp = types.ModuleType("tools.browser_tool_cdp")
    tools.browser_tool_cdp = cdp
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.browser_tool_cdp", cdp)
    browser_control.install()
    browser_control.begin_turn("run-9", "ceo")
    assert cdp._get_cdp_override() == "ws://127.0.0.1:9222/devtools/browser/run-9"


def test_agent_yields_until_the_person_is_quiet_then_resnapshots(monkeypatch):
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
        return {"generation": "g1", "cdpUrl": URL, "humanInputAt": at}

    monkeypatch.setattr(browser_control, "_post", acquire)
    browser_control.begin_turn("run-1", "ceo")
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


def test_cold_start_polls_acquire_until_the_browser_is_up(monkeypatch):
    monkeypatch.setattr(browser_control.time, "sleep", lambda _s: None)
    replies = iter(({"status": "starting"}, {"status": "starting"}, {"generation": "g1", "cdpUrl": URL}))
    posted = []
    monkeypatch.setattr(browser_control, "_post", lambda *a, **k: posted.append(a) or next(replies))
    state = browser_control.begin_turn("run-1", "ceo")
    assert browser_control.pre_tool_call("browser_snapshot", {}, tool_call_id="s1") is None
    assert len(posted) == 3 and state["generation"] == "g1"


def _clock(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(browser_control.time, "time", lambda: clock[0])
    monkeypatch.setattr(browser_control.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(browser_control.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    return clock


def test_hook_returns_within_budget_while_the_browser_keeps_starting(monkeypatch):
    # Hermes abandons a pre_tool_call after 30 s and then fails every tool for the turn.
    clock = _clock(monkeypatch)

    def post(_path, _body, timeout=20.0):
        clock[0] += min(timeout, 3.0)  # each request takes a while
        return {"status": "starting"}

    monkeypatch.setattr(browser_control, "_post", post)
    browser_control.begin_turn("run-1", "ceo")
    assert browser_control.pre_tool_call("browser_navigate", {}) == {"action": "block", "message": browser_control.STARTING_MESSAGE}
    assert clock[0] - 1000.0 <= browser_control.HOOK_BUDGET_S + 1 < 30


def test_hook_returns_within_budget_while_a_person_keeps_typing(monkeypatch):
    clock = _clock(monkeypatch)

    def post(_path, _body, timeout=20.0):
        clock[0] += 2.0
        return {"generation": "g1", "cdpUrl": URL, "humanInputAt": clock[0]}

    monkeypatch.setattr(browser_control, "_post", post)
    browser_control.begin_turn("run-1", "ceo")
    browser_control.pre_tool_call("browser_snapshot", {})
    assert clock[0] - 1000.0 <= browser_control.HOOK_BUDGET_S + 3 < 30
    assert browser_control.YIELD_MAX_S < browser_control.HOOK_BUDGET_S


def test_post_maps_any_http_error_to_none(monkeypatch):
    import urllib.error

    def raise_409(*_a, **_k):
        raise urllib.error.HTTPError("u", 409, "Conflict", None, None)

    monkeypatch.setattr(browser_control.urllib.request, "urlopen", raise_409)
    assert browser_control._post("acquire", {}) is None
