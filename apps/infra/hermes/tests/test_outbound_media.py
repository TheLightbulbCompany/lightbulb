"""Task 9 / design §2i: a `MEDIA:<path>` line in the reply becomes a real room attachment.

Observed in the Task 1 spike: the CEO's reply was `...Screenshot:\n\nMEDIA:/opt/data/profiles/
<agent>/cache/screenshots/browser_screenshot_<hex>.png` and the room message carried
`attachments: null` -- the human saw a filesystem path.
"""
import base64

import pytest

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


def _names(attachments):
    return [a["fileName"] for a in attachments]


def test_media_lines_become_attachments_and_leave_the_text(tmp_path):
    one, two = _shot(tmp_path, "one.png"), _shot(tmp_path, "two.png")
    text, attachments = turn.extract_media(f"Here it is.\nMEDIA:{one}\nMEDIA:{two}\n", tmp_path)
    assert text == "Here it is."  # the path never reaches the room as prose
    assert _names(attachments) == ["one.png", "two.png"]
    assert attachments[0] == {
        "fileName": "one.png",
        "contentType": "image/png",
        "dataBase64": base64.b64encode(_PNG).decode(),
    }


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
    text, attachments = turn.extract_media("\n".join(f"MEDIA:{s}" for s in shots), tmp_path)
    assert _names(attachments) == [s.name for s in shots[: turn.MAX_ATTACHMENTS]]
    assert text == f"MEDIA:{shots[-1]}"  # dropped from the upload, kept in the text


def test_media_over_the_per_type_byte_cap_is_left_in_the_text(tmp_path):
    big = _shot(tmp_path, "big.png", _PNG + b"0" * turn.MAX_ATTACHMENT_BYTES)
    assert turn.extract_media(f"MEDIA:{big}", tmp_path) == (f"MEDIA:{big}", [])


def test_media_past_the_outbound_budget_is_left_in_the_text(tmp_path, monkeypatch):
    """The reply POST crosses API Gateway's 10 MB body cap, so the batch is bounded by what the
    TRANSPORT carries, not by what storage accepts: over it the whole POST 413s and every
    attachment in the reply is lost, the screenshot beside the big file included."""
    monkeypatch.setattr(turn, "OUTBOUND_MEDIA_MAX_BYTES", len(_PNG) + 4)
    first, second = _shot(tmp_path, "first.png"), _shot(tmp_path, "second.png")

    text, attachments = turn.extract_media(f"MEDIA:{first}\nMEDIA:{second}", tmp_path)

    assert _names(attachments) == ["first.png"]
    assert text == f"MEDIA:{second}"


def test_the_budget_stays_under_the_gateways_body_cap():
    """Base64 is x4/3; API Gateway caps the body at 10 MB. If this ever stops holding, every
    reply at the budget 413s instead of arriving."""
    assert turn.OUTBOUND_MEDIA_MAX_BYTES * 4 / 3 < 10_000_000


def test_bytes_that_do_not_match_the_extension_are_left_in_the_text(tmp_path):
    """The backend re-checks the signature and rejects the WHOLE request on a mismatch, which
    would take every other attachment in the reply down with it."""
    fake = _shot(tmp_path, "creds.png", b'{"token": "not a png"}')
    real = _shot(tmp_path, "real.png")

    text, attachments = turn.extract_media(f"MEDIA:{fake}\nMEDIA:{real}", tmp_path)

    assert _names(attachments) == ["real.png"]
    assert text == f"MEDIA:{fake}"


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


def _replies_when_attachments_get(status: int, home, monkeypatch) -> list[dict]:
    shot = _shot(_profile_dir("ceo") / "cache", "shot.png")
    reply_text = f"Here.\nMEDIA:{shot}"
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, reply_text, turn.ZERO_USAGE))
    posts = []

    def _post(path, body):
        posts.append((path, body))
        return status if "attachments" in body else 200

    monkeypatch.setattr(turn, "_post_backend", _post)
    turn.run_turn("ceo", f.ingress())
    return [b for path, b in posts if path.startswith("agent-messages/")]


@pytest.mark.parametrize("status", turn.REPLY_TEXT_FALLBACK_STATUSES)
def test_a_backend_that_rejects_the_attachment_body_still_gets_the_reply(status, home, monkeypatch):
    """400 is a backend predating the field (`extra="forbid"` rejects the whole body), 413 is
    over the gateway's body cap, and 409 is the recovery case -- an earlier attempt already
    stored the full text, so the re-post replays it. Losing a screenshot beats losing the
    reply, and it removes the deploy ordering."""
    replies = _replies_when_attachments_get(status, home, monkeypatch)

    assert len(replies) == 2
    assert replies[1]["text"] == f"Here.\nMEDIA:{_profile_dir('ceo') / 'cache' / 'shot.png'}"
    assert "attachments" not in replies[1]
    assert replies[0]["eventId"] == replies[1]["eventId"]  # same idempotency key: one message, not two


@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_a_status_a_re_post_cannot_recover_from_is_not_retried(status, home, monkeypatch):
    """A second identical POST fails identically (401/403/404), and re-posting into a rate
    limit is the opposite of the instruction (429)."""
    assert len(_replies_when_attachments_get(status, home, monkeypatch)) == 1
