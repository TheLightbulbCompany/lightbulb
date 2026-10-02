import json

import bridge
from bridge import turn

EP = "https://api.invalid/api/v1/internal/calls/call-1/frames"


class _Res:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _frames(spec, **kw):
    return turn.call_frame_publisher(spec, "call-1", "agent-1", "run-1", **kw)


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
            assert e["type"] == "call.turn.frame"
            assert (e["payload"]["callId"], e["payload"]["turnId"], e["payload"]["agentId"]) == ("call-1", "run-1", "agent-1")


def test_backend_sink_failure_never_raises(monkeypatch):
    def boom(req, timeout):
        raise OSError("down")
    monkeypatch.setattr(turn.urllib.request, "urlopen", boom)
    pub = _frames({"sink": "backend", "endpoint": EP})
    pub.tool_start("tc-1", "browser", {})
    pub.finished("completed", "hi", None)


def test_no_sink_keeps_the_appsync_publish(monkeypatch):
    calls = []
    monkeypatch.setattr(turn, "publish_frame", lambda e, c, *ev: calls.append((e, c)) or 200)
    pub = _frames({"endpoint": "https://e.invalid", "channel": "/ws/w/users/k"})
    pub.tool_start("tc-1", "browser", {})
    assert calls and calls[-1] == ("https://e.invalid", "/ws/w/users/k")


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
