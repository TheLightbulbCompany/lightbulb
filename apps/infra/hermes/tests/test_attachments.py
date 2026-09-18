"""Task 25, bridge half only. The backend GET /internal/messaging/attachments/{...} route
is lane D's; per lane-A-brief.md we stub sync._get_backend_bytes exactly as the brief shows."""
import base64
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from bridge import sync, turn
from tests import factories as f

_RED_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)
_BLUE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYPj/HwADAgH/5ncLrgAAAABJRU5ErkJggg=="
)


def _install_native_vision_stub(monkeypatch):
    """The bridge image omits Hermes itself; mirror the pinned helper contract at that seam."""
    package = types.ModuleType("tools")
    package.__path__ = []
    module = types.ModuleType("tools.vision_tools")

    async def prepare(path, task_id, region, *, validate_decode):
        data = Path(path).read_bytes()
        if validate_decode and not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("source is not a recognized image")
        return SimpleNamespace(path=Path(path), mime="image/png")

    def encode(path, mime_type=None):
        return f"data:{mime_type};base64,{base64.b64encode(Path(path).read_bytes()).decode('ascii')}"

    module._prepare_image = prepare
    module._image_to_base64_data_url = encode
    module._unlink_quietly = lambda _path: None
    monkeypatch.setitem(sys.modules, "tools", package)
    monkeypatch.setitem(sys.modules, "tools.vision_tools", module)


def _capture_agent_call(monkeypatch):
    seen = {}

    class _FakeAgent:
        model = "model"
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **kw):
            seen.update(kw)
            return {"api_calls": 1, "final_response": "ok"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: _FakeAgent())
    return seen


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


def test__run_hermes_sends_a_current_image_as_native_user_content(home, monkeypatch):
    _install_native_vision_stub(monkeypatch)
    seen = _capture_agent_call(monkeypatch)
    monkeypatch.setattr(sync, "_get_backend_bytes", lambda _path: _RED_PNG)
    body = f.ingress(message=f.message(attachments=[
        {"id": "a1", "fileName": "photo.jpg", "contentType": "image/jpeg", "size": len(_RED_PNG)},
    ]))

    turn._run_hermes("ceo", body, None)

    content = seen["user_message"]
    assert content[0] == {"type": "text", "text": "Prez: ship it"}
    assert content[1]["type"] == "image_url"
    header, encoded = content[1]["image_url"]["url"].split(",", 1)
    assert header == "data:image/png;base64"
    assert base64.b64decode(encoded) == _RED_PNG


def test__run_hermes_keeps_current_and_historical_images_on_their_messages(home, monkeypatch):
    _install_native_vision_stub(monkeypatch)
    seen = _capture_agent_call(monkeypatch)

    current = f.message(messageId="current", text="now", attachments=[
        {"id": "current-image", "fileName": "now.png", "contentType": "image/png", "size": len(_RED_PNG)},
    ])
    agent_history = f.message(
        messageId="agent-history", messageSeq=5, author={"kind": "agent", "id": "ceo", "displayName": "CEO"},
        text="earlier",
    )
    human_history = f.message(messageId="human-history", messageSeq=6, text="past", attachments=[
        {"id": "history-image", "fileName": "past.png", "contentType": "image/png", "size": len(_BLUE_PNG)},
    ])
    body = f.ingress(message=current, recentHistory=[agent_history, human_history])

    monkeypatch.setattr(
        sync,
        "_get_backend_bytes",
        lambda path: _RED_PNG if path.endswith("current-image") else _BLUE_PNG,
    )

    turn._run_hermes("ceo", body, None)

    assert seen["conversation_history"][0] == {"role": "assistant", "content": "earlier"}
    historical = seen["conversation_history"][1]["content"]
    assert historical[0] == {"type": "text", "text": "Prez: past"}
    assert base64.b64decode(historical[1]["image_url"]["url"].split(",", 1)[1]) == _BLUE_PNG
    current_content = seen["user_message"]
    assert current_content[0] == {"type": "text", "text": "Prez: now"}
    assert base64.b64decode(current_content[1]["image_url"]["url"].split(",", 1)[1]) == _RED_PNG


def test__run_hermes_does_not_turn_a_pdf_into_image_content(home, monkeypatch):
    _install_native_vision_stub(monkeypatch)
    seen = _capture_agent_call(monkeypatch)
    monkeypatch.setattr(sync, "_get_backend_bytes", lambda _path: b"%PDF-1.4\n")
    body = f.ingress(message=f.message(attachments=[
        {"id": "document", "fileName": "brief.pdf", "contentType": "application/pdf", "size": 9},
    ]))

    turn._run_hermes("ceo", body, None)

    assert seen["user_message"] == "Prez: ship it"


# ---- review fix (IMPORTANT 4): one bad attachment must not fail the whole turn ---------------
def test_a_failed_attachment_fetch_is_skipped_not_fatal(home, monkeypatch):
    def flaky_get(path):
        if "a1" in path:
            raise RuntimeError("network blip")
        return b"bytes"

    monkeypatch.setattr(sync, "_get_backend_bytes", flaky_get)
    body = {
        "room": {"id": "room-1"},
        "message": {"id": "m9", "attachments": [
            {"id": "a1", "fileName": "bad.png", "contentType": "image/png", "size": 5},
            {"id": "a2", "fileName": "ok.pdf", "contentType": "application/pdf", "size": 5},
        ]},
        "recentHistory": [],
    }
    paths = sync.materialize_attachments(body)
    assert [Path(p).name for p in paths] == ["a2.pdf"]  # a1 skipped, a2 still yielded
