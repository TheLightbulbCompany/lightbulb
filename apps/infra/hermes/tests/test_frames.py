import json
from bridge import turn

ROOM = "11111111-1111-4111-8111-111111111111"
RUN = "22222222-2222-4222-8222-222222222222"
ROOT = "55555555-5555-4555-8555-555555555555"


def test_every_frame_is_stamped_with_run_agent_and_thread_root():
    event = turn.frame_event(ROOM, "ceo", RUN, ROOT, {"type": "tool_start", "tool": "terminal"})
    assert event["type"] == "room.run.frame" and event["schemaVersion"] == 1 and event["roomId"] == ROOM
    assert event["payload"]["frame"] == {"type": "tool_start", "tool": "terminal", "runId": RUN, "agentId": "ceo"}
    assert event["payload"]["threadRootMessageId"] == ROOT


def test_a_dm_frame_omits_the_thread_root_key_entirely():
    assert "threadRootMessageId" not in turn.frame_event(ROOM, "ceo", RUN, None, {"type": "done"})["payload"]


def test_the_publish_body_double_encodes_the_event(monkeypatch):
    seen = {}

    class FakeResponse:
        status = 200
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data)
        seen["auth"] = req.headers.get("Authorization", "")
        return FakeResponse()

    monkeypatch.setattr(turn.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(turn, "_frame_credentials", lambda: turn.Credentials("AK", "SK", "TOK"))
    event = turn.frame_event(ROOM, "ceo", RUN, None, {"type": "run_active", "active": True})
    assert turn.publish_frame("https://events.example.invalid/event", f"/ws/{ROOM}/rooms/{ROOM}/k", event) == 200
    assert seen["body"]["channel"] == f"/ws/{ROOM}/rooms/{ROOM}/k"
    assert json.loads(seen["body"]["events"][0])["payload"]["frame"]["type"] == "run_active"
    assert seen["auth"].startswith("AWS4-HMAC-SHA256 Credential=AK/")
