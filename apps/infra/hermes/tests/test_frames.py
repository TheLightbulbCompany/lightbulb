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


# ---- fix round (minor 10): AppSync refuses ~230 KiB; skip rather than let it 4xx silently -----
def test_an_oversized_frame_is_skipped_not_sent(monkeypatch):
    called = []
    monkeypatch.setattr(turn.urllib.request, "urlopen", lambda *a, **k: called.append(1))
    event = turn.frame_event(ROOM, "ceo", RUN, None, {"type": "chunk", "content": "x" * 250_000})
    assert turn.publish_frame("https://events.example.invalid/event", f"/ws/{ROOM}/rooms/{ROOM}/k", event) == 0
    assert called == []  # never even reached signing/urlopen


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


# ---- review fix (cheap b): one boto3 Session cached across frame publishes, not one per frame
def test_frame_credentials_reuses_one_cached_boto_session_across_calls(monkeypatch):
    import sys
    import types

    monkeypatch.setattr(turn, "_frame_boto_session", None)
    created = []

    class FakeCreds:
        def get_frozen_credentials(self):
            return turn.Credentials("AK", "SK", None)

    class FakeSession:
        def __init__(self):
            created.append(1)

        def get_credentials(self):
            return FakeCreds()

    # A fake module injected into sys.modules -- not a real `import boto3` -- so this test
    # runs the same whether or not the real boto3 package happens to be installed (it isn't
    # in the lane-common canonical `--with pytest --with botocore` invocation).
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.session = types.SimpleNamespace(Session=FakeSession)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    turn._frame_credentials()
    turn._frame_credentials()
    turn._frame_credentials()
    assert len(created) == 1


# ---- ordering (dev 2026-09-23: a CEO DM stream rendered scrambled) --------------------------
# Every frame carries a per-run, monotonically increasing `seq` so the client can order and
# de-duplicate, and batches leave in seq order even when two threads flush at once.
import threading
import time as _time


def _publisher(sent, delay=0.0):
    def publish(endpoint, channel, *events):
        if delay:
            _time.sleep(delay)
        sent.extend(e["payload"]["frame"] for e in events)

    return turn.FramePublisher("https://events.example.invalid/event", f"/ws/{ROOM}/rooms/{ROOM}/k",
                               ROOM, "ceo", RUN, None, publish=publish)


def test_every_frame_gets_a_monotonic_per_run_seq():
    sent = []
    frames = _publisher(sent)
    frames.started()
    for word in ("a", "b", "c", "d", "e", "f", "g"):
        frames.delta(word)
    frames.delta(None)
    frames.finished("completed", "abcdefg", None)
    seqs = [f["seq"] for f in sent]
    assert seqs == list(range(seqs[0], seqs[0] + len(sent)))


def test_concurrent_flushes_send_every_frame_once_with_its_seq():
    # Arrival order across threads (and across AppSync publishes) is not guaranteed; the client
    # orders by seq. What must hold: each frame goes out exactly once and its seq matches it.
    sent = []
    frames = _publisher(sent, delay=0.01)
    for i in range(40):
        frames.delta(str(i))
    threads = [threading.Thread(target=frames.delta, args=(None,)) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    base = min(f["seq"] for f in sent)
    assert sorted(f["seq"] for f in sent) == list(range(base, base + 40))
    assert all(f["content"] == str(f["seq"] - base) for f in sent)


def test_a_lifecycle_frame_is_numbered_after_the_deltas_it_follows():
    sent = []
    frames = _publisher(sent)
    frames.delta("a")
    frames.delta("b")
    frames.finished("completed", "ab", None)
    assert max(f["seq"] for f in sent if f.get("append")) < min(f["seq"] for f in sent if not f.get("append"))



def test_a_redelivered_attempt_numbers_above_the_dead_one():
    """Same runId, new process: the second attempt's seqs must all exceed the first's, or a client
    that watched the first drops them as duplicates. Also stays a JS-safe integer."""
    sent_first, sent_second = [], []
    _publisher(sent_first).started()
    _publisher(sent_second).started()
    assert min(f["seq"] for f in sent_second) > max(f["seq"] for f in sent_first)
    assert max(f["seq"] for f in sent_second) < 2**53


def test_tool_start_masks_typed_text_and_env_but_keeps_the_action():
    run = turn.display_args("lightbulb_desktop", {"action": "invoke", "command": "system.run",
                                                   "args": {"argv": ["ls"], "cwd": "/", "env": {"TOKEN": "sk-1"}}})
    assert "sk-1" not in str(run) and run["args"]["argv"] == ["ls"]
    assert turn.display_args("browser_type", {"ref": "e3", "text": "hunter2"})["text"] == turn.MASK
    listed = {"action": "file", "fileAction": "dir.list", "args": {"path": "/tmp"}}
    assert turn.display_args("lightbulb_desktop", listed) == listed
