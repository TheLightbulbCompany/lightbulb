import pytest
from bridge import turn
from tests import factories as f


def test_a_pdf_attachment_in_history_no_longer_400s_the_whole_ingress():
    hist = f.message(messageSeq=6, attachments=[
        {"id": "33333333-3333-4333-8333-333333333333", "fileName": "deck.pdf",
         "contentType": "application/pdf", "size": 900_000}])
    assert turn.parse_ingress(f.ingress(recentHistory=[hist]))["recentHistory"][0]["attachments"][0]["contentType"] == "application/pdf"


def test_event_id_must_equal_the_message_id():
    with pytest.raises(turn.IngressError, match="eventId"):
        turn.parse_ingress(f.ingress(eventId="44444444-4444-4444-8444-444444444444"))


def test_history_must_be_strictly_increasing_and_below_the_trigger():
    with pytest.raises(turn.IngressError, match="messageSeq"):
        turn.parse_ingress(f.ingress(recentHistory=[f.message(messageSeq=9)]))


def test_target_agent_must_be_a_participant():
    bad = [p for p in f.ingress()["participants"] if p["id"] != "ceo"]
    with pytest.raises(turn.IngressError, match="participant"):
        turn.parse_ingress(f.ingress(participants=bad))


def test_text_over_100k_bytes_is_refused():
    with pytest.raises(turn.IngressError, match="text"):
        turn.parse_ingress(f.ingress(message=f.message(text="x" * 100_001)))


def test_an_unsupported_attachment_type_is_still_refused():
    msg = f.message(attachments=[{"id": f.ROOM, "fileName": "x.exe",
                                  "contentType": "application/octet-stream", "size": 10}])
    with pytest.raises(turn.IngressError, match="contentType"):
        turn.parse_ingress(f.ingress(message=msg))


# ---- fix round (Important 3): attachment cap is per content type, mirroring the backend's
# own table (core/services/messaging.py:51-63) -- a flat 5 MiB cap 400'd a legal 6-20 MiB PDF.
def test_a_6mib_pdf_is_accepted():
    msg = f.message(attachments=[{"id": f.ROOM, "fileName": "deck.pdf",
                                  "contentType": "application/pdf", "size": 6 * 1024 * 1024}])
    assert turn.parse_ingress(f.ingress(message=msg))["message"]["attachments"][0]["size"] == 6 * 1024 * 1024


def test_a_6mib_png_is_still_refused():
    msg = f.message(attachments=[{"id": f.ROOM, "fileName": "photo.png",
                                  "contentType": "image/png", "size": 6 * 1024 * 1024}])
    with pytest.raises(turn.IngressError, match="size exceeds"):
        turn.parse_ingress(f.ingress(message=msg))


def test_a_21mib_pdf_is_refused():
    msg = f.message(attachments=[{"id": f.ROOM, "fileName": "huge.pdf",
                                  "contentType": "application/pdf", "size": 21 * 1024 * 1024}])
    with pytest.raises(turn.IngressError, match="size exceeds"):
        turn.parse_ingress(f.ingress(message=msg))


# ---- a zip (or any file this image can't handle) in history must never block the room ----
def _att(name, content_type, size=10):
    return {"id": "33333333-3333-4333-8333-333333333333", "fileName": name,
            "contentType": content_type, "size": size}


def test_a_zip_on_the_current_message_is_accepted_as_a_file_not_an_image():
    msg = f.message(attachments=[_att("files.zip", "application/zip", 6 * 1024 * 1024)])
    assert turn.parse_ingress(f.ingress(message=msg))["message"]["attachments"][0]["contentType"] == "application/zip"
    assert "application/zip" not in turn.IMAGE_ATTACHMENT_TYPES


def test_an_unhandled_attachment_in_history_is_dropped_and_named_not_refused():
    hist = f.message(messageSeq=6, text="here", attachments=[
        _att("x.exe", "application/octet-stream"), _att("big.png", "image/png", 6 * 1024 * 1024),
        _att("deck.pdf", "application/pdf")])
    entry = turn.parse_ingress(f.ingress(recentHistory=[hist]))["recentHistory"][0]
    assert [a["fileName"] for a in entry["attachments"]] == ["deck.pdf"]
    assert "x.exe" in entry["text"] and "big.png" in entry["text"] and entry["text"].startswith("here")
