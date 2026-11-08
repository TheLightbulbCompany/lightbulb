"""A Worker run casts its browser tab to its task's origin room (identity.frames), and nowhere without one."""
from bridge import turn


def test_task_run_casts_to_the_rooms_channel(monkeypatch):
    sent = []
    monkeypatch.setattr(turn, "publish_frame", lambda endpoint, channel, *events: sent.append((endpoint, channel, events)) or 1)
    identity = {"runId": "run-1", "agentId": "agent-1",
                "frames": {"endpoint": "https://events", "channel": "/ws/w/rooms/r1/dk", "roomId": "r1"}}
    sink = turn._task_frame_sink(identity)
    sink({"seq": 1, "jpegBase64": "AA==", "w": 10, "h": 10})
    ((endpoint, channel, (event,)),) = sent
    assert (endpoint, channel, event["type"]) == ("https://events", "/ws/w/rooms/r1/dk", "room.browser.frame")
    assert event["payload"] == {"roomId": "r1", "runId": "run-1", "agentId": "agent-1", "seq": 1, "jpegBase64": "AA==", "w": 10, "h": 10}


def test_no_frames_target_no_cast():
    assert turn._task_frame_sink({"runId": "run-1", "agentId": "agent-1"}) is None
    assert turn._task_frame_sink({"runId": "run-1", "agentId": "agent-1", "frames": {"endpoint": "", "channel": "c", "roomId": "r"}}) is None
