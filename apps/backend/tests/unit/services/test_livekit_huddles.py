import json
from types import SimpleNamespace

import jwt
import pytest
from livekit import api

from core.services import livekit_huddles as lh


class FakeAPI:
    def __init__(self, identities=None, list_error=None):
        self.calls = []
        self.identities = identities or []
        self.list_error = list_error
        self.room = SimpleNamespace(list_participants=self._list, send_data=self._send)
        self.agent_dispatch = SimpleNamespace(create_dispatch=self._dispatch)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _list(self, req):
        if self.list_error:
            raise self.list_error
        self.calls.append(("list", req))
        return SimpleNamespace(participants=[SimpleNamespace(identity=i) for i in self.identities])

    async def _send(self, req):
        self.calls.append(("send", req))

    async def _dispatch(self, req):
        self.calls.append(("dispatch", req))


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    for k, v in dict(
        LIVEKIT_URL="wss://lk", LIVEKIT_API_KEY="key", LIVEKIT_API_SECRET="s" * 40, ENVIRONMENT="dev"
    ).items():
        monkeypatch.setattr(lh.settings, k, v)


def fake(monkeypatch, **kw):
    f = FakeAPI(**kw)
    monkeypatch.setattr(lh, "_api", lambda: f)
    return f


def test_mint_token_claims():
    c = jwt.decode(lh.mint_token(room_id="r1", user_id="u1", name="Ann"), options={"verify_signature": False})
    assert c["sub"] == "u1" and c["name"] == "Ann"
    assert c["video"]["room"] == "huddle-dev-r1"
    assert c["video"]["roomJoin"] is True and c["video"]["canPublishData"] is True
    assert c["exp"] - c["nbf"] <= 900 + 60


async def test_has_ceo_and_participants(monkeypatch):
    fake(monkeypatch, identities=["u1", "agent-ceo"])
    assert await lh.has_ceo("r1") is True
    assert await lh.participants("r1") == ["u1"]
    fake(monkeypatch, identities=["u1"])
    assert await lh.has_ceo("r1") is False


async def test_missing_room_is_empty(monkeypatch):
    fake(monkeypatch, list_error=api.TwirpError("not_found", "no room", status=404))
    assert await lh.has_ceo("r1") is False
    assert await lh.participants("r1") == []


async def test_dispatch_ceo(monkeypatch):
    f = fake(monkeypatch)
    await lh.dispatch_ceo("r1", {"a": 1})
    _, req = f.calls[0]
    assert req.agent_name == "lightbulb-huddle" and req.room == "huddle-dev-r1"
    assert json.loads(req.metadata) == {"a": 1}


async def test_send_frames(monkeypatch):
    f = fake(monkeypatch)
    await lh.send_frames("r1", [{"t": 1}])
    _, req = f.calls[0]
    assert req.topic == "call.frame" and list(req.destination_identities) == ["agent-ceo"]
    assert json.loads(req.data) == {"events": [{"t": 1}]}


@pytest.mark.parametrize("field", ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"])
def test_configured_false_when_any_empty(monkeypatch, field):
    assert lh.configured() is True
    monkeypatch.setattr(lh.settings, field, "")
    assert lh.configured() is False


async def test_send_frames_packs_into_bounded_packets_in_order(monkeypatch):
    f = fake(monkeypatch)
    events = [{"seq": i, "text": "x" * 3_000} for i in range(10)]
    await lh.send_frames("r1", events)
    sent = [req.data for _, req in f.calls]
    assert len(sent) > 1 and all(len(d) <= lh.MAX_PACKET_BYTES for d in sent)
    assert [e for d in sent for e in json.loads(d)["events"]] == events
    # Greedy: every packet but the last is too full to take the next event.
    assert all(len(d) + 3_020 > lh.MAX_PACKET_BYTES for d in sent[:-1])


async def test_send_frames_drops_an_oversize_event(monkeypatch):
    f = fake(monkeypatch)
    metrics = []
    monkeypatch.setattr(lh, "put_metric", lambda name, **kw: metrics.append(name))
    await lh.send_frames("r1", [{"seq": 1}, {"seq": 2, "text": "x" * 14_000}, {"seq": 3}])
    assert [json.loads(req.data) for _, req in f.calls] == [{"events": [{"seq": 1}, {"seq": 3}]}]
    assert metrics == ["huddle.frame.oversize"]


async def test_send_frames_sends_nothing_when_all_dropped(monkeypatch):
    f = fake(monkeypatch)
    monkeypatch.setattr(lh, "put_metric", lambda name, **kw: None)
    await lh.send_frames("r1", [{"text": "x" * 20_000}])
    assert f.calls == []
