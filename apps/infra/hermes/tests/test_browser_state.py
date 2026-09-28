import json
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
