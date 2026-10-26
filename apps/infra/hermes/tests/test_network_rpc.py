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
