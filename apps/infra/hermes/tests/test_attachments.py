"""Task 25, bridge half only. The backend GET /internal/messaging/attachments/{...} route
is lane D's; per lane-A-brief.md we stub sync._get_backend_bytes exactly as the brief shows."""
import json
from pathlib import Path
from bridge import sync, turn
from tests import factories as f


def test_materialize_fetches_only_messages_with_attachments(home, monkeypatch):
    fetched = []

    def fake_get(path):
        fetched.append(path)
        return b"bytes"

    monkeypatch.setattr(sync, "_get_backend_bytes", fake_get)
    body = {
        "room": {"id": "room-1"},
        "message": {"id": "m9", "attachments": [{"id": "a1", "fileName": "x.png", "contentType": "image/png", "size": 5}]},
        "recentHistory": [
            {"messageId": "m1", "text": "no attachment"},
            {"messageId": "m2", "attachments": [{"id": "a2", "fileName": "doc.pdf", "contentType": "application/pdf", "size": 5}]},
        ],
    }
    paths = sync.materialize_attachments(body)
    assert [Path(p).name for p in paths] == ["a1.png", "a2.pdf"]
    # OWNER is "org_test" in this test env (conftest.py), not the brief's literal example.
    assert fetched == ["attachments/org_test/room-1/a1", "attachments/org_test/room-1/a2"]
    assert Path(paths[0]).read_bytes() == b"bytes"
    assert sync.materialize_attachments(body) == paths and len(fetched) == 2  # cached on disk, not refetched


def test_room_context_and_system_prompt_carry_the_media_list():
    ctx = turn.room_context(f.ingress(), "ceo", media=["/mnt/a1.png"])
    assert ctx["media"] == ["/mnt/a1.png"]
    assert turn.room_context(f.ingress(), "ceo")["media"] == []
    block = turn.system_prompt(f.ingress(), "ceo", media=["/mnt/a1.png"]).split("```json", 1)[1].split("```", 1)[0]
    assert json.loads(block)["media"] == ["/mnt/a1.png"]


def test__run_hermes_materializes_attachments_before_building_the_system_prompt(home, monkeypatch):
    seen = {}

    class _FakeAgent:
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **kw):
            seen["system_message"] = kw["system_message"]
            return {"api_calls": 1, "final_response": "ok"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: _FakeAgent())
    monkeypatch.setattr(sync, "materialize_attachments", lambda body: ["/mnt/a1.png"])
    turn._run_hermes("ceo", f.ingress(), None)
    block = seen["system_message"].split("```json", 1)[1].split("```", 1)[0]
    assert json.loads(block)["media"] == ["/mnt/a1.png"]
