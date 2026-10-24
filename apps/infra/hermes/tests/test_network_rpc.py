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


def test_invite_returns_the_line_and_never_logs_it(net, caplog):
    caplog.set_level(logging.DEBUG)
    line = "lightbulb join TICKET-abc123secret"
    net.answer(**{"team.invite": {"line": line, "expires_at": "2026-10-25T00:00:00Z"}})
    answer = _rpc("network.invite", {"for": "Aden", "name": "Aden's Mac"})
    assert answer == {"ok": True, "payload": {"line": line, "expires_at": "2026-10-25T00:00:00Z"}}
    assert _sent(net, "team.invite") == [{"op": "team.invite", "mode": "person", "for": "Aden", "name": "Aden's Mac"}]
    assert "TICKET" not in caplog.text


def test_network_rpc_when_off_answers_unavailable(net, monkeypatch):
    monkeypatch.setattr(bridge, "NETWORK_KEY", "")
    for method, params in (("network.invite", {"for": "x"}), ("network.sessions", {}),
                           ("network.app", {"op": "directory"}), ("network.hold", {"hold": "H1"}),
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
    net.answer(**{"hold.answer": {"id": "H1", "verdict": "allow", "scope": "always"},
                  "directory": {"people": []}, "app.send": {"id": "r1", "state": "stored"}})
    assert _rpc("network.hold", {"hold": "H1", "verdict": "allow", "scope": "always", "decided_by": "user_x"}) == \
        {"ok": True, "payload": {"id": "H1", "verdict": "allow", "scope": "always"}}
    assert _sent(net, "hold.answer")[-1] == {"op": "hold.answer", "hold": "H1", "verdict": "allow",
                                             "scope": "always", "decided_by": "user_x"}
    assert _rpc("network.app", {"op": "directory", "query": "ad"})["payload"] == {"people": []}
    assert _sent(net, "directory")[-1] == {"op": "directory", "query": "ad"}
    assert _rpc("network.app", {"op": "send", "app": "slack", "person": "ab12cd34", "to": "mac",
                                "text": "hi", "request_id": "q"})["payload"] == {"id": "r1", "state": "stored"}
    assert _sent(net, "app.send")[-1] == {"op": "app.send", "app": "slack", "person": "ab12cd34", "to": "mac",
                                          "text": "hi", "request_id": "q"}
    assert _rpc("network.app", {"op": "nope"})["error"]["code"] == "INVALID_REQUEST"
    net.answer(**{"hold.answer": {"ok": False, "error": "not_found"}})
    assert _rpc("network.hold", {"hold": "H9", "verdict": "allow", "decided_by": "u"})["error"]["code"] == "not_found"


def test_network_sessions_rereads_the_file_and_declares_it(net):
    assert network.ensure_started(background=False)
    new = {"workspace": "Acme", "sessions": [{"name": "friday", "agent": "Friday", "agentId": "a-friday"},
                                             {"name": "atlas", "agent": "Atlas", "agentId": "a-atlas"}]}
    net.s3.seed("org_test/network/sessions.json", json.dumps(new).encode())
    assert _rpc("network.sessions") == {"ok": True, "payload": {"count": 2}}
    assert net.calls("cloud.sessions.set")[-1]["sessions"] == new["sessions"]
    assert network.agent_for_session("atlas") == "a-atlas"
    assert json.loads(network.SESSIONS_FILE.read_text()) == new
