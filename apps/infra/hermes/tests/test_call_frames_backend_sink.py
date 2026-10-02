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
