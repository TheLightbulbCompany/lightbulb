from __future__ import annotations

import json

import pytest
from bridge import browser_control

URL = "wss://bedrock-agentcore.us-east-1.amazonaws.com/browser-streams/aws.browser.v1/sessions/01ABC/automation?X-Amz-Signature=s"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("BROWSER_CDP_URL", "ws://127.0.0.1:9222")
    monkeypatch.setattr(browser_control, "_LEASE", {})
    monkeypatch.setattr(browser_control, "_BROWSING", {})
    monkeypatch.setattr(browser_control, "_timer", None)
    monkeypatch.setattr(browser_control, "_SINKS", {})
    monkeypatch.setattr(browser_control, "_frames_until", 0.0)
    monkeypatch.setattr(browser_control, "_MAC_STATES", {})
    mux = {"upstream": [], "closed": [], "disconnects": 0, "casts": [], "dropped": [], "shown": [],
           "human": {}, "close": {}}
    cloud = browser_control.cdp_mux.CLOUD
    monkeypatch.setattr(browser_control.cdp_mux, "cast", lambda run, sink, ttl, resend=False: mux["casts"].append(
        (run, sink, round(ttl), resend)))
    monkeypatch.setattr(browser_control.cdp_mux, "set_upstream", lambda url, gen, upstream=cloud: mux["upstream"].append(
        (url, gen) if upstream == cloud else (url, gen, upstream)))
    monkeypatch.setattr(browser_control.cdp_mux, "close_run", lambda run, upstream=None: mux["closed"].append(
        run if upstream is None else (run, upstream)))
    monkeypatch.setattr(browser_control.cdp_mux, "disconnect", lambda: mux.__setitem__("disconnects", mux["disconnects"] + 1))
    monkeypatch.setattr(browser_control.cdp_mux, "url_for", lambda run, upstream=cloud: (
        f"ws://127.0.0.1:9222/devtools/browser/{run}" if upstream == cloud else f"ws://mux/{upstream}/{run}"))
    monkeypatch.setattr(browser_control.cdp_mux, "drop", mux["dropped"].append)
    monkeypatch.setattr(browser_control.cdp_mux, "show", lambda run, upstream: mux["shown"].append((run, upstream)) or True)
    monkeypatch.setattr(browser_control.cdp_mux, "human_input_at", lambda upstream: mux["human"].get(upstream, 0.0))
    monkeypatch.setattr(browser_control.cdp_mux, "last_close", lambda upstream: mux["close"].pop(upstream, None))
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


def test_navigate_needs_no_prior_snapshot_and_counts_as_one(monkeypatch):
    """Hermes: browser_navigate must come first and returns a compact snapshot itself."""
    monkeypatch.setattr(browser_control, "_post", _acquire())
    browser_control.begin_turn("run-1", "ceo")
    assert browser_control.pre_tool_call("browser_navigate", {}, tool_call_id="n1") is None
    browser_control.post_tool_call("browser_navigate", status="ok", tool_call_id="n1")
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


def test_profiles_let_agents_open_any_page_in_the_shared_browser(home):
    """CDP mode makes Hermes treat the browser as off-host; its URL guard reads the RAW profile
    config, so the setting is persisted there (about:, chrome://, file:// pages)."""
    from bridge import sync

    sync.write_profile_config("ceo", {"browser": {"backend": "browser-use", "cloud_provider": "x"}})
    assert sync.normalize_profile_backends("ceo")["browser"] == {
        "backend": "off", "cloud_provider": "x", "allow_private_urls": True}
    assert sync.read_profile_config("ceo")["browser"]["allow_private_urls"] is True


def test_frames_are_cast_only_while_someone_watches_and_only_for_room_runs(monkeypatch, clean):
    monkeypatch.setattr(browser_control.time, "time", lambda: 1000.0)
    sink = object()
    monkeypatch.setattr(browser_control, "_post", _acquire())  # nobody watching frames
    browser_control.begin_turn("run-1", "ceo", sink)
    browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1")
    assert clean["casts"] == []
    monkeypatch.setattr(browser_control, "_post", _acquire(framesAt=990.0))  # a watcher 10 s ago
    browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1")
    assert clean["casts"] == [("run-1", sink, 35, False)]
    browser_control.begin_turn("run-2", "ceo")  # no room: nowhere to show frames
    browser_control.pre_tool_call("browser_snapshot", {}, task_id="t2")
    assert len(clean["casts"]) == 1


def test_a_watcher_heartbeat_casts_every_browsing_run_and_resends(monkeypatch, clean):
    monkeypatch.setattr(browser_control.time, "time", lambda: 1000.0)
    monkeypatch.setattr(browser_control, "_post", _acquire())
    sink = object()
    state = browser_control.begin_turn("run-1", "ceo", sink)
    browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1")
    assert browser_control.frames_wanted(45) == 1
    assert clean["casts"] == [("run-1", sink, 45, True)]
    browser_control.release(state)
    assert browser_control.frames_wanted(45) == 0  # an ended run is never cast again


def test_input_goes_to_the_named_run(monkeypatch):
    sent = []
    monkeypatch.setattr(browser_control.cdp_mux, "dispatch", lambda run, event: sent.append((run, event["type"])))
    browser_control.dispatch_input({"runId": "run-1", "type": "click", "x": 1, "y": 2})
    browser_control.dispatch_input({"type": "text", "text": "hi"})
    assert sent == [("run-1", "click"), (None, "text")]


def test_rpc_input_errors_are_codes_never_page_text(monkeypatch):
    from bridge import server

    def boom(event):
        raise {"lookup": LookupError("x"), "cdp": RuntimeError("secret page text")}[event["why"]]

    monkeypatch.setattr(browser_control, "dispatch_input", boom)
    assert server.RPC_METHODS["browser.input"]({"why": "lookup"})["error"]["code"] == "NOT_CASTING"
    failed = server.RPC_METHODS["browser.input"]({"why": "cdp"})["error"]
    assert failed["code"] == "UNAVAILABLE" and "secret" not in failed["message"]


def test_invoke_uses_the_held_session_with_the_runtime_role(monkeypatch):
    import sys
    import types

    calls = []

    class Client:
        def invoke_browser(self, **kw):
            calls.append(kw)
            return {"result": {"screenshot": {"status": "SUCCESS", "data": b"\x89PNG"}}}

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *_a, **_k: Client()))
    with pytest.raises(RuntimeError, match="use a browser tool first"):
        browser_control.invoke({"screenshot": {}})
    browser_control._LEASE.update(generation="g1", cdpUrl=URL)
    assert browser_control.invoke({"screenshot": {"format": "PNG"}}) == {"status": "SUCCESS", "data": "iVBORw=="}
    assert calls == [{"browserIdentifier": "aws.browser.v1", "sessionId": "01ABC", "action": {"screenshot": {"format": "PNG"}}}]


# ---- a paired Mac (use_browser): acquire, one sync engine, release before run.ended ------------

DOOR = "ws://10.0.1.7:8000/api/v1/internal/mac-cdp/ticket"
MAC = "mac:mac-1:run-1"


def _mac_acquire(answers, posted=None, **extra):
    """_post_mac: acquire answers from ``answers`` (a list, last one repeats); release/synced 200."""
    def post(path, body, timeout=20.0):
        if posted is not None:
            posted.append((path, dict(body)))
        if path == "mac/acquire":
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
            return answer if isinstance(answer, tuple) else (200, answer)
        return 200, extra.get(path, {"ok": True} if path == "mac/synced" else {"sync": None})
    return post


def _ready(generation="m1", **extra):
    return {"deviceId": "mac-1", "generation": generation, "cdpUrl": DOOR, "deviceName": "Ada's Mac",
            "humanInputAt": 0, "sync": None, **extra}


def test_post_mac_returns_status_and_detail_on_http_error(monkeypatch):
    import io
    import urllib.error

    def raise_409(*_a, **_k):
        body = io.BytesIO(json.dumps({"detail": {"code": "no_mac", "message": "Your Mac isn't connected."}}).encode())
        raise urllib.error.HTTPError("u", 409, "Conflict", None, body)

    monkeypatch.setattr(browser_control.urllib.request, "urlopen", raise_409)
    assert browser_control._post_mac("mac/acquire", {}) == (409, {"code": "no_mac", "message": "Your Mac isn't connected."})
    monkeypatch.setattr(browser_control.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(OSError("down")))
    assert browser_control._post_mac("mac/acquire", {}) == (0, {})


def test_select_mac_without_device_lets_backend_pick(monkeypatch, clean):
    posted = []
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()], posted))
    state = browser_control.begin_turn("run-1", "ceo")
    assert browser_control.select_mac() is None
    assert posted == [("mac/acquire", {"runId": "run-1", "agentId": "ceo", "family": "room", "budgetS": 25.0})]
    assert state["mac"]["deviceId"] == "mac-1"
    assert browser_control.cdp_url() == f"ws://mux/{MAC}/run-1"
    assert clean["upstream"] == [(DOOR, "m1", MAC)]
    assert browser_control.select_mac("bad id!") == "Invalid deviceId."


def test_refresh_adopts_device_and_generation_from_acquire(monkeypatch, clean):
    posted = []
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready("m1"), _ready("m2")], posted))
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    assert state["mac"] == {"deviceId": "mac-1", "generation": "m1", "deviceName": "Ada's Mac"}
    browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1")
    assert state["mac"]["generation"] == "m2"
    assert posted[-1][1]["deviceId"] == "mac-1"  # adopted: the run sticks to that Mac


def test_new_generation_after_wake_drops_pipe_and_requires_snapshot(monkeypatch, clean):
    reaped = []
    monkeypatch.setattr(browser_control, "_disconnect", lambda task_id: reaped.append(task_id) or True)
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready("m1"), _ready("m1"), _ready("m1"), _ready("m2")]))
    browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    assert browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1", tool_call_id="s1") is None
    browser_control.post_tool_call("browser_snapshot", status="ok", tool_call_id="s1")
    assert browser_control.pre_tool_call("browser_click", {}, task_id="t1") is None
    blocked = browser_control.pre_tool_call("browser_click", {}, task_id="t1")  # the Mac slept and woke
    assert blocked["action"] == "block" and "fresh browser_snapshot" in blocked["message"]
    assert reaped == ["t1"]
    assert [u[1] for u in clean["upstream"]] == ["m1", "m1", "m1", "m2"]  # set_upstream drops the old pipe


def test_select_mac_surfaces_backend_message(monkeypatch, clean):
    message = "Update the Lightbulb desktop app on that Mac to let agents use its browser."
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([(409, {"code": "desktop_update_required", "message": message})]))
    state = browser_control.begin_turn("run-1", "ceo")
    assert browser_control.select_mac() == message
    assert state["mac"] is None
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([(0, {})]))
    assert browser_control.select_mac() == browser_control.MAC_UNAVAILABLE


def test_select_mac_disconnects_cloud_clients(monkeypatch, clean):
    reaped = []
    monkeypatch.setattr(browser_control, "_post", _acquire())
    monkeypatch.setattr(browser_control, "_disconnect", lambda task_id: reaped.append(task_id) or True)
    browser_control.begin_turn("run-1", "ceo")
    browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1")
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()]))
    assert browser_control.select_mac() is None
    assert ("run-1", browser_control.cdp_mux.CLOUD) in clean["closed"] and reaped == ["t1"]


def _checkpoints(monkeypatch, fail=None):
    ran = []

    def checkpoint(params, budget_s=None):
        ran.append(params)
        if params["action"] == fail:
            raise RuntimeError("browser state checkpoint failed")
        return {"ok": True}

    monkeypatch.setattr(browser_control.browser_state, "checkpoint", checkpoint)
    return ran


def _join_sync(state):
    if state.get("macSync") is not None:
        state["macSync"].join(5)


def test_acquire_sync_runs_checkpoint_over_the_mux_then_reports_synced(monkeypatch, clean):
    posted, ran = [], _checkpoints(monkeypatch)
    sync = {"syncId": "L1", "steps": [{"action": "load", "stateUrl": "https://s3/x", "baseOutUrl": "https://s3/b"}]}
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready(sync=sync), _ready()], posted))
    state = browser_control.begin_turn("run-1", "ceo")
    assert browser_control.select_mac() is None
    _join_sync(state)
    assert ran == [{**sync["steps"][0], "source": "mac", "cdpUrl": f"ws://mux/{MAC}/sync:run-1"}]
    assert ("sync:run-1", MAC) in clean["closed"]
    assert posted[-1] == ("mac/synced", {"runId": "run-1", "agentId": "ceo", "family": "room",
                                         "deviceId": "mac-1", "syncId": "L1", "ok": True})


def test_sync_failure_reports_ok_false(monkeypatch, clean):
    posted, ran = [], _checkpoints(monkeypatch, fail="save")
    sync = {"syncId": "L1", "steps": [{"action": "save"}, {"action": "load"}]}
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready(sync=sync)], posted))
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    _join_sync(state)
    assert [p["action"] for p in ran] == ["save"]  # stops at the first failure
    assert posted[-1][0] == "mac/synced" and posted[-1][1]["ok"] is False


def test_failed_save_reports_kind_and_room(monkeypatch, clean):
    posted, _ran = [], _checkpoints(monkeypatch, fail="save")
    sync = {"syncId": "S1", "steps": [{"action": "save"}]}
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()], posted, **{"mac/release": {"sync": sync}}))
    state = browser_control.begin_turn("run-1", "ceo", room_id="room-9")
    browser_control.select_mac()
    browser_control.release(state)
    synced = [body for path, body in posted if path == "mac/synced"]
    assert synced == [{"runId": "run-1", "agentId": "ceo", "family": "room", "deviceId": "mac-1",
                       "syncId": "S1", "ok": False, "kind": "save", "roomId": "room-9"}]


def test_tool_call_blocked_while_mac_sync_runs(monkeypatch, clean):
    import threading

    gate = threading.Event()
    monkeypatch.setattr(browser_control.browser_state, "checkpoint", lambda params, **_: gate.wait(5) and {"ok": True})
    monkeypatch.setattr(browser_control, "HOOK_BUDGET_S", 0.2)
    sync = {"syncId": "L1", "steps": [{"action": "load"}]}
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready(sync=sync), _ready()]))
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    assert browser_control.pre_tool_call("browser_navigate", {}) == {"action": "block", "message": browser_control.MAC_SYNCING}
    gate.set()
    _join_sync(state)
    assert browser_control.pre_tool_call("browser_navigate", {}) is None


def test_release_saves_synchronously_before_returning(monkeypatch, clean):
    posted, ran = [], _checkpoints(monkeypatch)
    sync = {"syncId": "S1", "steps": [{"action": "save"}]}
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()], posted, **{"mac/release": {"sync": sync}}))
    state = browser_control.begin_turn("run-1", "ceo", family="paperclip")
    browser_control.select_mac()
    browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1")
    browser_control.release(state)
    assert [p["action"] for p in ran] == ["save"]
    assert [path for path, _ in posted][-2:] == ["mac/release", "mac/synced"]
    assert posted[-2][1] == {"runId": "run-1", "agentId": "ceo", "family": "paperclip", "deviceId": "mac-1", "generation": "m1"}
    assert clean["dropped"] == [MAC] and state["mac"] is None


def test_mac_run_end_closes_its_tabs_before_dropping_the_pipe(monkeypatch, clean):
    order = []
    monkeypatch.setattr(browser_control.cdp_mux, "close_run", lambda run, upstream=None: order.append(("close", run, upstream)))
    monkeypatch.setattr(browser_control.cdp_mux, "drop", lambda name: order.append(("drop", name)))
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()]))
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    order.clear()
    browser_control.release(state)
    assert order.index(("close", "run-1", MAC)) < order.index(("drop", MAC))


def test_select_cloud_releases_mac(monkeypatch, clean):
    posted = []
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()], posted))
    monkeypatch.setattr(browser_control, "_post", _acquire())
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    browser_control.select_cloud()
    assert posted[-1][0] == "mac/release" and state["mac"] is None and clean["dropped"] == [MAC]
    assert browser_control.cdp_url() == "ws://127.0.0.1:9222/devtools/browser/run-1"
    assert browser_control.pre_tool_call("browser_click", {})["action"] == "block"  # fresh snapshot on the cloud


def test_mac_run_is_never_cast_but_a_cloud_run_still_is(monkeypatch, clean):
    """The desktop app casts a Mac's own live view itself (Task 9); the bridge only ever casts
    CLOUD runs, even while frames are wanted."""
    monkeypatch.setattr(browser_control.time, "time", lambda: 1000.0)
    monkeypatch.setattr(browser_control, "_post", _acquire())
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()]))
    cloud_sink, mac_sink = object(), object()
    browser_control.begin_turn("run-1", "ceo", cloud_sink)
    browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1")
    browser_control.begin_turn("run-2", "growth", mac_sink)
    browser_control.select_mac()
    browser_control.pre_tool_call("browser_snapshot", {}, task_id="t2")
    assert browser_control.frames_wanted(45) == 1
    assert clean["casts"] == [("run-1", cloud_sink, 45, True)]


def test_computer_use_blocked_on_mac(monkeypatch, clean):
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()]))
    browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    assert browser_control.pre_tool_call("computer_use", {}) == {"action": "block", "message": browser_control.COMPUTER_USE_MAC}


def test_mac_never_yields_to_cloud_human_input(monkeypatch, clean):
    monkeypatch.setattr(browser_control.time, "time", lambda: 1000.0)
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready(humanInputAt=999.5)]))
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    assert state["humanInputAt"] == 0.0  # the backend's (cloud) input time never applies to a Mac
    assert browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1") is None


def test_mac_agent_yields_after_local_human_input(monkeypatch, clean):
    clock = _clock(monkeypatch)
    slept = []
    monkeypatch.setattr(browser_control.time, "sleep", lambda s: slept.append(s) or clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()]))
    browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    assert browser_control.pre_tool_call("browser_snapshot", {}, tool_call_id="s1") is None
    browser_control.post_tool_call("browser_snapshot", status="ok", tool_call_id="s1")
    clock[0] += 10
    clean["human"][MAC] = clock[0] - 1  # the person clicked in the agent window a second ago
    result = browser_control.pre_tool_call("browser_click", {})
    assert slept == [2.0] and "fresh browser_snapshot" in result["message"]


def test_abort_saves_within_ten_seconds_then_gives_up(monkeypatch, clean):
    import threading
    import time as real_time

    assert browser_control.MAC_STOP_SAVE_S == 10
    monkeypatch.setattr(browser_control, "MAC_STOP_SAVE_S", 0.3)
    stuck, started = threading.Event(), threading.Event()
    monkeypatch.setattr(browser_control.browser_state, "checkpoint", lambda params, **_: started.set() or stuck.wait(5))
    sync = {"syncId": "S1", "steps": [{"action": "save"}]}
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()], None, **{"mac/release": {"sync": sync}}))
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1")
    t0 = real_time.monotonic()
    browser_control.abort("run-1")
    browser_control.release(state)  # the turn's own teardown waits for Stop's save, then goes on
    assert started.is_set() and real_time.monotonic() - t0 < 2
    assert clean["dropped"] == [MAC] and state["mac"] is None
    stuck.set()


def test_access_ended_close_gives_specific_message_and_deselects_mac(monkeypatch, clean):
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()]))
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    clean["close"][MAC] = (4502, "BROWSER_OFF")
    assert browser_control.pre_tool_call("browser_snapshot", {}) == {"action": "block", "message": browser_control.ACCESS_ENDED}
    assert state["mac"] is None and clean["dropped"] == [MAC]


def test_capacity_close_gives_busy_message(monkeypatch, clean):
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()]))
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    clean["close"][MAC] = (4503, "")
    assert browser_control.pre_tool_call("browser_snapshot", {}) == {"action": "block", "message": browser_control.MAC_BUSY}
    assert state["mac"] is not None  # still selected: the next call tries again
    assert browser_control.pre_tool_call("browser_snapshot", {}) is None


def test_show_sends_on_the_runs_mac_pipe_only(monkeypatch, clean):
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()]))
    browser_control.begin_turn("run-1", "ceo")
    assert browser_control.show() is False  # no Mac selected
    browser_control.select_mac()
    assert browser_control.show() is True and clean["shown"] == [("run-1", MAC)]


# ---- fix round 1 ------------------------------------------------------------------------------


def test_a_finished_sync_never_blocks_the_next_claimed_sync(monkeypatch, clean):
    ran = _checkpoints(monkeypatch)
    first = {"syncId": "L1", "steps": [{"action": "load"}]}
    second = {"syncId": "L2", "steps": [{"action": "save"}, {"action": "load"}]}
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready(sync=first), _ready(sync=second)]))
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    state["macSync"].join(5)  # done, but nobody cleared it (select_mac/renew never wait for it)
    browser_control.renew()
    _join_sync(state)
    assert [p["action"] for p in ran] == ["load", "save", "load"]


def test_select_mac_fits_under_api_gateway_and_sends_its_budget(monkeypatch, clean):
    """API Gateway cuts the bridge's acquire at 30 s: the backend must answer (202 while a cold
    Chrome launches) inside the budget the bridge sends, or its claim is orphaned."""
    calls = []
    post = _mac_acquire([_ready()])
    monkeypatch.setattr(browser_control, "_post_mac",
                        lambda path, body, timeout=20.0: calls.append((timeout, body.get("budgetS"))) or post(path, body))
    browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    timeout, budget = calls[0]
    assert browser_control.MAC_SELECT_BUDGET_S <= 28 and 27 < timeout <= 28
    assert budget == browser_control.MAC_ACQUIRE_MAX_S == 25 and budget <= timeout - 2
    calls.clear()
    browser_control.pre_tool_call("browser_snapshot", {})  # a tool call's acquire: inside the hook budget
    timeout, budget = calls[0]
    assert timeout <= browser_control.HOOK_BUDGET_S and budget <= browser_control.HOOK_BUDGET_S - 2


def test_select_mac_polls_202_until_the_launch_is_done(monkeypatch, clean):
    clock = _clock(monkeypatch)
    answers = [(202, {"status": "starting"}), (202, {"status": "starting"}), _ready()]
    post = _mac_acquire(answers)

    def slow(path, body, timeout=20.0):
        clock[0] += 3.0
        return post(path, body)

    monkeypatch.setattr(browser_control, "_post_mac", slow)
    state = browser_control.begin_turn("run-1", "ceo")
    assert browser_control.select_mac() is None and state["mac"]["generation"] == "m1"
    assert clock[0] - 1000.0 < 28


def test_a_running_sync_leaves_the_acquire_its_slice(monkeypatch, clean):
    """A sync that ends late must not eat the acquire's time: the call says "loading", never
    "unavailable" for an acquire that would have succeeded."""
    clock = _clock(monkeypatch)
    timeouts = []
    post = _mac_acquire([_ready()])

    def acquire(path, body, timeout=20.0):
        timeouts.append(timeout)
        if timeout < 6:  # the backend answers in 6 s; a shorter wait sees nothing
            clock[0] += timeout
            return 0, {}
        clock[0] += 6
        return post(path, body)

    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()]))
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    monkeypatch.setattr(browser_control, "_post_mac", acquire)

    class Sync:
        def __init__(self, done_at):
            self.done_at = done_at

        def join(self, t):
            clock[0] = min(clock[0] + t, max(clock[0], self.done_at))

        def is_alive(self):
            return clock[0] < self.done_at

    state["macSync"] = Sync(clock[0] + 15)  # ends 15 s into a 20 s hook
    assert browser_control.pre_tool_call("browser_navigate", {}) == {"action": "block", "message": browser_control.MAC_SYNCING}
    assert timeouts == []
    state["macSync"] = Sync(clock[0] + 11)
    assert browser_control.pre_tool_call("browser_navigate", {}) is None
    assert timeouts[0] >= browser_control.MAC_ACQUIRE_MIN_S


def test_sync_steps_share_one_budget_that_ends_before_the_claim(monkeypatch, clean):
    clock = _clock(monkeypatch)
    budgets = []

    def checkpoint(params, budget_s=None):
        budgets.append(budget_s)
        clock[0] += 60

    monkeypatch.setattr(browser_control.browser_state, "checkpoint", checkpoint)
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()]))
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    start = clock[0]
    sync = {"syncId": "L1", "steps": [{"action": "save"}, {"action": "load"}, {"action": "load"}]}
    assert browser_control._run_mac_sync(state, sync) is False  # the third step has no time left
    assert budgets == [browser_control.MAC_SYNC_BUDGET_S, browser_control.MAC_SYNC_BUDGET_S - 60]
    # The steps end by the budget; the synced report's own wait still lands inside the claim.
    assert clock[0] - start <= browser_control.MAC_SYNC_BUDGET_S + 20 < 180  # mac_browser.CLAIM_SECONDS


def test_release_runs_its_save_on_the_fresh_ticket(monkeypatch, clean):
    fresh = "ws://10.0.1.7:8000/api/v1/internal/mac-cdp/fresh"
    order = []
    monkeypatch.setattr(browser_control.browser_state, "checkpoint",
                        lambda params, **_: order.append(("save", clean["upstream"][-1])))
    sync = {"syncId": "S1", "steps": [{"action": "save"}]}
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire(
        [_ready()], None, **{"mac/release": {"sync": sync, "cdpUrl": fresh, "generation": "m1"}}))
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1")
    browser_control.release(state)
    assert order == [("save", (fresh, "m1", MAC))]


def test_after_access_ended_the_run_never_falls_back_to_the_cloud(monkeypatch, clean):
    cloud = []
    monkeypatch.setattr(browser_control, "_post", lambda *a, **k: cloud.append(a) or {"generation": "g1", "cdpUrl": URL})
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()]))
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    clean["close"][MAC] = (4403, "")
    assert browser_control.pre_tool_call("browser_snapshot", {})["message"] == browser_control.ACCESS_ENDED
    assert browser_control.pre_tool_call("browser_snapshot", {}) == {"action": "block", "message": browser_control.MAC_DESELECTED}
    assert browser_control.cdp_url() == "" and cloud == []
    browser_control.select_cloud()  # the agent chose the cloud
    assert browser_control.pre_tool_call("browser_snapshot", {}) is None and len(cloud) == 1
    state["macEnded"] = True
    assert browser_control.select_mac() is None  # or chose the Mac again
    assert browser_control.pre_tool_call("browser_snapshot", {}) is None


def test_stop_closes_the_macs_tabs_only_after_its_save(monkeypatch, clean):
    import threading

    order, saved = [], threading.Event()
    monkeypatch.setattr(browser_control.cdp_mux, "close_run", lambda run, upstream=None: order.append(("close", run, upstream)))
    monkeypatch.setattr(browser_control.cdp_mux, "drop", lambda name: order.append(("drop", name)))
    monkeypatch.setattr(browser_control.browser_state, "checkpoint", lambda params, **_: order.append(("save",)) or saved.set())
    sync = {"syncId": "S1", "steps": [{"action": "save"}]}
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()], None, **{"mac/release": {"sync": sync}}))
    state = browser_control.begin_turn("run-1", "ceo")
    browser_control.select_mac()
    browser_control.pre_tool_call("browser_snapshot", {}, task_id="t1")
    order.clear()
    browser_control.abort("run-1")
    browser_control.release(state)
    assert ("close", "run-1", None) not in order[:order.index(("save",))]  # no all-upstream close first
    assert order.index(("save",)) < order.index(("close", "run-1", MAC)) < order.index(("drop", MAC))


def test_stop_during_a_refresh_never_raises(monkeypatch, clean):
    """Stop deselects the Mac from another thread while this turn's acquire is in flight."""
    state = browser_control.begin_turn("run-1", "ceo")
    monkeypatch.setattr(browser_control, "_post_mac", _mac_acquire([_ready()]))
    browser_control.select_mac()
    ready = _mac_acquire([_ready()])

    def racing(path, body, timeout=20.0):
        if path == "mac/acquire":
            state["mac"] = None  # what Stop's _forget_mac does meanwhile
        return ready(path, body)

    monkeypatch.setattr(browser_control, "_post_mac", racing)
    assert browser_control.pre_tool_call("browser_snapshot", {})["action"] == "block"
    assert browser_control._upstream_of(state) == browser_control.cdp_mux.CLOUD
    assert browser_control.show() is False
    assert browser_control._run_mac_sync(state, {"syncId": "x", "steps": [{"action": "save"}]}) is False
