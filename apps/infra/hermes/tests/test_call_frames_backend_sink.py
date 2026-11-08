import json

import bridge
from bridge import turn

ROOM = "11111111-1111-4111-8111-111111111111"
EP = f"https://api.invalid/api/v1/internal/huddles/org_test/rooms/{ROOM}/frames"
IDENTITY = {"agentId": "agent-1", "roomId": ROOM}


class _Res:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _frames(spec, **kw):
    return turn.call_frame_publisher(spec, IDENTITY, "run-1", **kw)


def test_backend_sink_posts_batches_with_bearer(monkeypatch):
    monkeypatch.setattr(bridge, "HOOK_TOKEN", "hook-late")
    posts = []
    monkeypatch.setattr(turn.urllib.request, "urlopen",
                        lambda req, timeout: posts.append((req, timeout)) or _Res())
    pub = _frames({"sink": "backend", "endpoint": EP})
    for i in range(7):
        pub.tool_start(f"tc-{i}", "browser", {})
    pub.finished("completed", "hi", None)
    assert posts
    for req, timeout in posts:
        assert req.full_url == EP and timeout == turn.FRAME_TIMEOUT_S
        assert req.get_header("Authorization") == "Bearer hook-late"
        events = json.loads(req.data)["events"]
        assert 1 <= len(events) <= turn.FRAME_BATCH
        for e in events:
            assert e["type"] == "voice.run.frame"
            assert (e["payload"]["roomId"], e["payload"]["runId"], e["payload"]["agentId"]) == (ROOM, "run-1", "agent-1")
            assert (e["payload"]["frame"]["runId"], e["payload"]["frame"]["agentId"]) == ("run-1", "agent-1")


def test_backend_sink_failure_never_raises(monkeypatch):
    def boom(req, timeout):
        raise OSError("down")
    monkeypatch.setattr(turn.urllib.request, "urlopen", boom)
    pub = _frames({"sink": "backend", "endpoint": EP})
    pub.tool_start("tc-1", "browser", {})
    pub.finished("completed", "hi", None)


def test_backend_sink_relays_only_frames_the_worker_reads(monkeypatch):
    posts = []
    monkeypatch.setattr(turn.urllib.request, "urlopen", lambda req, timeout: posts.append(req) or _Res())
    pub = _frames({"sink": "backend", "endpoint": EP})
    for _ in range(12):
        pub.thinking("hmm")
    pub.thinking(None)
    assert posts == []  # reasoning deltas never leave the bridge
    pub.finished("completed", "hi", None)
    types = [e["payload"]["frame"]["type"] for req in posts for e in json.loads(req.data)["events"]]
    assert types == ["chunk", "run_ended"]


def test_backend_sink_relays_run_queued(monkeypatch):
    posts = []
    monkeypatch.setattr(turn.urllib.request, "urlopen", lambda req, timeout: posts.append(req) or _Res())
    _frames({"sink": "backend", "endpoint": EP}).emit({"type": "run_queued"})
    (req,) = posts
    assert [e["payload"]["frame"]["type"] for e in json.loads(req.data)["events"]] == ["run_queued"]


class _Accepted(_Res):
    status = 202  # the backend's "accepted, nobody heard it"


def test_backend_sink_tracks_whether_a_worker_heard_the_last_batch(monkeypatch):
    monkeypatch.setattr(bridge, "HOOK_TOKEN", "hook-late")
    monkeypatch.setattr(turn.urllib.request, "urlopen", lambda req, timeout: _Res())
    pub = _frames({"sink": "backend", "endpoint": EP})
    pub.finished("completed", "hi", None)
    assert pub.delivered is True
    monkeypatch.setattr(turn.urllib.request, "urlopen", lambda req, timeout: _Accepted())
    pub = _frames({"sink": "backend", "endpoint": EP})
    pub.finished("completed", "hi", None)
    assert pub.delivered is False


def _quiet_run(monkeypatch, *, delivered: bool, output: str = "Three tasks open; two due today."):
    """run_call_turn with everything but the decision stubbed: Hermes answers `output`, the frames'
    closing batch came back delivered or not."""
    posts = []
    pub = turn.VoiceFramePublisher(EP, ROOM, "agent-1", "run-1", publish=lambda *a: 200 if delivered else 202)
    monkeypatch.setattr(turn, "call_frame_publisher", lambda spec, identity, run_id: pub)
    monkeypatch.setattr(turn, "_run_call", lambda identity, request, frames: ("completed", None, output, dict(turn.ZERO_USAGE)))
    for name in ("finish_run", "report_usage", "save_profile", "restore_profile", "_await_post_turn_writes", "_end_turn"):
        monkeypatch.setattr(turn, name, lambda *a, **k: None)
    monkeypatch.setattr(turn, "_pop_post_turn_agent", lambda: None)
    monkeypatch.setattr(turn, "run_aborted", lambda _r: False)
    monkeypatch.setattr(turn.sync, "restore_root_auth", lambda: None)
    monkeypatch.setattr(turn.sync, "save_root_auth", lambda: None)
    monkeypatch.setattr(turn, "_post_backend", lambda path, body: posts.append((path, body)) or 200)
    identity = {"runId": "run-1", "agentId": "agent-1", "roomId": ROOM, "sessionId": f"agent:agent-1:lightbulb:channel:{ROOM}", "family": "voice", "callId": "ca11ca11-1111-4111-8111-111111111111"}
    request = {"voice": {"utterance": "what's on my board?"}, "frames": {"sink": "backend", "endpoint": EP}}
    turn.register_call_run(identity)  # as server.py does before the turn starts
    turn.run_call_turn(identity, request, reserved=True)
    return posts


def test_a_completed_answer_nobody_heard_is_posted_into_the_room(monkeypatch):
    """10-02: Forge's answer landed 50 s after hang-up and went nowhere. The closing batch's 202 says
    the call is gone; the room gets the answer as the agent's own message, once per run."""
    posts = _quiet_run(monkeypatch, delivered=False)
    assert [p[0] for p in posts] == [f"agent-messages/{bridge.OWNER}"]
    body = posts[0][1]
    assert (body["eventId"], body["roomId"], body["agentId"]) == ("live-late:run-1", ROOM, "agent-1")
    assert body["text"] == 'From the Live call "what\'s on my board?":\n\nThree tasks open; two due today.'


def test_an_answer_the_worker_heard_is_not_posted_twice(monkeypatch):
    assert _quiet_run(monkeypatch, delivered=True) == []
