"""Jev slice 2, bridge half: a backend-routed turn replies where the
backend placed it. Without ``routing`` (an older backend) the turn still runs (N-1 safe)."""

import sys
import types

import pytest
from bridge import turn
from tests import factories as f

ROOT = "55555555-5555-4555-8555-555555555555"
ROUTING = {"decidedBy": "jev", "placement": "thread"}


def _run(monkeypatch, body):
    """Run one turn; return the reply POST body and every published frame payload."""
    sent = []
    monkeypatch.setattr(turn, "publish_frame",
                        lambda endpoint, channel, *events: sent.extend(e["payload"] for e in events) or 200)
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, "ack", turn.ZERO_USAGE))
    return sent


def _reply(posted):
    return next(b for path, b in posted if path.startswith("agent-messages/"))


def test_a_routed_turn_runs(home, posted, monkeypatch):
    _run(monkeypatch, None)
    turn.run_turn("ceo", f.ingress(eventKind="room_event", routing=ROUTING, replyThreadRootId=None))
    assert _reply(posted)["text"] == "ack"


def test_an_unrouted_room_event_runs_without_a_bedrock_call(home, posted, monkeypatch):
    """Bedrock removal A1: the ambient gate (a Bedrock Converse per room event) is gone, so an older
    backend's unrouted room_event takes the turn -- what the gate did whenever it could not answer."""
    no_aws = types.SimpleNamespace(client=lambda *a, **k: pytest.fail("no AWS client on a room turn"))
    monkeypatch.setitem(sys.modules, "boto3", no_aws)
    _run(monkeypatch, None)
    turn.run_turn("ceo", f.ingress(eventKind="room_event"))
    assert _reply(posted)["text"] == "ack"


def test_null_reply_thread_root_replies_top_level(home, posted, monkeypatch):
    sent = _run(monkeypatch, None)
    turn.run_turn("ceo", f.ingress(eventKind="room_event", routing={**ROUTING, "placement": "main"},
                                   replyThreadRootId=None))
    assert _reply(posted)["threadRootMessageId"] is None
    assert sent and all("threadRootMessageId" not in p for p in sent)


def test_a_string_reply_thread_root_replies_in_that_thread(home, posted, monkeypatch):
    sent = _run(monkeypatch, None)
    turn.run_turn("ceo", f.ingress(eventKind="room_event", routing=ROUTING, replyThreadRootId=ROOT))
    assert _reply(posted)["threadRootMessageId"] == ROOT
    assert sent and all(p["threadRootMessageId"] == ROOT for p in sent)


def test_the_turn_context_carries_the_placed_thread(home, posted, monkeypatch):
    _run(monkeypatch, None)
    body = f.ingress(eventKind="room_event", routing=ROUTING, replyThreadRootId=ROOT)
    turn.run_turn("ceo", body)
    assert body["_turnContext"]["threadRootMessageId"] == ROOT


def test_absent_reply_thread_root_keeps_the_old_threading(home, posted, monkeypatch):
    _run(monkeypatch, None)
    body = f.ingress()
    turn.run_turn("ceo", body)
    assert _reply(posted)["threadRootMessageId"] == body["message"]["messageId"]


@pytest.mark.parametrize("root", [ROOT, None])
def test_a_dm_follows_the_backend_placement(home, posted, monkeypatch, root):
    # The backend sends a DM's thread root only when the request itself was in that thread.
    _run(monkeypatch, None)
    body = f.ingress(replyThreadRootId=root)
    body["room"]["kind"] = "dm"
    turn.run_turn("ceo", body)
    assert _reply(posted)["threadRootMessageId"] == root


def test_parse_ingress_accepts_routing_and_reply_thread_root():
    for root in (None, ROOT):
        for placement in ("main", "thread"):
            body = f.ingress(routing={"decidedBy": "jev", "placement": placement}, replyThreadRootId=root)
            assert turn.parse_ingress(body)["replyThreadRootId"] == root


@pytest.mark.parametrize("over", [
    {"routing": "jev"},
    {"routing": {"decidedBy": "someone", "placement": "main"}},
    {"routing": {"decidedBy": "jev", "placement": "side"}},
    {"replyThreadRootId": "not-a-uuid"},
    {"replyThreadRootId": 7},
])
def test_parse_ingress_rejects_malformed_routing(over):
    with pytest.raises(turn.IngressError):
        turn.parse_ingress(f.ingress(**over))
