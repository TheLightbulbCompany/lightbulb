"""ask_person off a task run: the agent asks in its reply. The task-run path is in test_plugin_ask_tasks.py."""
import pathlib
import sys
import urllib.request

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402


@pytest.fixture
def no_http(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("no HTTP call"))
    monkeypatch.setattr(lightbulb, "_post", lambda *a, **k: pytest.fail("no HTTP call"))


def test_room_turn_ask_person_makes_no_http_call(no_http, monkeypatch):
    """Paperclip exit (task 8b.2): the Paperclip interaction branch is gone, even for a named task."""
    monkeypatch.setattr(turn_context, "read", lambda sid: {"family": "room", "roomId": "r1"})
    out = lightbulb._ask_person({"question": "Which one?", "options": ["A", "B"], "task": "WAL-6", "blocked": True},
                                session_id="s1")
    assert out == lightbulb.ASK_IN_REPLY
    assert "task" not in lightbulb.ASK_PERSON_SCHEMA["parameters"]["properties"]


def test_more_than_four_options_is_refused_before_any_call(no_http):
    out = lightbulb._ask_person({"question": "Which?", "options": ["A", "B", "C", "D", "E"]})
    assert "2 to 4 options" in out


def test_ask_person_is_registered_and_listed_in_the_manifest(monkeypatch):
    registered = {}
    monkeypatch.setattr(lightbulb, "_register_redaction_default", lambda: 0)

    class Registry:
        def register_hook(self, *_a):
            pass

        def register_tool(self, name, toolset, schema, handler, **_kw):
            registered[name] = (schema, handler)

    lightbulb.register(Registry())
    manifest = (pathlib.Path(lightbulb.__file__).parent / "plugin.yaml").read_text()
    assert registered["ask_person"] == (lightbulb.ASK_PERSON_SCHEMA, lightbulb._ask_person)
    assert "  - ask_person\n" in manifest
