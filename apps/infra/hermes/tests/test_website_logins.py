"""The bridge half of website logins: asks wait on the backend and never outlive their turn.
The Hermes seams themselves are proven against the real image in smoke/website_logins_smoke.py."""
import json

import pytest

from bridge import sync, website_logins as wl


@pytest.fixture
def backend(monkeypatch):
    """A scripted backend: `polls` is the sequence of request states the wait will see."""
    state = {"calls": [], "polls": []}

    def call(method, path, body=None, timeout=20.0):
        state["calls"].append((method, path, body))
        if path == "/requests":
            return {"requestId": "a" * 32}
        if method == "GET" and path.startswith("/requests/"):
            return state["polls"].pop(0) if state["polls"] else {"status": "pending"}
        if path.endswith("/resolve"):
            return {"username": "me@example.com", "password": "pw-not-real"}
        return {}

    monkeypatch.setattr(wl, "_call", call)
    monkeypatch.setattr(wl, "POLL_S", 0)
    monkeypatch.setattr(wl.time, "sleep", lambda _s: None)
    return state


def _in_turn(fn, aborted=lambda: False):
    token = wl._ASK.set({"agentId": "agent-1", "roomId": "room-1", "threadRootMessageId": None, "aborted": aborted})
    try:
        return fn()
    finally:
        wl._ASK.reset(token)


def test_login_ask_files_the_request_then_resolves_the_saved_login(backend):
    backend["polls"] = [{"status": "pending"}, {"status": "answered", "loginId": "vault_0123456789ab"}]
    answer = _in_turn(lambda: wl._save_login_prompt("https://app.example.com", "app.example.com"))
    assert answer == {"identifier": "me@example.com", "password": "pw-not-real"}
    filed = backend["calls"][0]
    assert filed[:2] == ("POST", "/requests")
    assert filed[2] == {"kind": "login", "origin": "https://app.example.com", "agentId": "agent-1",
                        "roomId": "room-1", "threadRootMessageId": None}
    assert backend["calls"][-1][1] == "/logins/vault_0123456789ab/resolve"


def test_code_ask_returns_the_code(backend):
    backend["polls"] = [{"status": "answered", "kind": "code", "code": "123456"}]
    assert _in_turn(lambda: wl._code_prompt("accounts.google.com", "")) == "123456"
    assert backend["calls"][0][2]["origin"] == "https://accounts.google.com"


def test_declined_ask_is_none_and_not_cancelled(backend):
    backend["polls"] = [{"status": "declined"}]
    assert _in_turn(lambda: wl._save_login_prompt("https://x.com", "x.com")) is None
    assert not any(path.endswith("/cancel") for _m, path, _b in backend["calls"])


def test_an_aborted_turn_stops_waiting_and_closes_the_ask(backend):
    assert _in_turn(lambda: wl._code_prompt("x.com", ""), aborted=lambda: True) == ""
    assert backend["calls"][-1][:2] == ("POST", f"/requests/{'a' * 32}/cancel")


def test_a_long_wait_keeps_the_browser_lease(backend, monkeypatch):
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr(wl.time, "monotonic", lambda: next(clock))
    renewed = []
    monkeypatch.setattr(wl.browser_control, "renew", lambda: renewed.append(1))
    backend["polls"] = [{"status": "pending"}] * 8 + [{"status": "answered", "code": "9"}]
    assert _in_turn(lambda: wl._code_prompt("x.com", "")) == "9"
    assert len(renewed) >= 2


def test_no_turn_no_ask(backend):
    assert wl._save_login_prompt("https://x.com", "x.com") is None
    assert backend["calls"] == []


def test_hermes_desktop_pointers_are_rewritten_in_json_results():
    raw = json.dumps({"error": "Tell them to run `hermes vault add` or use Desktop → Settings → Passwords & Logins for x."})
    out = wl.transform_tool_result("browser_vault_save_login", raw)
    assert "hermes vault add" not in out and "Connections" in json.loads(out)["error"]
    assert wl.transform_tool_result("browser_navigate", raw) is None


def test_www_twin_is_the_only_extra_origin():
    assert wl._allowed("https://supademo.com") == ("https://supademo.com", "https://www.supademo.com")
    assert wl._allowed("https://www.x.com") == ("https://www.x.com", "https://x.com")


def test_the_local_vault_never_syncs():
    assert sync.is_excluded("vault/vault.key") and sync.is_excluded("vault/vault.json.enc")
