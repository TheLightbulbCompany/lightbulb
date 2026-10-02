"""lightbulb_voice_profile: only inside a voice run, ids from the turn, never from the model."""
import io
import json
import pathlib
import sys
import urllib.error

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

SESSION = "agent:ceo:lightbulb:channel:r"
RUN = "22222222-2222-4222-8222-222222222222"


def _ctx(tmp_path, monkeypatch, **over):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    base = {"apiUrl": "https://api.invalid/", "ownerId": "org_test", "hookToken": "h",
            "agentId": "ceo", "runId": RUN, "roomId": None,
            "voiceRoomId": "11111111-1111-4111-8111-111111111111"}
    base.update(over)
    turn_context.write(tmp_path, SESSION, base)


def _no_post(monkeypatch):
    monkeypatch.setattr(lightbulb, "_post", lambda *a: pytest.fail("no HTTP call"))


def test_refused_outside_a_voice_run(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch, voiceRoomId=None, roomId="11111111-1111-4111-8111-111111111111")
    _no_post(monkeypatch)
    assert lightbulb._voice_profile({"action": "rename", "name": "X"}, session_id=SESSION) == lightbulb.VOICE_ONLY
    assert lightbulb._voice_profile({"action": "rename"}, session_id="none") == lightbulb.VOICE_ONLY


def test_posts_c8_with_the_turns_ids_not_the_models(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: calls.append((url, token, body))
                        or {"ok": True, "agentId": "ceo", "name": "Ada", "notes": ""})
    out = lightbulb._voice_profile({"action": "rename", "name": "Ada", "agentId": "forge",
                                    "runId": "evil"}, session_id=SESSION)
    assert json.loads(out)["name"] == "Ada"
    assert calls == [("https://api.invalid/api/v1/internal/agents/voice-profile/org_test", "h",
                      {"agentId": "ceo", "runId": RUN, "action": "rename", "name": "Ada"})]


def test_an_http_error_is_a_short_failure(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)

    def boom(*_a):
        raise urllib.error.HTTPError("u", 409, "Conflict", {}, io.BytesIO(b'{"detail":"name_taken"}'))

    monkeypatch.setattr(lightbulb, "_post", boom)
    assert lightbulb._voice_profile({"action": "rename", "name": "Ada"}, session_id=SESSION) \
        == "Voice profile update failed: name_taken"


def test_an_http_error_without_a_code_reports_the_status(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)

    def boom(*_a):
        raise urllib.error.HTTPError("u", 502, "Bad Gateway", {}, io.BytesIO(b"<html>"))

    monkeypatch.setattr(lightbulb, "_post", boom)
    assert lightbulb._voice_profile({"action": "add_note", "text": "slower"}, session_id=SESSION) \
        == "Voice profile update failed: 502"


def test_registered(monkeypatch):
    registered = {}
    monkeypatch.setattr(lightbulb, "_register_redaction_default", lambda: 0)

    class Registry:
        def register_hook(self, *_a):
            pass

        def register_tool(self, name, toolset, schema, handler, **_kw):
            registered[name] = (schema, handler)

    lightbulb.register(Registry())
    assert registered["lightbulb_voice_profile"] == (lightbulb.VOICE_PROFILE_SCHEMA, lightbulb._voice_profile)
