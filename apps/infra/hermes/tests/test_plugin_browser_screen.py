import base64
import json
import pathlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))

from lightbulb import cloud_screen


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"action": "screenshot"}, {"action": "screenshot"}),
        ({"action": "click", "x": 10, "y": 20}, {"action": "click", "x": 10, "y": 20}),
        ({"action": "type", "text": "hello"}, {"action": "type", "text": "hello"}),
        ({"action": "key", "key": "enter"}, {"action": "key", "key": "enter"}),
        (
            {"action": "scroll", "x": 100, "y": 100, "deltaY": -500},
            {"action": "scroll", "x": 100, "y": 100, "deltaY": -500},
        ),
    ],
)
def test_browser_screen_posts_each_agent_facing_action(monkeypatch, tmp_path, args, expected):
    monkeypatch.setattr(
        cloud_screen.turn_context,
        "read",
        lambda _sid: {"apiUrl": "https://api.example", "ownerId": "org", "hookToken": "token", "runId": "run"},
    )
    monkeypatch.setattr(cloud_screen.turn_context, "_home", lambda: tmp_path)
    monkeypatch.setattr(cloud_screen.browser_control, "current_lease", lambda: {"runId": "run", "generation": "g1"})
    sent = {}

    def post(url, token, body):
        sent.update(url=url, token=token, body=body)
        if body["action"] == "screenshot":
            return {"action": "screenshot", "status": "SUCCESS", "data": base64.b64encode(b"png").decode()}
        return {"action": body["action"], "status": "SUCCESS"}

    monkeypatch.setattr(cloud_screen, "_post", post)
    result = json.loads(cloud_screen.handle(args, session_id="session"))
    assert sent["url"].endswith("/api/v1/internal/browser/org/screen")
    assert sent["token"] == "token"
    assert sent["body"] == {"runId": "run", "generation": "g1", **expected}
    assert result["status"] == "SUCCESS"
    if args["action"] == "screenshot":
        artifact = result["artifacts"][0]
        assert artifact["media"].startswith("MEDIA:")
        assert Path(artifact["path"]).read_bytes() == b"png"


def test_browser_screen_fails_closed_without_the_turn_lease(monkeypatch):
    monkeypatch.setattr(cloud_screen.turn_context, "read", lambda _sid: {"runId": "run"})
    monkeypatch.setattr(cloud_screen.browser_control, "current_lease", lambda: None)
    assert "unavailable" in cloud_screen.handle({"action": "screenshot"}, session_id="session")
