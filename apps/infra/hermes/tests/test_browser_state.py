import json
import re
import threading
import time
from pathlib import Path

import pytest

from bridge import browser_state


def _params(**extra):
    return {
        "action": "save",
        "cdpUrl": "wss://bedrock-agentcore.us-east-1.amazonaws.com/browser-streams/aws.browser.v1/sessions/abc/automation",
        "stateUrl": "https://bucket.s3.us-east-1.amazonaws.com/browser-state/owner/latest.json?X-Amz-Signature=x",
        **extra,
    }


def test_save_uploads_current_state_and_preserves_other_origins(monkeypatch):
    calls = []
    current = {"cookies": [{"name": "new"}], "origins": [{"origin": "https://new.example", "localStorage": []}]}
    previous = {"cookies": [{"name": "old"}], "origins": [
        {"origin": "https://old.example", "localStorage": [{"name": "k", "value": "v"}]},
        {"origin": "https://new.example", "localStorage": [{"name": "stale", "value": "1"}]},
    ]}

    def run(argv, **kwargs):
        calls.append(argv)
        if "save" in argv:
            Path(argv[-1]).write_text(json.dumps(current))
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_args): return json.dumps(previous).encode()

    def urlopen(request, timeout):
        if request.get_method() == "GET": return Response()
        assert request.get_header("Content-type") == "application/json"
        body = json.loads(request.data)
        assert body["cookies"] == current["cookies"]
        assert {o["origin"] for o in body["origins"]} == {"https://old.example", "https://new.example"}
        assert next(o for o in body["origins"] if o["origin"] == "https://new.example")["localStorage"] == []
        return Response()

    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state, "_OPEN", type("Open", (), {"open": staticmethod(urlopen)})())
    result = browser_state.checkpoint(_params(previousStateUrl=_params()["stateUrl"]))

    assert result == {"ok": True, "bytes": result["bytes"]}
    assert any("connect" in call for call in calls)
    assert any("close" in call for call in calls)


def test_rejects_non_aws_urls_without_running_cli(monkeypatch):
    called = False

    def run(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(browser_state.subprocess, "run", run)
    with pytest.raises(ValueError):
        browser_state.checkpoint(_params(cdpUrl="wss://evil.example/automation"))
    assert called is False


def test_invalid_action_does_not_attach_or_close(monkeypatch):
    called = False

    def run(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(browser_state.subprocess, "run", run)
    with pytest.raises(ValueError):
        browser_state.checkpoint(_params(action="bogus"))
    assert called is False


def test_cdp_path_and_s3_redirects_are_rejected(monkeypatch):
    with pytest.raises(ValueError):
        browser_state.checkpoint(_params(cdpUrl="wss://bedrock-agentcore.us-east-1.amazonaws.com/not-cdp"))
    assert browser_state._valid_url("https://bucket.s3.us-east-1.amazonaws.com/state", "https")
    with pytest.raises(ValueError):
        browser_state._valid_url("https://bucket.s3.us-east-1.amazonaws.com:444/state", "https")


def test_the_vpc_custom_browser_cdp_url_is_accepted():
    """Codex on #4391: the env's VPC-mode custom browser has its own id in the stream path; a
    bridge that took only aws.browser.v1 refused every checkpoint on it."""
    base = "wss://bedrock-agentcore.us-east-1.amazonaws.com/browser-streams/{}/sessions/abc/automation"
    for browser in ("aws.browser.v1", "lightbulb_dev_workspace_browser-Ab12Cd34Ef"):
        assert browser_state._valid_url(base.format(browser), "wss")
    for bad in ("../x", "a/b", ""):
        with pytest.raises(ValueError):
            browser_state._valid_url(base.format(bad), "wss")


def test_load_runs_state_load_then_reload_and_returns_no_state(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_args): return b'{"cookies":[],"origins":[{"origin":"https://example.com","localStorage":[]}]}'

    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state, "_OPEN", type("Open", (), {"open": staticmethod(lambda *_args, **_kwargs: Response())})())
    assert browser_state.checkpoint(_params(action="load")) == {"ok": True, "bytes": 0}
    actions = [c[3:] for c in calls]
    assert actions[:7] == [
        ["connect", _params()["cdpUrl"]],
        ["cookies", "clear"],
        ["state", "load", calls[2][-1]],  # the cookies, before any site can be slow
        ["open", "https://example.com"],
        ["storage", "local", "clear"],
        ["state", "load", calls[5][-1]],
        ["reload"],
    ]


def test_load_restores_the_rest_when_one_saved_site_does_not_load(monkeypatch):
    """A sleeping site timed out Page.navigate and failed every restore: the browser never
    started again (dev 2026-09-28). It is now left out of this restore; the rest loads."""
    calls, loaded = [], []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[3:5] == ["open", "https://slow.example"]:
            return type("Completed", (), {"returncode": 1})()
        if argv[3:5] == ["state", "load"]:
            loaded.append(json.loads(Path(argv[-1]).read_text()))
        return type("Completed", (), {"returncode": 0})()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_args):
            return json.dumps({"cookies": [{"name": "sid", "value": "v", "domain": ".a.example", "path": "/"}],
                               "origins": [{"origin": "https://slow.example", "localStorage": [{"name": "k", "value": "1"}]},
                                           {"origin": "https://ok.example", "localStorage": [{"name": "k", "value": "2"}]}]}).encode()

    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state, "_OPEN", type("Open", (), {"open": staticmethod(lambda *_args, **_kwargs: Response())})())
    assert browser_state.checkpoint(_params(action="load")) == {"ok": True, "bytes": 0}
    assert loaded[0]["cookies"][0]["name"] == "sid" and loaded[0]["origins"] == []
    assert [o["origin"] for load in loaded[1:] for o in load["origins"]] == ["https://ok.example"]
    assert ["storage", "local", "clear"] in [c[3:] for c in calls]


def test_load_stops_trying_sites_before_the_budget_runs_out(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(browser_state.time, "monotonic", lambda: clock[0])
    opened, loaded = [], []

    def run(argv, **kwargs):
        if argv[3] == "open":
            opened.append(argv[4])
            clock[0] += 12  # every site hangs to its cap
            return type("Completed", (), {"returncode": 1})()
        if argv[3:5] == ["state", "load"]:
            loaded.append(json.loads(Path(argv[-1]).read_text()))
        return type("Completed", (), {"returncode": 0})()

    body = json.dumps({"cookies": [], "origins": [{"origin": f"https://s{i}.example", "localStorage": []} for i in range(20)]}).encode()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_args): return body

    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state, "_OPEN", type("Open", (), {"open": staticmethod(lambda *_args, **_kwargs: Response())})())
    assert browser_state.checkpoint(_params(action="load")) == {"ok": True, "bytes": 0}
    assert 0 < len(opened) < 20 and loaded[0]["origins"] == []


def test_load_rejects_non_origin_before_running_cli(monkeypatch):
    called = False

    def run(*_args, **_kwargs):
        nonlocal called
        called = True

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_args):
            return b'{"cookies":[],"origins":[{"origin":"https://example.com/path","localStorage":[]}]}'

    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state, "_OPEN", type("Open", (), {"open": staticmethod(lambda *_args, **_kwargs: Response())})())
    with pytest.raises(ValueError, match="origin"):
        browser_state.checkpoint(_params(action="load"))
    assert called is False


def test_all_work_shares_one_deadline_and_cleanup_is_capped(monkeypatch):
    timeouts = []
    network_timeouts = []

    def run(argv, **kwargs):
        timeouts.append((argv[-1], kwargs["timeout"]))
        if "save" in argv:
            Path(argv[-1]).write_text('{"cookies":[],"origins":[]}')
        return type("Completed", (), {"returncode": 0})()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass

    def urlopen(*_args, **kwargs):
        network_timeouts.append(kwargs["timeout"])
        return Response()

    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state.time, "monotonic", iter([1000, 1080, 1090, 1095, 1096, 1097]).__next__)
    monkeypatch.setattr(browser_state, "_OPEN", type("Open", (), {"open": staticmethod(urlopen)})())
    browser_state.checkpoint(_params())
    assert [timeout for _, timeout in timeouts[:-1]] == [20, 10]
    assert network_timeouts == [5]
    assert timeouts[-1][0] == "close"
    assert timeouts[-1][1] == 4


def test_subprocess_failure_cleans_up_and_does_not_claim_success(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return type("Completed", (), {"returncode": 1, "stdout": "", "stderr": "secret"})()

    monkeypatch.setattr(browser_state.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="browser state"):
        browser_state.checkpoint(_params())
    assert any("close" in c for c in calls)


def test_helpers_use_private_writable_sockets_with_root_home(monkeypatch):
    import stat
    sockets = []
    monkeypatch.setenv('HOME', '/')
    monkeypatch.setenv('OPENCLAW_HOOK_TOKEN', 'must-not-inherit')

    def run(argv, **kwargs):
        env = kwargs['env']
        socket_dir = Path(env['AGENT_BROWSER_SOCKET_DIR'])
        assert socket_dir.is_dir()
        assert stat.S_IMODE(socket_dir.stat().st_mode) == 0o700
        assert 'OPENCLAW_HOOK_TOKEN' not in env
        sockets.append(socket_dir)
        return type('Completed', (), {'returncode': 0})()

    monkeypatch.setattr(browser_state.subprocess, 'run', run)
    monkeypatch.setattr(browser_state, '_download', lambda *_args: b'{"cookies":[],"origins":[]}')
    browser_state.checkpoint(_params(action='load'))
    assert sockets and len(set(sockets)) == 1
    assert not sockets[0].exists()


def _cookie(domain, value):
    return {"name": "sid", "value": value, "domain": domain, "path": "/", "expires": -1,
            "httpOnly": True, "secure": True, "sameSite": "Lax"}


def test_save_with_a_base_keeps_what_a_mac_saved_meanwhile(monkeypatch):
    base = {"cookies": [_cookie(".a.test", "a0"), _cookie(".gone.test", "g0")], "origins": [
        {"origin": "https://a.test", "localStorage": [{"name": "k", "value": "a0"}]}]}
    # The live session: a.test renewed, gone.test signed out, nothing about mac.test.
    current = {"cookies": [_cookie(".a.test", "a1")], "origins": [
        {"origin": "https://a.test", "localStorage": [{"name": "k", "value": "a0"}]}]}
    # The workspace save now: a Mac added mac.test and changed a.test's storage.
    previous = {"cookies": [_cookie(".a.test", "a0"), _cookie(".gone.test", "g0"), _cookie(".mac.test", "m1")],
                "origins": [{"origin": "https://a.test", "localStorage": [{"name": "k", "value": "mac"}]}]}
    urls = {"base": base, "prev": previous}
    uploads = {}

    def run(argv, **kwargs):
        if "save" in argv:
            Path(argv[-1]).write_text(json.dumps(current))
        return type("Completed", (), {"returncode": 0})()

    class Response:
        def __init__(self, body=b""): self.body = body
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_args): return self.body

    def urlopen(request, timeout):
        name = request.full_url.split("/")[-1].split("?")[0]
        if request.get_method() == "GET":
            return Response(json.dumps(urls[name]).encode())
        uploads[name] = json.loads(request.data)
        return Response()

    s3 = "https://bucket.s3.us-east-1.amazonaws.com/browser-state/owner/"
    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state, "_OPEN", type("Open", (), {"open": staticmethod(urlopen)})())
    result = browser_state.checkpoint(_params(
        stateUrl=s3 + "new", previousStateUrl=s3 + "prev", baseStateUrl=s3 + "base", baseOutUrl=s3 + "nextbase"))
    assert result["base"] is True
    # The next base is this export, plus the origins it cannot see carried from the old base.
    assert uploads["nextbase"]["cookies"] == current["cookies"]
    assert uploads["nextbase"]["origins"] == current["origins"]
    saved = uploads["new"]
    assert {c["domain"]: c["value"] for c in saved["cookies"]} == {".a.test": "a1", ".mac.test": "m1"}
    assert saved["origins"] == previous["origins"]  # the session did not change a.test's storage


def test_save_with_an_unreadable_base_fails_rather_than_guessing(monkeypatch):
    current = {"cookies": [], "origins": []}
    previous = {"cookies": [_cookie(".mac.test", "m1")], "origins": []}
    uploads = {}

    def run(argv, **kwargs):
        if "save" in argv:
            Path(argv[-1]).write_text(json.dumps(current))
        return type("Completed", (), {"returncode": 0})()

    class Response:
        def __init__(self, body=b""): self.body = body
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_args): return self.body

    def urlopen(request, timeout):
        name = request.full_url.split("/")[-1].split("?")[0]
        if request.get_method() == "GET":
            if name == "base":
                raise OSError("404")
            return Response(json.dumps(previous).encode())
        uploads[name] = json.loads(request.data)
        return Response()

    s3 = "https://bucket.s3.us-east-1.amazonaws.com/browser-state/owner/"
    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state, "_OPEN", type("Open", (), {"open": staticmethod(urlopen)})())
    with pytest.raises(RuntimeError, match="browser state"):
        browser_state.checkpoint(_params(
            stateUrl=s3 + "new", previousStateUrl=s3 + "prev", baseStateUrl=s3 + "base", baseOutUrl=s3 + "nextbase"))
    assert uploads == {}


def test_load_records_the_browsers_own_post_load_export_as_the_base(monkeypatch):
    saved = {"cookies": [_cookie(".a.test", "a0")], "origins": [
        {"origin": "https://a.test", "localStorage": [{"name": "k", "value": "v"}]},
        {"origin": "https://b.test", "localStorage": [{"name": "k", "value": "w"}]}]}
    # Chrome capped the expiry and reset sameSite on load; only a.test is visible.
    exported = {"cookies": [{**_cookie(".a.test", "a0"), "expires": 1_800_000_000, "sameSite": "None"}],
                "origins": [{"origin": "https://a.test", "localStorage": [{"name": "k", "value": "v"}]}]}
    uploads = {}

    def run(argv, **kwargs):
        if "save" in argv:
            Path(argv[-1]).write_text(json.dumps(exported))
        return type("Completed", (), {"returncode": 0})()

    class Response:
        def __init__(self, body=b""): self.body = body
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_args): return self.body

    def urlopen(request, timeout):
        name = request.full_url.split("/")[-1].split("?")[0]
        if request.get_method() == "GET":
            return Response(json.dumps(saved).encode())
        uploads[name] = json.loads(request.data)
        return Response()

    s3 = "https://bucket.s3.us-east-1.amazonaws.com/browser-state/owner/"
    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state, "_OPEN", type("Open", (), {"open": staticmethod(urlopen)})())
    result = browser_state.checkpoint(_params(action="load", stateUrl=s3 + "saved", baseOutUrl=s3 + "base"))
    assert result == {"ok": True, "bytes": 0, "base": True}
    base = uploads["base"]
    assert base["cookies"] == exported["cookies"]  # normalisation baked into the base
    assert {o["origin"] for o in base["origins"]} == {"https://a.test", "https://b.test"}
    # A save right after, with nothing changed, therefore writes nothing of its own:
    merged = browser_state._merge_three(base, exported, {"cookies": [_cookie(".a.test", "mac")], "origins": []})
    assert merged["cookies"] == [_cookie(".a.test", "mac")]
    # ...while a real renewal still wins.
    renewed = {**exported, "cookies": [{**exported["cookies"][0], "expires": 1_900_000_000}]}
    assert browser_state._merge_three(base, renewed, {"cookies": [], "origins": []})["cookies"] == renewed["cookies"]


def test_an_origin_the_restore_skipped_is_never_written_by_the_next_save():
    base = {"cookies": [], "origins": [], "skippedOrigins": ["https://slow.test"]}
    current = {"cookies": [], "origins": [{"origin": "https://slow.test", "localStorage": [{"name": "k", "value": "stale"}]}]}
    previous = {"cookies": [], "origins": [{"origin": "https://slow.test", "localStorage": [{"name": "k", "value": "ws"}]}]}
    assert browser_state._merge_three(base, current, previous)["origins"] == previous["origins"]


def test_a_failed_state_upload_writes_no_base(monkeypatch):
    uploads = []

    def run(argv, **kwargs):
        if "save" in argv:
            Path(argv[-1]).write_text('{"cookies":[],"origins":[]}')
        return type("Completed", (), {"returncode": 0})()

    def upload(url, body, deadline):
        uploads.append(url.split("/")[-1])
        raise OSError("PUT failed")

    s3 = "https://bucket.s3.us-east-1.amazonaws.com/browser-state/owner/"
    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state, "_upload", upload)
    with pytest.raises(RuntimeError):
        browser_state.checkpoint(_params(stateUrl=s3 + "new", baseOutUrl=s3 + "nextbase"))
    assert uploads == ["new"]  # the base is written only after the state it belongs to


def test_the_next_base_carries_origins_this_client_cannot_see(monkeypatch):
    base = {"cookies": [], "origins": [{"origin": "https://unseen.test", "localStorage": [{"name": "k", "value": "b"}]}]}
    current = {"cookies": [], "origins": [{"origin": "https://seen.test", "localStorage": []}]}
    uploads = {}

    def run(argv, **kwargs):
        if "save" in argv:
            Path(argv[-1]).write_text(json.dumps(current))
        return type("Completed", (), {"returncode": 0})()

    s3 = "https://bucket.s3.us-east-1.amazonaws.com/browser-state/owner/"
    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state, "_download", lambda url, deadline: json.dumps(base).encode())
    monkeypatch.setattr(browser_state, "_upload", lambda url, body, deadline: uploads.__setitem__(url.split("/")[-1], json.loads(body)))
    browser_state.checkpoint(_params(stateUrl=s3 + "new", baseStateUrl=s3 + "base", baseOutUrl=s3 + "nextbase"))
    assert {o["origin"] for o in uploads["nextbase"]["origins"]} == {"https://seen.test", "https://unseen.test"}


def test_a_session_with_nothing_to_restore_still_records_its_own_base(monkeypatch):
    exported = {"cookies": [_cookie(".profile.test", "p")], "origins": []}
    uploads = {}

    def run(argv, **kwargs):
        assert "clear" not in argv  # recording a base never changes the session
        if "save" in argv:
            Path(argv[-1]).write_text(json.dumps(exported))
        return type("Completed", (), {"returncode": 0})()

    s3 = "https://bucket.s3.us-east-1.amazonaws.com/browser-state/owner/"
    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state, "_upload", lambda url, body, deadline: uploads.__setitem__(url.split("/")[-1], json.loads(body)))
    assert browser_state.checkpoint(_params(action="base", stateUrl=s3 + "g.base.json"))["base"] is True
    assert uploads["g.base.json"] == exported | {"visibleOrigins": []}


def test_the_cloud_digest_sees_a_partition_key_change_but_not_its_own_normalisation():
    exported = {**_cookie(".a.test", "a0"), "expires": 1_800_000_000, "sameSite": "None"}
    base = {"cookies": [exported], "origins": []}
    previous = {"cookies": [_cookie(".a.test", "workspace")], "origins": []}
    # Nothing changed since the post-load export: the workspace's copy stands.
    same = browser_state._merge_three(base, {"cookies": [dict(exported)], "origins": []}, previous)
    assert same["cookies"] == previous["cookies"]
    # Only partitionKey changed: that is a change the session made.
    partitioned = {**exported, "partitionKey": "https://top.test"}
    changed = browser_state._merge_three(base, {"cookies": [partitioned], "origins": []}, previous)
    assert changed["cookies"] == [partitioned]


def test_a_visible_origin_cleared_to_empty_is_deleted_from_the_workspace():
    token = {"origin": "https://app.test", "localStorage": [{"name": "token", "value": "t"}]}
    base = {"cookies": [], "origins": [token], "visibleOrigins": ["https://app.test"]}
    current = {"cookies": [], "origins": []}  # the page cleared its last key: the export omits it
    previous = {"cookies": [], "origins": [token]}
    merged = browser_state._merge_three(base, current, previous, open_origins={"https://app.test"})
    assert merged["origins"] == [{"origin": "https://app.test", "localStorage": []}]


def test_an_invisible_skipped_or_closed_origin_missing_from_the_export_is_kept():
    kept = {"origin": "https://unseen.test", "localStorage": [{"name": "k", "value": "v"}]}
    closed = {"origin": "https://closed.test", "localStorage": [{"name": "k", "value": "v"}]}
    slow = {"origin": "https://slow.test", "localStorage": [{"name": "k", "value": "v"}]}
    base = {"cookies": [], "origins": [kept, closed, slow], "visibleOrigins": ["https://closed.test"],
            "skippedOrigins": ["https://slow.test"]}
    previous = {"cookies": [], "origins": [kept, closed, slow]}
    # unseen.test was carried (invisible), slow.test was skipped, closed.test's tab is not open.
    merged = browser_state._merge_three(base, {"cookies": [], "origins": []}, previous, open_origins={"https://slow.test"})
    assert merged["origins"] == previous["origins"]


def test_open_origins_parse_tab_urls_and_never_fail(monkeypatch):
    out = json.dumps({"success": True, "data": {"tabs": [{"id": "t1", "url": "https://app.test/x?y"},
                                                         {"url": "about:blank"}]}}).encode()
    monkeypatch.setattr(browser_state.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": out})())
    assert browser_state._open_origins({}, time.monotonic() + 30) == {"https://app.test"}
    monkeypatch.setattr(browser_state.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": b""})())
    assert browser_state._open_origins({}, time.monotonic() + 30) == set()


def test_an_origin_cleared_then_recreated_with_the_old_values_is_a_change(monkeypatch):
    token = {"origin": "https://app.test", "localStorage": [{"name": "token", "value": "t"}]}
    states = iter([
        {"cookies": [], "origins": []},       # save 1: the page cleared its last key
        {"cookies": [], "origins": [token]},  # save 2: the same token comes back
    ])
    store = {
        "base": {"cookies": [], "origins": [token], "visibleOrigins": ["https://app.test"]},
        "prev": {"cookies": [], "origins": [token]},
    }

    def run(argv, **kwargs):
        if "save" in argv:
            Path(argv[-1]).write_text(json.dumps(next(states)))
        return type("Completed", (), {"returncode": 0})()

    def upload(url, body, deadline):
        name = url.split("/")[-1]
        store["base" if name == "nextbase" else "prev"] = json.loads(body)

    s3 = "https://bucket.s3.us-east-1.amazonaws.com/browser-state/owner/"
    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state, "_open_origins", lambda env, deadline: {"https://app.test"})
    monkeypatch.setattr(browser_state, "_download", lambda url, deadline: json.dumps(store[url.split("/")[-1]]).encode())
    monkeypatch.setattr(browser_state, "_upload", upload)
    params = _params(stateUrl=s3 + "new", previousStateUrl=s3 + "prev", baseStateUrl=s3 + "base", baseOutUrl=s3 + "nextbase")
    browser_state.checkpoint(params)
    assert store["prev"]["origins"] == [{"origin": "https://app.test", "localStorage": []}]
    assert {"origin": "https://app.test", "localStorage": []} in store["base"]["origins"]
    browser_state.checkpoint(params)
    assert store["prev"]["origins"] == [token]  # detected, not mistaken for "unchanged"


def test_slow_origins_cannot_run_the_restore_out_of_time(monkeypatch):
    """Prod 2026-09-28: 8 heavy origins ate the fixed 100 s and the restore failed after 105 s,
    sign-ins and all. Every site now hangs to its step cap: the restore still finishes inside
    the budget, the cookies still load, and the skipped sites are recorded as skipped."""
    clock = [0.0]
    monkeypatch.setattr(browser_state.time, "monotonic", lambda: clock[0])
    loaded, uploads = [], {}
    saved = {"cookies": [_cookie(".google.com", "sid")],
             "origins": [{"origin": f"https://s{i}.example", "localStorage": [{"name": "k", "value": "v"}]}
                         for i in range(8)]}

    def run(argv, **kwargs):
        assert kwargs["timeout"] > 0
        if argv[3] in ("open", "reload"):
            clock[0] += kwargs["timeout"]  # hangs until killed at its own cap
            raise browser_state.subprocess.TimeoutExpired(argv, kwargs["timeout"])
        clock[0] += 1
        if argv[3:5] == ["state", "load"]:
            loaded.append(json.loads(Path(argv[-1]).read_text()))
        if argv[3:5] == ["state", "save"]:
            Path(argv[-1]).write_text(json.dumps({"cookies": saved["cookies"], "origins": []}))
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state, "_download", lambda url, deadline: json.dumps(saved).encode())
    monkeypatch.setattr(browser_state, "_upload", lambda url, body, deadline: uploads.__setitem__(url.split("/")[-1], json.loads(body)))
    s3 = "https://bucket.s3.us-east-1.amazonaws.com/browser-state/owner/"
    result = browser_state.checkpoint(_params(action="load", stateUrl=s3 + "saved", baseOutUrl=s3 + "base"))
    assert result == {"ok": True, "bytes": 0, "base": True}
    assert clock[0] < browser_state.OPERATION_TIMEOUT_S
    assert loaded[0]["cookies"] == saved["cookies"]
    assert set(uploads["base"]["skippedOrigins"]) == {o["origin"] for o in saved["origins"]}


# ---- a paired Mac's pipe: the same checkpoint over the mux, vetted, cookies-only load ----------

MUX = "ws://127.0.0.1:9222/devtools/browser/" + "0123456789abcdef01234567"
S3 = "https://bucket.s3.us-east-1.amazonaws.com/browser-state/owner/"


def test_mux_url_accepted():
    assert browser_state._valid_cdp_url(MUX, "mac") == MUX
    assert browser_state._valid_cdp_url(_params()["cdpUrl"]) == _params()["cdpUrl"]


def test_url_kind_follows_the_source():
    with pytest.raises(ValueError):
        browser_state._valid_cdp_url(MUX)  # the cloud needs its AgentCore URL
    with pytest.raises(ValueError):
        browser_state._valid_cdp_url(_params()["cdpUrl"], "mac")  # a Mac needs the run's loopback pipe
    with pytest.raises(ValueError):
        browser_state.checkpoint({**_params(), "source": "mac"})


@pytest.mark.parametrize("url", [
    "ws://10.0.1.7:8000/api/v1/internal/mac-cdp/ticket",           # the door itself
    "wss://api.thelightbulb.company/api/v1/internal/mac-cdp/ticket",
    "ws://127.0.0.1:9223/devtools/browser/0123456789abcdef01234567",
    "ws://127.0.0.1:9222/devtools/browser/0123456789abcdef01234567?x=1",
    "ws://localhost:9222/devtools/browser/0123456789abcdef01234567",
    "wss://evil.example/browser-streams/aws.browser.v1/sessions/abc/automation",
])
def test_private_or_public_ws_url_refused(url):
    for source in ("cloud", "mac"):
        with pytest.raises(ValueError):
            browser_state._valid_cdp_url(url, source)


def _mac(monkeypatch, exported, downloads):
    calls, uploads = [], {}

    def run(argv, **kwargs):
        calls.append(argv[3:])
        if argv[3:5] == ["state", "save"]:
            Path(argv[-1]).write_text(json.dumps(exported))
        return type("Completed", (), {"returncode": 0, "stdout": b"[]"})()

    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(browser_state, "_download", lambda url, deadline: json.dumps(downloads[url.split("/")[-1]]).encode())
    monkeypatch.setattr(browser_state, "_upload", lambda url, body, deadline: uploads.__setitem__(url.split("/")[-1], json.loads(body)))
    return calls, uploads


def test_mac_load_opens_no_visible_page_and_loads_localstorage_in_the_background(monkeypatch):
    saved = {"cookies": [_cookie(".a.test", "a0")], "origins": [
        {"origin": "https://a.test", "localStorage": [{"name": "k", "value": "v"}]},
        {"origin": "https://bad.test", "localStorage": [{"name": 1, "value": "x"}]}]}  # vetted out
    calls, _ = _mac(monkeypatch, {"cookies": [_cookie(".a.test", "a0")], "origins": []}, {"saved": saved})
    loaded = []
    monkeypatch.setattr(browser_state, "_mac_origins", lambda url, origins, deadline: loaded.append((url, origins)) or set())
    browser_state.checkpoint({"action": "load", "source": "mac", "cdpUrl": MUX,
                              "stateUrl": S3 + "saved", "baseOutUrl": S3 + "base"})
    assert not any(c[:1] in (["open"], ["reload"], ["storage"]) for c in calls)  # agent-browser never navigates
    assert ["cookies", "clear"] in calls and ["connect", MUX] in calls
    assert loaded == [(MUX, [saved["origins"][0]])]


def test_mac_load_base_carries_loaded_origins_and_skips_the_rest(monkeypatch):
    saved = {"cookies": [], "origins": [{"origin": "https://a.test", "localStorage": [{"name": "k", "value": "v"}]},
                                        {"origin": "https://slow.test", "localStorage": [{"name": "k", "value": "s"}]}]}
    _, uploads = _mac(monkeypatch, {"cookies": [], "origins": []}, {"saved": saved})
    monkeypatch.setattr(browser_state, "_mac_origins", lambda url, origins, deadline: {"https://a.test"})
    browser_state.checkpoint({"action": "load", "source": "mac", "cdpUrl": MUX,
                              "stateUrl": S3 + "saved", "baseOutUrl": S3 + "base"})
    base = uploads["base"]
    assert base["origins"] == saved["origins"][:1] and base["skippedOrigins"] == ["https://slow.test"]
    # A save that does not see a.test (its tab is closed) never mistakes it for cleared.
    merged = browser_state._merge_three(base, {"cookies": [], "origins": []}, saved, {"https://a.test"})
    assert merged["origins"] == saved["origins"]


class FakeMacChrome:
    """The Mac's Chrome over the pipe: a background target, Fetch pausing its navigations (never
    a network request: a paused request is answered by the client or never completes), and
    localStorage per origin. It plays the storage script's contract: nothing changes unless the
    page is the expected origin; otherwise the page's storage becomes the items, as a diff."""

    def __init__(self, storage=None, lagging=(), broken=(), refuse_hidden=False):
        from websockets.sync.server import serve

        self.refuse_hidden = refuse_hidden  # an older Chrome or desktop app

        self.broken = set(broken)  # origins whose page script fails

        self.seen, self.fulfilled, self.closed, self.ops = [], [], [], []
        self.storage = storage or {}
        self.created, self.fetch_on, self.lagging = None, False, set(lagging)
        self.server = serve(self.handle, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}/devtools/browser/x"

    def handle(self, ws):
        navigating, current, pending = None, "null", None
        for raw in ws:
            m = json.loads(raw)
            method, p, sid = m["method"], m.get("params") or {}, m.get("sessionId")
            self.seen.append(method)
            result = {}
            if method == "Target.createTarget":
                self.created = p
                if p.get("hidden") and self.refuse_hidden:
                    ws.send(json.dumps({"id": m["id"], "error": {"code": -32602, "message": "Hidden target cannot be created in a new window"}}))
                    continue
                result = {"targetId": "BG"}
            elif method == "Target.attachToTarget":
                result = {"sessionId": "S-BG"}
            elif method == "Fetch.enable":
                assert "Network.setBypassServiceWorker" in self.seen, "a service worker would answer first"
                self.fetch_on = p["patterns"] == [{"urlPattern": "*", "requestStage": "Request"}]
            elif method == "Page.navigate":
                assert self.fetch_on, "navigated before Fetch was on"
                navigating = (m["id"], p["url"])
                ws.send(json.dumps({"method": "Fetch.requestPaused", "sessionId": sid,
                                    "params": {"requestId": "R" + str(m["id"]), "request": {"url": p["url"]}}}))
                continue  # the navigation completes only once the client answers the request
            elif method == "Fetch.fulfillRequest":
                self.fulfilled.append((navigating[1], p["responseCode"], p["body"]))
                target = navigating[1].rstrip("/")
                ws.send(json.dumps({"id": navigating[0], "sessionId": sid, "result": {"frameId": "F"}}))
                # A lagging origin: a stale load event (an earlier navigation's) arrives before it commits.
                ws.send(json.dumps({"method": "Page.loadEventFired", "sessionId": sid, "params": {}}))
                if target in self.lagging:
                    pending = target
                else:
                    current = target
            elif method == "Runtime.evaluate":
                args = json.loads("[" + re.search(r"\)\((.*)\)\s*$", p["expression"], re.S).group(1) + "]")
                if current in self.broken:
                    ws.send(json.dumps({"id": m["id"], "sessionId": sid, "error": {"message": "boom"}}))
                    continue
                if len(args) == 1:  # the read script
                    have = self.storage.get(current, {})
                    value = [[k, v] for k, v in have.items()] if current == args[0] else None
                    ws.send(json.dumps({"id": m["id"], "sessionId": sid, "result": {"result": {"value": value}}}))
                    continue
                expected, items = args
                if current == expected:
                    have, want = self.storage.setdefault(current, {}), dict(items)
                    for k in [k for k in have if k not in want]:
                        del have[k]
                        self.ops.append(("remove", current, k))
                    for k, v in want.items():
                        if have.get(k) != v:
                            have[k] = v
                            self.ops.append(("set", current, k))
                result = {"result": {"value": expected if current == expected else None}}
                if pending:
                    current, pending = pending, None  # it commits just after
            elif method == "Target.closeTarget":
                self.closed.append(p["targetId"])
            ws.send(json.dumps({"id": m["id"], "result": result, **({"sessionId": sid} if sid else {})}))


def _ls(origin, **items):
    return {"origin": origin, "localStorage": [{"name": k, "value": v} for k, v in items.items()]}


def test_mac_localstorage_loads_in_a_background_tab_with_no_network_request():
    fake = FakeMacChrome()
    try:
        saved = [_ls("https://a.test", k="v", t="1"), _ls("http://localhost:3000", s="x")]
        reached = browser_state._mac_origins(fake.url, saved, time.monotonic() + 10)
        assert reached == {"https://a.test", "http://localhost:3000"}
        assert fake.created == {"url": "about:blank", "background": True, "hidden": True}
        assert fake.fulfilled == [("https://a.test/", 200, ""), ("http://localhost:3000/", 200, "")]  # every request answered
        assert fake.storage == {"https://a.test": {"k": "v", "t": "1"}, "http://localhost:3000": {"s": "x"}}
        assert fake.closed == ["BG"]
        assert fake.seen.index("Network.setBypassServiceWorker") < fake.seen.index("Fetch.enable") < fake.seen.index("Page.navigate")
    finally:
        fake.server.shutdown()


def test_a_chrome_that_refuses_a_hidden_target_gets_the_background_tab():
    fake = FakeMacChrome(refuse_hidden=True)
    try:
        reached = browser_state._mac_origins(fake.url, [_ls("https://a.test", k="v")], time.monotonic() + 10)
        assert reached == {"https://a.test"} and fake.storage == {"https://a.test": {"k": "v"}}
        assert fake.created == {"url": "about:blank", "background": True} and fake.closed == ["BG"]
    finally:
        fake.server.shutdown()


def test_a_stale_load_event_never_writes_one_origins_items_into_another():
    fake = FakeMacChrome(storage={"https://a.test": {"k": "a"}}, lagging={"https://b.test"})
    try:
        reached = browser_state._mac_origins(fake.url, [_ls("https://a.test", k="a"), _ls("https://b.test", k="b")],
                                             time.monotonic() + 10)
        assert reached == {"https://a.test"}  # b.test is skipped, not written into a.test
        assert fake.storage == {"https://a.test": {"k": "a"}} and fake.closed == ["BG"]
    finally:
        fake.server.shutdown()


def test_matching_values_are_left_alone_and_an_empty_save_still_empties():
    fake = FakeMacChrome(storage={"https://a.test": {"k": "v", "old": "x"}, "https://c.test": {"t": "1"}})
    try:
        browser_state._mac_origins(fake.url, [_ls("https://a.test", k="v"), _ls("https://c.test")], time.monotonic() + 10)
        assert fake.ops == [("remove", "https://a.test", "old"), ("remove", "https://c.test", "t")]  # k=v untouched
        assert fake.storage == {"https://a.test": {"k": "v"}, "https://c.test": {}}
    finally:
        fake.server.shutdown()


def test_the_storage_script_diffs_and_never_clears():
    """The real script, in node, against a localStorage that records what it is asked."""
    import shutil
    import subprocess

    if not shutil.which("node"):
        pytest.skip("node is not installed")
    harness = """
const calls = []; const data = new Map([["k", "v"], ["old", "x"]]);
const localStorage = new Proxy({
  getItem: k => data.has(k) ? data.get(k) : null,
  setItem: (k, v) => { calls.push(["set", k]); data.set(k, v); },
  removeItem: k => { calls.push(["remove", k]); data.delete(k); },
  clear: () => { calls.push(["clear"]); data.clear(); },
}, { ownKeys: () => [...data.keys()], getOwnPropertyDescriptor: () => ({ enumerable: true, configurable: true }) });
const location = { origin: "https://a.test" };
const results = [SCRIPT_OK, SCRIPT_ELSEWHERE];
console.log(JSON.stringify({ calls, data: [...data], results }));
"""
    ok = f"{browser_state._SET_STORAGE_JS}({json.dumps('https://a.test')}, {json.dumps([['k', 'v'], ['n', '1']])})"
    elsewhere = f"{browser_state._SET_STORAGE_JS}({json.dumps('https://b.test')}, {json.dumps([])})"
    out = subprocess.run(["node", "-e", harness.replace("SCRIPT_OK", ok).replace("SCRIPT_ELSEWHERE", elsewhere)],
                         capture_output=True, text=True, check=True, timeout=30)
    got = json.loads(out.stdout)
    assert got["calls"] == [["remove", "old"], ["set", "n"]]  # no clear, k=v untouched
    assert got["data"] == [["k", "v"], ["n", "1"]] and got["results"] == ["https://a.test", None]


def test_mac_localstorage_round_trips_to_the_workspace(monkeypatch):
    """The other direction: localStorage an agent or person set on the Mac reaches the workspace."""
    base = {"cookies": [], "origins": [_ls("https://a.test", k="v0")], "visibleOrigins": ["https://a.test"]}
    current = {"cookies": [], "origins": [_ls("https://a.test", k="v1"), _ls("http://localhost:3000", s="new")]}
    previous = {"cookies": [], "origins": [_ls("https://a.test", k="v0")]}
    saved = _mac_save(monkeypatch, base, current, previous)["new"]
    assert {o["origin"]: o["localStorage"] for o in saved["origins"]} == {
        "https://a.test": [{"name": "k", "value": "v1"}], "http://localhost:3000": [{"name": "s", "value": "new"}]}


def _mac_save(monkeypatch, base, current, previous):
    _, uploads = _mac(monkeypatch, current, {"base": base, "prev": previous})
    browser_state.checkpoint({"action": "save", "source": "mac", "cdpUrl": MUX, "stateUrl": S3 + "new",
                              "previousStateUrl": S3 + "prev", "baseStateUrl": S3 + "base",
                              "baseOutUrl": S3 + "nextbase"})
    return uploads


def test_mac_source_bad_cookie_keeps_workspace_domain(monkeypatch):
    base = {"cookies": [_cookie(".a.test", "a0"), _cookie(".b.test", "b0")], "origins": []}
    bad = {**_cookie(".a.test", "a1"), "httpOnly": "yes"}  # malformed: never a sign-out
    current = {"cookies": [bad, _cookie(".b.test", "b1")], "origins": []}
    previous = {"cookies": [_cookie(".a.test", "ws"), _cookie(".b.test", "b0")], "origins": []}
    saved = _mac_save(monkeypatch, base, current, previous)["new"]
    assert {c["domain"]: c["value"] for c in saved["cookies"]} == {".a.test": "ws", ".b.test": "b1"}


def test_mac_source_drops_bad_origin(monkeypatch):
    base = {"cookies": [], "origins": [], "visibleOrigins": ["https://a.test"]}
    current = {"cookies": [], "origins": [{"origin": "https://a.test/path", "localStorage": []},
                                          {"origin": "https://b.test", "localStorage": [{"name": 1, "value": "x"}]}]}
    previous = {"cookies": [], "origins": [{"origin": "https://b.test", "localStorage": [{"name": "k", "value": "ws"}]}]}
    saved = _mac_save(monkeypatch, base, current, previous)["new"]
    assert saved["origins"] == previous["origins"]


def test_mac_source_holds_back_mass_signout(monkeypatch):
    domains = [f".d{n}.test" for n in range(8)]
    base = {"cookies": [_cookie(d, "0") for d in domains], "origins": []}
    current = {"cookies": [_cookie(domains[0], "1")], "origins": []}  # seven gone at once: a reset profile
    previous = {"cookies": [_cookie(d, "ws") for d in domains], "origins": []}
    saved = _mac_save(monkeypatch, base, current, previous)["new"]
    assert {c["domain"]: c["value"] for c in saved["cookies"]} == {domains[0]: "1", **{d: "ws" for d in domains[1:]}}
    # A few sign-outs are real and go through.
    few = {"cookies": [_cookie(d, "0") for d in domains[:6]], "origins": []}
    saved = _mac_save(monkeypatch, base, few, previous)["new"]
    assert {c["domain"] for c in saved["cookies"]} == set(domains[:6])


def test_cloud_source_unchanged(monkeypatch):
    """No source means cloud: a signed-out majority is written, and a malformed cookie too."""
    domains = [f".d{n}.test" for n in range(8)]
    base = {"cookies": [_cookie(d, "0") for d in domains], "origins": []}
    odd = {**_cookie(domains[0], "1"), "httpOnly": "yes"}
    previous = {"cookies": [_cookie(d, "ws") for d in domains], "origins": []}
    _, uploads = _mac(monkeypatch, {"cookies": [odd], "origins": []}, {"base": base, "prev": previous})
    browser_state.checkpoint(_params(stateUrl=S3 + "new", previousStateUrl=S3 + "prev",
                                     baseStateUrl=S3 + "base", baseOutUrl=S3 + "nextbase"))
    assert uploads["new"]["cookies"] == [odd]
    with pytest.raises(ValueError):  # an unknown source is refused
        browser_state.checkpoint({**_params(), "source": "phone"})


def test_mac_save_uploads_the_vetted_export_never_the_raw_body(monkeypatch):
    raw = {"cookies": [{**_cookie(".a.test", "a1"), "extra": "x"}, {**_cookie(".b.test", "b"), "secure": "no"}],
           "origins": [], "junk": 1}
    _, uploads = _mac(monkeypatch, raw, {})
    browser_state.checkpoint({"action": "save", "source": "mac", "cdpUrl": MUX, "stateUrl": S3 + "new"})
    assert uploads["new"] == {"cookies": [_cookie(".a.test", "a1")], "origins": []}


def test_a_closed_origins_localstorage_is_read_in_the_background_and_a_failing_one_skipped():
    fake = FakeMacChrome(storage={"https://left.test": {"token": "t"}, "https://gone.test": {"k": "v"}},
                         broken={"https://broken.test"})
    try:
        found = browser_state._read_origins(fake.url, ["https://left.test", "https://broken.test", "https://gone.test"],
                                            time.monotonic() + 10)
        assert found == {"https://left.test": [{"name": "token", "value": "t"}],
                         "https://gone.test": [{"name": "k", "value": "v"}]}  # broken.test is left out, not fatal
        assert fake.created == {"url": "about:blank", "background": True, "hidden": True} and fake.closed == ["BG"]
        assert [f[0] for f in fake.fulfilled] == ["https://left.test/", "https://broken.test/", "https://gone.test/"]
    finally:
        fake.server.shutdown()


def test_a_save_keeps_the_origins_a_run_navigated_away_from_or_closed(monkeypatch):
    """prod 2026-09-29: the export reads open pages only, so localStorage a run wrote on a page it
    then left (or whose tab closed at release) never reached the workspace."""
    from bridge import cdp_mux

    exported = {"cookies": [], "origins": [_ls("https://open.test", o="1")]}
    uploads, forgotten, reads = {}, [], []

    def run(argv, **kwargs):
        if "save" in argv:
            Path(argv[-1]).write_text(json.dumps(exported))
        return type("Completed", (), {"returncode": 0})()

    def read(url, origins, deadline):
        reads.append(origins)
        return {"https://left.test": [{"name": "token", "value": "t"}], "https://empty.test": []}

    s3 = "https://bucket.s3.us-east-1.amazonaws.com/browser-state/owner/"
    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(cdp_mux, "visited_origins", lambda url=None: {"https://open.test": 1, "https://left.test": 2,
                                                                      "https://broken.test": 3, "https://empty.test": 4})
    monkeypatch.setattr(cdp_mux, "forget_origins", lambda url, seen: forgotten.append(dict(seen)))
    monkeypatch.setattr(browser_state, "_read_origins", read)
    monkeypatch.setattr(browser_state, "_upload", lambda url, body, deadline: uploads.__setitem__(url.split("/")[-1], json.loads(body)))
    browser_state.checkpoint(_params(stateUrl=s3 + "new"))
    # open.test is exported; the rest newest landing first, so a stuck old read never starves them
    assert reads == [["https://empty.test", "https://broken.test", "https://left.test"]]
    assert {o["origin"]: o["localStorage"] for o in uploads["new"]["origins"]} == {
        "https://open.test": [{"name": "o", "value": "1"}],
        "https://left.test": [{"name": "token", "value": "t"}],
        "https://empty.test": [],  # a real clear (logout, then the tab left): recorded, not skipped
    }
    # Nothing committed holds left.test yet (this upload is not the commit): read it again next
    # save. broken.test failed: also read again. empty.test: nothing committed to clear.
    assert forgotten == [{"https://empty.test": 4}]  # the exact landing that was read

    # The next save's committed copy (previousStateUrl) holds left.test as read: now it is done.
    committed = {"cookies": [], "origins": [_ls("https://left.test", token="t")]}
    monkeypatch.setattr(browser_state, "_download", lambda url, deadline: json.dumps(committed).encode())
    browser_state.checkpoint(_params(stateUrl=s3 + "next", previousStateUrl=s3 + "new"))
    assert forgotten[-1] == {"https://left.test": 2, "https://empty.test": 4}


def test_a_failed_save_forgets_nothing(monkeypatch):
    from bridge import cdp_mux

    forgotten = []

    def run(argv, **kwargs):
        if "save" in argv:
            Path(argv[-1]).write_text('{"cookies":[],"origins":[]}')
        return type("Completed", (), {"returncode": 0})()

    def upload(url, body, deadline):
        raise OSError("PUT failed")

    monkeypatch.setattr(browser_state.subprocess, "run", run)
    monkeypatch.setattr(cdp_mux, "visited_origins", lambda url=None: {"https://left.test": 1})
    monkeypatch.setattr(cdp_mux, "forget_origins", lambda url, origins: forgotten.append(origins))
    monkeypatch.setattr(browser_state, "_read_origins", lambda url, origins, deadline: {"https://left.test": [{"name": "k", "value": "v"}]})
    monkeypatch.setattr(browser_state, "_upload", upload)
    with pytest.raises(RuntimeError):
        browser_state.checkpoint(_params())
    assert forgotten == []  # the next save reads it again
