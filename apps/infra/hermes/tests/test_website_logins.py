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


def _in_turn(fn, aborted=lambda: False, ctx=None):
    ask = ctx if ctx is not None else {}
    ask.update({"agentId": "agent-1", "roomId": "room-1", "threadRootMessageId": None, "aborted": aborted})
    token = wl._ASK.set(ask)
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


def test_a_code_is_typed_into_the_browser_by_the_person_never_handed_to_the_agent(backend, monkeypatch):
    """The ask is one line in the room; the wait watches the page, not a form. Nobody is polled
    for the code and nothing is cancelled: there is no request to cancel."""
    looks = iter([True, True, False])  # the code field is gone on the third look: they typed it
    monkeypatch.setattr(wl, "_code_field_present", lambda: next(looks))
    monkeypatch.setattr(wl, "_page_heading", lambda: "Check your text messages")
    ctx = {}
    assert _in_turn(lambda: wl._code_prompt("accounts.intuit.com", ""), ctx=ctx) == ""
    assert ctx["code"] == "page_moved"
    assert backend["calls"] == [("POST", "/requests", {
        "kind": "code", "origin": "https://accounts.intuit.com", "agentId": "agent-1", "roomId": "room-1",
        "threadRootMessageId": None, "hint": "Check your text messages"})]


def test_nobody_typing_the_code_in_time_is_reported_as_such(backend, monkeypatch):
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr(wl.time, "monotonic", lambda: next(clock))
    renewed = []
    monkeypatch.setattr(wl.browser_control, "renew", lambda: renewed.append(1))
    monkeypatch.setattr(wl, "_code_field_present", lambda: None)  # the page never answers
    monkeypatch.setattr(wl, "_page_heading", lambda: "")
    ctx = {}
    assert _in_turn(lambda: wl._code_prompt("x.com", ""), ctx=ctx) == ""
    assert ctx["code"] == "nobody_came"
    assert len(renewed) >= 2  # the browser lease was kept through the wait
    assert not any(path.endswith("/cancel") for _m, path, _b in backend["calls"])


def test_hermes_code_declined_becomes_what_happened(backend):
    declined = json.dumps({"success": False, "error_type": "code_declined",
                           "error": "The user did not enter a code. Do not ask again this turn."})
    moved = json.loads(_in_turn(lambda: wl.transform_tool_result("browser_vault_enter_code", declined),
                                ctx={"code": "page_moved"}))
    assert moved["success"] is True and moved["source"] == "person" and "browser_snapshot" in moved["next"]
    nobody = json.loads(_in_turn(lambda: wl.transform_tool_result("browser_vault_enter_code", declined),
                                 ctx={"code": "nobody_came"}))
    assert nobody["error_type"] == "nobody_came" and "Never ask for the code in chat" in nobody["error"]
    assert wl.transform_tool_result("browser_vault_enter_code", declined) is None  # no turn: Hermes's own text


def test_declined_ask_is_none_and_not_cancelled(backend):
    backend["polls"] = [{"status": "declined"}]
    assert _in_turn(lambda: wl._save_login_prompt("https://x.com", "x.com")) is None
    assert not any(path.endswith("/cancel") for _m, path, _b in backend["calls"])


def test_an_aborted_turn_stops_waiting_and_closes_the_ask(backend, monkeypatch):
    monkeypatch.setattr(wl, "_code_field_present", lambda: True)
    monkeypatch.setattr(wl, "_page_heading", lambda: "")
    ctx = {}
    assert _in_turn(lambda: wl._code_prompt("x.com", ""), aborted=lambda: True, ctx=ctx) == ""
    assert ctx["code"] == "nobody_came"
    assert _in_turn(lambda: wl._save_login_prompt("https://x.com", "x.com"), aborted=lambda: True) is None
    assert backend["calls"][-1][:2] == ("POST", f"/requests/{'a' * 32}/cancel")


def test_a_long_wait_keeps_the_browser_lease(backend, monkeypatch):
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr(wl.time, "monotonic", lambda: next(clock))
    renewed = []
    monkeypatch.setattr(wl.browser_control, "renew", lambda: renewed.append(1))
    backend["polls"] = [{"status": "pending"}] * 8 + [{"status": "answered", "loginId": "vault_0123456789ab"}]
    assert _in_turn(lambda: wl._save_login_prompt("https://x.com", "x.com"))["identifier"] == "me@example.com"
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
