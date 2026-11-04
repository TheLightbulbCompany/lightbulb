"""The backend's network.* RPC methods on /plugins/lightbulb-channel/rpc."""
from __future__ import annotations

import json
import logging

import bridge
from bridge import network, server
from test_network import KEY, _until, net  # noqa: F401 -- the fake sidecar fixture


def _sent(n, op):
    """What the bridge sent for `op`, without the line's random request id."""
    return [{k: v for k, v in c.items() if k != "id"} for c in n.calls(op)]


def _rpc(method, params=None):
    status, body = server._rpc({"method": method, "params": params or {}})
    assert status == 200
    return body


def test_join_starts_a_background_team_join_and_never_logs_the_link(net, caplog):
    caplog.set_level(logging.DEBUG)
    ticket = "TICKET-abc123secret"
    net.answer(**{"team.get": {"members": [{"key": "me"}]},
                  "team.join": {"state": "admitted", "reason": None, "team": "acme", "online": 2}})
    assert _rpc("network.join", {"ticket": ticket}) == {"ok": True, "payload": {"state": "joining"}}
    assert _until(lambda: net.calls("team.join"))
    sent = _sent(net, "team.join")[0]
    assert sent["ticket"] == ticket and sent["op"] == "team.join"
    assert _until(lambda: "network.join state=admitted" in caplog.text)
    assert "TICKET" not in caplog.text


def test_join_is_refused_once_the_workspace_is_on_a_network(net):
    net.answer(**{"team.get": {"members": [{"key": "me"}, {"key": "admin"}]}})
    answer = _rpc("network.join", {"ticket": "T"})
    assert answer["ok"] is False and answer["error"]["code"] == "already_in_team"
    assert net.calls("team.join") == []
    assert _rpc("network.join", {})["error"]["code"] == "INVALID_REQUEST"


def test_network_rpc_when_off_answers_unavailable(net, monkeypatch):
    monkeypatch.setattr(bridge, "NETWORK_KEY", "")
    for method, params in (("network.join", {"ticket": "t"}), ("network.sessions", {}),
                           ("network.hold", {"hold": "H1"}),
                           ("network.wake", {})):
        answer = _rpc(method, params)
        assert answer["ok"] is False and answer["error"]["code"] == "UNAVAILABLE", method
    assert net.spawns() == 0


def test_network_wake_only_starts_the_sidecar(net):
    assert not network._running()
    assert _rpc("network.wake") == {"ok": True, "payload": {}}
    assert network._running() and net.spawns() == 1
    assert _until(lambda: net.calls("inbox.peek"))       # the start's own sweep, nothing else
    assert {c["op"] for c in net.calls()} <= {"subscribe", "inbox.peek", "snapshot"}


def test_rpc_starts_the_sidecar_in_the_foreground_and_maps_ops(net):
    net.answer(**{"hold.answer": {"id": "H1", "verdict": "allow", "scope": "always"}})
    assert _rpc("network.hold", {"hold": "H1", "verdict": "allow", "scope": "always", "decided_by": "user_x"}) == \
        {"ok": True, "payload": {"id": "H1", "verdict": "allow", "scope": "always"}}
    assert _sent(net, "hold.answer")[-1] == {"op": "hold.answer", "hold": "H1", "verdict": "allow",
                                             "scope": "always", "decided_by": "user_x"}
    # Hosted apps reach the network through their person's own computer, never a workspace.
    assert _rpc("network.app", {"op": "directory"})["error"]["code"] == "UNAVAILABLE"
    net.answer(**{"hold.answer": {"ok": False, "error": "not_found"}})
    assert _rpc("network.hold", {"hold": "H9", "verdict": "allow", "decided_by": "u"})["error"]["code"] == "not_found"


def test_network_sessions_rereads_the_file_and_declares_it(net):
    assert network.ensure_started(background=False)
    new = {"workspace": "Acme", "members": ["user_a", "user_b"], "revoked_keys": ["k-signed-out"],
           "sessions": [{"name": "friday", "agent": "Friday", "agentId": "a-friday"},
                        {"name": "atlas", "agent": "Atlas", "agentId": "a-atlas"}]}
    net.s3.seed("org_test/network/sessions.json", json.dumps(new).encode())
    assert _rpc("network.sessions") == {"ok": True, "payload": {"count": 2}}
    declared = net.calls("cloud.sessions.set")[-1]
    assert (declared["sessions"], declared["members"]) == (new["sessions"], ["user_a", "user_b"])
    assert declared["revoked_keys"] == ["k-signed-out"]  # a sign-out reaches the warm engine
    assert "member_keys" not in declared
    assert network.agent_for_session("atlas") == "a-atlas"
    assert json.loads(network.SESSIONS_FILE.read_text()) == new


def test_a_retried_join_with_the_same_link_is_that_join_not_a_second(net):
    ticket = "TICKET-retry-once"
    net.answer(**{"team.get": {"members": [{"key": "me"}]},
                  "team.join": {"state": "admitted", "reason": None, "team": "acme", "online": 2}})
    assert _rpc("network.join", {"ticket": ticket})["payload"] == {"state": "joining"}
    assert _until(lambda: net.calls("team.join"))
    # Joined now: the team has two members, yet the same link's retry answers joining again.
    net.answer(**{"team.get": {"members": [{"key": "me"}, {"key": "admin"}]}})
    assert _until(lambda: network.join_known(ticket))
    assert _rpc("network.join", {"ticket": ticket}) == {"ok": True, "payload": {"state": "joining"}}
    assert len(net.calls("team.join")) == 1
    assert _rpc("network.join", {"ticket": "another-link"})["error"]["code"] == "already_in_team"


def test_leave_runs_team_leave(net):
    net.answer(**{"team.leave": {"left": True}})
    assert _rpc("network.leave") == {"ok": True, "payload": {"left": True}}
    assert _sent(net, "team.leave")[0]["op"] == "team.leave"


# The backend's memory.* calls (core/services/memory_sync.py, memory_api.py): one engine op each.
MEMORY_CALLS = {
    "memory.import": ({"entries": [{"subject": "api port", "fact": "8080", "kind": "fact", "scope": "team",
                                    "when": 1790000000, "key": "k1",
                                    "source": {"harness": "workspace", "repo": None, "session": None,
                                               "on_behalf_of": "Ana"}, "replaces": []}]},
                      {"ids": ["e1"], "refused": []}),
    "memory.list": ({"since_seq": 7}, {"entries": [{"id": "e1", "author": "k", "tombstone": True, "seq": 8}],
                                       "next": 8}),
    "memory.remember": ({"subject": "s", "fact": "f", "kind": "fact", "scope": "me", "on_behalf_of": "user_a"},
                        {"stored": False, "refused": "too_long", "neighbours": []}),
    "memory.forget": ({"entry": "e1"}, {}),
    "memory.forget_author": ({"author": "k" * 52}, {"forgotten": 3}),
}


def test_memory_ops_reach_the_engine_with_their_params_unchanged(net):
    for method, (params, answer) in MEMORY_CALLS.items():
        net.answer(**{method: answer})
        assert _rpc(method, params) == {"ok": True, "payload": answer}, method
        assert _sent(net, method)[-1] == {"op": method, **params}, method


def test_memory_op_refused_by_the_engine_is_ok_false(net):
    net.answer(**{"memory.import": {"ok": False, "error": "bad_request"}})
    answer = _rpc("memory.import", {"entries": [{"kind": "claim"}]})
    assert answer["ok"] is False and answer["error"]["code"] == "bad_request"


def test_memory_ops_when_off_answer_unavailable(net, monkeypatch):
    monkeypatch.setattr(bridge, "NETWORK_KEY", "")
    for method, (params, _) in MEMORY_CALLS.items():
        assert _rpc(method, params)["error"]["code"] == "UNAVAILABLE", method


def test_memory_op_passes_the_backends_timeout_to_the_engine(net, monkeypatch):
    seen = []
    real = network.ask
    monkeypatch.setattr(network, "ask", lambda op, body=None, timeout_s=10.0: seen.append((op, timeout_s)) or real(op, body, timeout_s))
    net.answer(**{"memory.import": {"ids": [], "refused": []}})
    status, body = server._rpc({"method": "memory.import", "params": {"entries": []}, "timeoutMs": 60000})
    assert status == 200 and body["ok"] is True
    assert [t for op, t in seen if op == "memory.import"] == [60.0]  # not the engine's 10 s default


def test_the_image_pins_an_engine_that_speaks_memory():
    # The fake sidecar above answers memory.* whatever the image ships. The engine first knew
    # memory.* in v0.7.0 (TheLightbulbCompany/cli #83); an older pin refuses every op, so no
    # team entry ever reaches workspace memory.
    import re
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    pins = re.findall(
        r"cli/releases/download/v(\d+)\.(\d+)\.(\d+)/lightbulb-linux-arm64", dockerfile
    )
    assert len(pins) == 1
    assert tuple(map(int, pins[0])) >= (0, 7, 0)


def test_network_invite_returns_ticket_and_team_id(net):
    net.answer(**{"team.invite": {"ticket": "T-1", "expires_at": 1790000000},
                  "team.get": {"team_id": "tm1", "members": [{"key": "me"}]}})
    assert _rpc("network.invite") == {"ok": True, "payload": {
        "ticket": "T-1", "team_id": "tm1", "expires_at": 1790000000}}
    assert _sent(net, "team.invite")[0] == {"op": "team.invite", "mode": "once", "code": False}
    assert _sent(net, "team.get")


def test_network_invite_reads_the_team_before_minting(net):
    net.answer(**{"team.get": {"ok": False, "error": "unsupported"}})
    assert _rpc("network.invite")["ok"] is False
    assert not _sent(net, "team.invite")


def test_network_invite_refused_is_an_op_error(net):
    net.answer(**{"team.invite": {"ok": False, "error": "unsupported"}})
    answer = _rpc("network.invite")
    assert answer["ok"] is False and answer["error"]["code"] == "unsupported"


def test_network_team_counts_members(net):
    net.answer(**{"team.get": {"team_id": "tm1", "members": [{"key": "a"}, {"key": "b"}]}})
    assert _rpc("network.team") == {"ok": True, "payload": {"team_id": "tm1", "member_count": 2}}


def test_network_invite_logs_no_ticket(net, caplog):
    caplog.set_level(logging.DEBUG)
    net.answer(**{"team.invite": {"ticket": "TICKET-secret9", "expires_at": 1},
                  "team.get": {"team_id": "tm1", "members": []}})
    _rpc("network.invite")
    assert "TICKET" not in caplog.text


def test_a2a_send_declares_then_sends_with_the_a2a_object(net):
    assert network.ensure_started(background=False)
    a2a = {"role": "ROLE_USER", "parts": [{"text": "hi"}], "metadata": {"https://x/ext": "jws.jws.jws"},
           "extensions": ["https://x/ext"]}
    net.answer(**{"app.send": {"receipt": "r1"}})
    params = {"to": "atlas", "text": "hi", "request_id": "req-1", "person": "user_a", "a2a": a2a}
    assert _rpc("network.a2a_send", params) == {"ok": True, "payload": {"receipt": "r1"}}
    ops = [c["op"] for c in net.calls() if c["op"] in ("cloud.sessions.set", "app.send")]
    assert ops[-2:] == ["cloud.sessions.set", "app.send"]
    assert {"name": "a2a-user_a", "agent": "app", "title": "A2A"} in _sent(net, "cloud.sessions.set")[-1]["sessions"]
    assert _sent(net, "app.send")[-1] == {"op": "app.send", "app": "a2a", "person": "user_a", "to": "atlas",
                                          "text": "hi", "request_id": "req-1", "a2a": a2a, "wait": True}
    declared_once = len(net.calls("cloud.sessions.set"))
    _rpc("network.a2a_send", params)  # declared once per process
    assert len(net.calls("cloud.sessions.set")) == declared_once
    assert len(net.calls("app.send")) == 2


def test_a2a_send_with_missing_params_is_invalid_request(net):
    full = {"to": "atlas", "text": "hi", "request_id": "r", "person": "u", "a2a": {"role": "ROLE_USER"}}
    for key in full:
        answer = _rpc("network.a2a_send", {k: v for k, v in full.items() if k != key})
        assert answer["ok"] is False and answer["error"]["code"] == "INVALID_REQUEST", key
    assert _rpc("network.a2a_send", {**full, "a2a": "x"})["error"]["code"] == "INVALID_REQUEST"
    assert net.calls("app.send") == []


def test_a2a_send_when_the_sidecar_is_off_is_unavailable(net, monkeypatch):
    monkeypatch.setattr(bridge, "NETWORK_KEY", "")
    answer = _rpc("network.a2a_send", {"to": "a", "text": "t", "request_id": "r", "person": "u", "a2a": {}})
    assert answer["ok"] is False and answer["error"]["code"] == "UNAVAILABLE"


def test_a2a_declares_accumulate_across_people(net):
    assert network.ensure_started(background=False)
    net.answer(**{"app.send": {"receipt": "r"}})
    p = lambda who: {"to": "t", "text": "x", "request_id": "r-" + who, "person": who, "a2a": {"role": "ROLE_USER"}}
    _rpc("network.a2a_send", p("A"))
    _rpc("network.a2a_send", p("B"))
    n = len(net.calls("cloud.sessions.set"))
    _rpc("network.a2a_send", p("A"))
    assert len(net.calls("cloud.sessions.set")) == n
    sessions = _sent(net, "cloud.sessions.set")[-1]["sessions"]
    assert {s["name"] for s in sessions if s["name"].startswith("a2a-")} == {"a2a-A", "a2a-B"}
    # The engine refuses the whole set unless every entry has a name and an agent
    # (net_ops.rs `cloud.sessions.set`): the shape a fake engine cannot catch.
    assert all(isinstance(s.get("name"), str) and isinstance(s.get("agent"), str) for s in sessions)


def test_a2a_failed_declare_is_not_cached(net):
    assert network.ensure_started(background=False)
    net.answer(**{"cloud.sessions.set": {"ok": False, "error": "internal"}})
    body = {"to": "t", "text": "x", "request_id": "r", "person": "A", "a2a": {"role": "ROLE_USER"}}
    assert _rpc("network.a2a_send", body)["ok"] is False
    assert network._a2a_declared == set() and net.calls("app.send") == []
    net.answer(**{"cloud.sessions.set": {"ok": True, "result": {}}, "app.send": {"receipt": "r"}})
    assert _rpc("network.a2a_send", body)["ok"] is True


def test_a2a_send_empty_strings_are_invalid_request(net):
    full = {"to": "a", "text": "t", "request_id": "r", "person": "u", "a2a": {"role": "ROLE_USER"}}
    for key in ("to", "text", "request_id", "person"):
        assert _rpc("network.a2a_send", {**full, key: ""})["error"]["code"] == "INVALID_REQUEST", key


def test_a_sessions_reload_keeps_the_a2a_declarations(net):
    assert network.ensure_started(background=False)
    net.answer(**{"app.send": {"receipt": "r"}})
    _rpc("network.a2a_send", {"to": "t", "text": "x", "request_id": "r", "person": "A", "a2a": {"role": "ROLE_USER"}})
    new = {"sessions": [{"name": "atlas", "agent": "Atlas", "agentId": "a-atlas"}]}
    net.s3.seed("org_test/network/sessions.json", json.dumps(new).encode())
    assert _rpc("network.sessions") == {"ok": True, "payload": {"count": 1}}
    declared = net.calls("cloud.sessions.set")[-1]["sessions"]
    assert declared == new["sessions"] + [{"name": "a2a-A", "agent": "app", "title": "A2A"}]  # a reply to A still lands
    assert network._a2a_declared == {"A"}
    assert json.loads(network.SESSIONS_FILE.read_text()) == new  # the file stays the backend's
