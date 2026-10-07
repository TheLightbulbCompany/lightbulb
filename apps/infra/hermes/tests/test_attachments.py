"""Task 25, bridge half only. The backend GET /internal/messaging/attachments/{...} route
is lane D's; per lane-A-brief.md we stub sync._get_backend_bytes exactly as the brief shows."""
import base64
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

    async def resize(prepared, scale_info, **kwargs):
        return encode(prepared.path, prepared.mime)

    module._prepare_image = prepare
    module._resize_prepared = resize
    module._EMBED_TARGET_BYTES = 256 * 1024
    module._EMBED_MAX_DIMENSION = 1568
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


def test_xlsx_materializes_with_extension_read_file_uses_for_extraction(home, monkeypatch):
    monkeypatch.setattr(sync, "_get_backend_bytes", lambda _path: b"PK\x03\x04workbook")
    attachment = {
        "id": "sheet-1",
        "fileName": "forecast.xlsx",
        "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "size": 12,
    }
    body = {"room": {"id": "room-1"}, "message": {"attachments": [attachment]}}

    [path] = sync.materialize_attachments(body)

    assert Path(path).name == "sheet-1.xlsx"


def test_room_context_and_system_prompt_carry_the_media_list():
    ctx = turn.room_context(f.ingress(), "ceo", media=["/mnt/a1.png"])
    assert ctx["media"] == ["/mnt/a1.png"]
    assert turn.room_context(f.ingress(), "ceo")["media"] == []
    prompt = turn.system_prompt(f.ingress(), "ceo", media=["/mnt/a1.png"])
    # Bug fix: the model picked a stale historical attachment via vision_analyze even though the
    # current message's own image already rode along natively -- this sentence tells it not to.
    assert "use vision_analyze only for files not shown" in prompt
    # The media list itself is per-turn now, so it rides the turn-context header, not the
    # (byte-stable, cached) system prompt.
    ctx, _ = f.split_turn_input(turn._turn_input(f.ingress(), media=["/mnt/a1.png"]))
    assert ctx["media"] == ["/mnt/a1.png"]


def test__run_hermes_materializes_attachments_before_building_the_system_prompt(home, monkeypatch):
    seen = {}

    class _FakeAgent:
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **kw):
            seen["user_message"] = kw["user_message"]
            return {"api_calls": 1, "final_response": "ok"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: _FakeAgent())
    # A PDF, deliberately -- it is in `media` but never native `_message_image_paths` content,
    # so this test does not also need the native-vision stub just to reach the turn header.
    attachment = {"id": "a1", "fileName": "a1.pdf", "contentType": "application/pdf", "size": 5}
    body = f.ingress(message=f.message(attachments=[attachment]))
    expected_path = str(sync.attachment_path(f.ROOM, attachment))
    monkeypatch.setattr(sync, "materialize_attachments", lambda body: [expected_path])

    turn._run_hermes("ceo", body, None)

    ctx, _ = f.split_turn_input(seen["user_message"])
    assert ctx["media"] == [expected_path]


# ---- fix round: the model answered about the WRONG image -----------------------------------
# `media` used to be sync.materialize_attachments(body) verbatim -- every materialized path,
# history included, with no marker for which belongs to the current message. With the current
# message's image already embedded natively in `user_message`, the model still called
# vision_analyze on a path from this list and picked a STALE historical attachment instead.
def test__run_hermes_lists_only_the_current_messages_media_not_history(home, monkeypatch):
    _install_native_vision_stub(monkeypatch)
    seen = _capture_agent_call(monkeypatch)
    monkeypatch.setattr(sync, "_get_backend_bytes", lambda path: _RED_PNG if "current" in path else _BLUE_PNG)

    current = f.message(messageId="current", text="now", attachments=[
        {"id": "current-image", "fileName": "now.png", "contentType": "image/png", "size": len(_RED_PNG)},
    ])
    history = f.message(messageId="history", messageSeq=5, text="earlier", attachments=[
        {"id": "history-image", "fileName": "past.png", "contentType": "image/png", "size": len(_BLUE_PNG)},
    ])
    body = f.ingress(message=current, recentHistory=[history])

    turn._run_hermes("ceo", body, None)

    ctx, _ = f.split_turn_input(seen["user_message"])
    media = ctx["media"]
    assert len(media) == 1
    assert media[0].endswith("current-image.png")


def test__run_hermes_sends_a_current_image_as_native_user_content(home, monkeypatch):
    _install_native_vision_stub(monkeypatch)
    seen = _capture_agent_call(monkeypatch)
    monkeypatch.setattr(sync, "_get_backend_bytes", lambda _path: _RED_PNG)
    body = f.ingress(message=f.message(attachments=[
        {"id": "a1", "fileName": "photo.jpg", "contentType": "image/jpeg", "size": len(_RED_PNG)},
    ]))

    turn._run_hermes("ceo", body, None)

    content = seen["user_message"]
    assert content[0]["type"] == "text"
    _, text = f.split_turn_input(content)
    assert text == "Prez: ship it"
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
    assert current_content[0]["type"] == "text"
    _, text = f.split_turn_input(current_content)
    assert text == "Prez: now"
    assert base64.b64decode(current_content[1]["image_url"]["url"].split(",", 1)[1]) == _RED_PNG


def test__run_hermes_moves_own_historical_images_to_a_user_provenance_turn(home, monkeypatch):
    _install_native_vision_stub(monkeypatch)
    seen = _capture_agent_call(monkeypatch)
    monkeypatch.setattr(sync, "_get_backend_bytes", lambda _path: _BLUE_PNG)
    body = f.ingress(message=f.message(text="now"), recentHistory=[f.message(
        messageId="agent-history", messageSeq=5,
        author={"kind": "agent", "id": "ceo", "displayName": "CEO"},
        text="earlier", attachments=[
            {"id": "history-image", "fileName": "past.png", "contentType": "image/png", "size": len(_BLUE_PNG)},
        ],
    )])

    turn._run_hermes("ceo", body, None)

    history = seen["conversation_history"]
    assert history[0] == {"role": "assistant", "content": "earlier"}
    assert history[1]["role"] == "user"
    assert history[1]["content"][0] == {
        "type": "text", "text": "[Context: image attachment from assistant CEO message]"
    }
    assert base64.b64decode(history[1]["content"][1]["image_url"]["url"].split(",", 1)[1]) == _BLUE_PNG


def test__run_hermes_does_not_turn_a_pdf_into_image_content(home, monkeypatch):
    _install_native_vision_stub(monkeypatch)
    seen = _capture_agent_call(monkeypatch)
    monkeypatch.setattr(sync, "_get_backend_bytes", lambda _path: b"%PDF-1.4\n")
    body = f.ingress(message=f.message(attachments=[
        {"id": "document", "fileName": "brief.pdf", "contentType": "application/pdf", "size": 9},
    ]))

    turn._run_hermes("ceo", body, None)

    assert isinstance(seen["user_message"], str)  # no image content list for a PDF
    _, text = f.split_turn_input(seen["user_message"])
    assert text == "Prez: ship it"


def test_native_image_content_uses_the_resized_bytes_and_cleans_up(home, monkeypatch):
    _install_native_vision_stub(monkeypatch)
    source = home / "large.png"
    source.write_bytes(_RED_PNG)
    native = sys.modules["tools.vision_tools"]
    cleaned = []

    async def resize(prepared, scale_info, **kwargs):
        assert kwargs == {"max_base64_bytes": 256 * 1024, "max_dimension": 1568, "force_jpeg": True}
        return "data:image/png;base64," + base64.b64encode(_BLUE_PNG).decode("ascii")

    monkeypatch.setattr(native, "_resize_prepared", resize)
    monkeypatch.setattr(native, "_unlink_quietly", cleaned.append)
    part = turn._native_image_part(str(source))
    assert base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) == _BLUE_PNG
    assert cleaned == [source]


def test_image_budget_keeps_current_then_newest_history_without_dropping_text(home, monkeypatch):
    _install_native_vision_stub(monkeypatch)
    seen = _capture_agent_call(monkeypatch)
    monkeypatch.setattr(sync, "_get_backend_bytes", lambda _path: _RED_PNG)

    def message(seq):
        return f.message(messageId=f"m{seq}", messageSeq=seq, text=f"text {seq}", attachments=[
            {"id": f"a{seq}-{i}", "fileName": "image.png", "contentType": "image/png", "size": len(_RED_PNG)}
            for i in range(4)
        ])

    body = f.ingress(message=message(7), recentHistory=[message(seq) for seq in range(1, 7)])
    turn._run_hermes("ceo", body, None)
    history = seen["conversation_history"]
    assert len(history) == 6
    assert all(row["role"] == "user" for row in history)
    assert history[:2] == [{"role": "user", "content": f"Prez: text {seq}"} for seq in (1, 2)]
    for seq, row in enumerate(history[2:], 3):
        assert row["content"][0] == {"type": "text", "text": f"Prez: text {seq}"}
        assert len(row["content"][1:]) == 4
    assert seen["user_message"][0]["type"] == "text"
    _, text = f.split_turn_input(seen["user_message"])
    assert text == "Prez: text 7"
    assert len(seen["user_message"][1:]) == 4
    rows = [*history, {"content": seen["user_message"]}]
    assert sum(part["type"] == "image_url" for row in rows if isinstance(row["content"], list)
               for part in row["content"]) == 20


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
