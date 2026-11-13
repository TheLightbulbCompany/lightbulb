"""Cost caps: a phone call ends after 20 minutes, a meeting after 90; both env-overridable, both say goodbye first."""

import importlib
import os

os.environ.setdefault("LIGHTBULB_API_URL", "http://x")
os.environ.setdefault("LIGHTBULB_HUDDLE_SECRET", "x")

from huddle_agent import meet, phone  # noqa: E402


def test_defaults_and_env_overrides(monkeypatch):
    assert phone.CALL_MAX_S == 20 * 60 and meet.MEETING_MAX_S == 90 * 60
    monkeypatch.setenv("PHONE_MAX_S", "5")
    monkeypatch.setenv("MEET_MAX_S", "7")
    try:
        assert importlib.reload(phone).CALL_MAX_S == 5
        assert importlib.reload(meet).MEETING_MAX_S == 7
    finally:
        monkeypatch.undo()
        importlib.reload(phone), importlib.reload(meet)
