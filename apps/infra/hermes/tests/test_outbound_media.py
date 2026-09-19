"""Task 9 / design §2i: a `MEDIA:<path>` line in the reply becomes a real room attachment.

Observed in the Task 1 spike: the CEO's reply was `...Screenshot:\n\nMEDIA:/opt/data/profiles/
<agent>/cache/screenshots/browser_screenshot_<hex>.png` and the room message carried
`attachments: null` -- the human saw a filesystem path.
"""
import base64

from bridge import turn
from bridge.sync import _profile_dir
from tests import factories as f

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


def _shot(root, name="shot.png", data=_PNG):
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(data)
    return path


def test_media_lines_become_attachments_and_leave_the_text(tmp_path):
    one, two = _shot(tmp_path, "one.png"), _shot(tmp_path, "two.png")
    text, paths = turn.extract_media(f"Here it is.\nMEDIA:{one}\nMEDIA:{two}\n", tmp_path)
    assert text == "Here it is."  # the path never reaches the room as prose
    assert paths == [one, two]


def test_a_path_outside_the_profile_is_never_read(tmp_path):
    outside = _shot(tmp_path / "elsewhere")
    profile = tmp_path / "profiles" / "ceo"
    profile.mkdir(parents=True)
    reply = f"see\nMEDIA:{outside}"
    assert turn.extract_media(reply, profile) == (reply, [])


def test_a_symlink_out_of_the_profile_is_never_read(tmp_path):
    outside = _shot(tmp_path / "elsewhere")
    profile = tmp_path / "profiles" / "ceo"
    profile.mkdir(parents=True)
    link = profile / "shot.png"
    link.symlink_to(outside)
    assert turn.extract_media(f"MEDIA:{link}", profile) == (f"MEDIA:{link}", [])


def test_over_cap_media_is_left_in_the_text(tmp_path):
    shots = [_shot(tmp_path, f"{i}.png") for i in range(turn.MAX_ATTACHMENTS + 1)]
    text, paths = turn.extract_media("\n".join(f"MEDIA:{s}" for s in shots), tmp_path)
    assert paths == shots[: turn.MAX_ATTACHMENTS]
    assert text == f"MEDIA:{shots[-1]}"  # dropped from the upload, kept in the text


def test_media_over_the_per_type_byte_cap_is_left_in_the_text(tmp_path):
    big = _shot(tmp_path, "big.png", _PNG + b"0" * turn.MAX_ATTACHMENT_BYTES)
    assert turn.extract_media(f"MEDIA:{big}", tmp_path) == (f"MEDIA:{big}", [])


def test_a_missing_media_path_is_ignored(tmp_path):
    reply = f"MEDIA:{tmp_path / 'gone.png'}"
    assert turn.extract_media(reply, tmp_path) == (reply, [])


def test_a_type_the_room_does_not_accept_is_ignored(tmp_path):
    script = _shot(tmp_path, "run.sh", b"#!/bin/sh\n")
    reply = f"MEDIA:{script}"
    assert turn.extract_media(reply, tmp_path) == (reply, [])


def test_a_reply_with_no_media_is_untouched(tmp_path):
    assert turn.extract_media("just words", tmp_path) == ("just words", [])


def test_the_reply_post_carries_the_screenshot_inline(home, posted, monkeypatch):
    shot = _shot(_profile_dir("ceo") / "cache" / "screenshots", "browser_screenshot_ab.png")
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, f"Screenshot:\n\nMEDIA:{shot}", turn.ZERO_USAGE))

    turn.run_turn("ceo", f.ingress())

    reply = next(b for path, b in posted if path.startswith("agent-messages/"))
    assert reply["text"] == "Screenshot:"
    assert reply["attachments"] == [
        {
            "fileName": "browser_screenshot_ab.png",
            "contentType": "image/png",
            "dataBase64": base64.b64encode(_PNG).decode(),
        }
    ]


def test_a_reply_without_media_posts_no_attachments_key(home, posted, monkeypatch):
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, "ack", turn.ZERO_USAGE))
    turn.run_turn("ceo", f.ingress())
    reply = next(b for path, b in posted if path.startswith("agent-messages/"))
    assert "attachments" not in reply and reply["text"] == "ack"


def test_a_backend_that_rejects_the_attachment_body_still_gets_the_reply(home, monkeypatch):
    """The backend is `extra="forbid"`, so one that predates the field 400s the whole reply.
    Losing a screenshot beats losing the reply -- and it removes the deploy ordering."""
    shot = _shot(_profile_dir("ceo") / "cache", "shot.png")
    reply_text = f"Here.\nMEDIA:{shot}"
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, reply_text, turn.ZERO_USAGE))
    posts = []

    def _post(path, body):
        posts.append((path, body))
        return 400 if "attachments" in body else 200

    monkeypatch.setattr(turn, "_post_backend", _post)

    turn.run_turn("ceo", f.ingress())

    replies = [b for path, b in posts if path.startswith("agent-messages/")]
    assert len(replies) == 2
    assert replies[1]["text"] == reply_text and "attachments" not in replies[1]
    assert replies[0]["eventId"] == replies[1]["eventId"]  # same idempotency key: one message, not two
