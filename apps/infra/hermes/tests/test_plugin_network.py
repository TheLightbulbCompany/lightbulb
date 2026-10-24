"""The lightbulb_network tool: `from` is the turn's agent's session, request ids replay on a re-run."""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402
from bridge import network  # noqa: E402

SESSION = "agent:a-friday:lightbulb:network:t-1"
CTX = {"apiUrl": "https://api.invalid", "ownerId": "org_test", "hookToken": "h", "agentId": "a-friday",
       "runId": None, "eventId": "m1", "sessionKey": SESSION, "family": "unavailable"}


@pytest.fixture
def net(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    turn_context.write(tmp_path, SESSION, CTX)
    monkeypatch.setattr(network, "_running", lambda: True)
    monkeypatch.setattr(network, "_sessions", {"sessions": [{"name": "friday", "agentId": "a-friday"},
                                                            {"name": "ceo", "agentId": "a-ceo"}]})
    calls = []
    monkeypatch.setattr(network, "ask", lambda op, body=None, timeout_s=10.0: calls.append((op, body))
                        or {"id": "r1", "state": "sent"})
    return calls


def _send(**extra):
    return lightbulb._network({"action": "send", "to": "mac-prasiddha", "text": "hi", **extra},
                              session_id=SESSION)


def test_send_derives_from_from_the_turns_agent_not_the_model(net):
    assert json.loads(_send(**{"from": "ceo"})) == {"id": "r1", "state": "sent"}
    assert net == [("send", {"from": "friday", "to": "mac-prasiddha", "text": "hi", "request_id": "m1:net:1"})]


def test_an_agent_without_a_declared_session_is_told_so(net, monkeypatch):
    monkeypatch.setattr(network, "_sessions", {"sessions": [{"name": "ceo", "agentId": "a-ceo"}]})
    assert _send() == lightbulb.NETWORK_NO_SESSION
    assert net == []


def test_a_rerun_turn_reuses_its_request_ids(net, tmp_path):
    _send()
    _send()
    turn_context.write(tmp_path, SESSION, CTX)  # the bridge writes a fresh context for the re-run
    _send()
    _send()
    assert [b["request_id"] for _, b in net] == ["m1:net:1", "m1:net:2", "m1:net:1", "m1:net:2"]


def test_the_tool_answers_cleanly_when_the_network_is_off(net, monkeypatch):
    monkeypatch.setattr(network, "_running", lambda: False)
    assert _send() == lightbulb.NETWORK_OFF

    def off(op, body=None, timeout_s=10.0):
        raise network.NetworkOff(op)
    monkeypatch.setattr(network, "ask", off)
    assert lightbulb._network({"action": "directory"}, session_id=SESSION) == lightbulb.NETWORK_OFF
    assert lightbulb._network({"action": "receipt", "request_id": "m1:net:1"},
                              session_id=SESSION) == lightbulb.NETWORK_OFF


def test_a_refusal_carries_only_its_code(net, monkeypatch):
    def refuse(op, body=None, timeout_s=10.0):
        raise network.OpError(op, "unknown_session")
    monkeypatch.setattr(network, "ask", refuse)
    assert _send() == "Lightbulb network send refused: unknown_session"
