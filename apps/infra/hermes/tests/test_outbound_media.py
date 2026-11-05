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
    text, attachments, _ = turn.extract_media(f"Here it is.\nMEDIA:{one}\nMEDIA:{two}\n", tmp_path)
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
    text, attachments, uploads = turn.extract_media(f"see\nMEDIA:{outside}", profile)
    assert attachments == uploads == []
    assert "MEDIA:" not in text and "Couldn't attach shot.png" in text
    assert "lost when the workspace restarts" in text  # the human learns the file will not survive


def test_a_symlink_out_of_the_profile_is_never_read(tmp_path):
    outside = _shot(tmp_path / "elsewhere")
    profile = tmp_path / "profiles" / "ceo"
    profile.mkdir(parents=True)
    link = profile / "shot.png"
    link.symlink_to(outside)
    text, attachments, _ = turn.extract_media(f"MEDIA:{link}", profile)
    assert attachments == [] and text.startswith("_(Couldn't attach shot.png")


_XLSX = b"PK\x03\x04" + b"\x00" * 10


def test_a_file_in_the_outbox_goes_up(tmp_path):
    # Observed on dev 2026-09-24: `MEDIA:/opt/data/sales.xlsx` was dropped (path) -- only the
    # profile dir counted. The outbox is the one HOME folder an agent can send from.
    profile = tmp_path / "profiles" / "ceo"
    profile.mkdir(parents=True)
    sheet = _shot(tmp_path / "outbox", "sales.xlsx", _XLSX)
    nested = _shot(tmp_path / "outbox" / "q3", "chart.png")
    text, attachments, _ = turn.extract_media(f"Here.\nMEDIA:{sheet}\nMEDIA:{nested}", profile, tmp_path)
    assert text == "Here."
    assert _names(attachments) == ["sales.xlsx", "chart.png"]
    # HERMES_HOME is never saved to S3: the file is kept in the profile's own outbox too.
    assert (profile / "outbox" / "sales.xlsx").read_bytes() == _XLSX


@pytest.mark.parametrize("rel", [
    "sales.xlsx",                                   # loose in HOME: shared scratch between agents
    "media/messaging/attachments/dm-room/a.png",    # another room's cached attachment
    "profiles/cfo/books.xlsx",                      # a sibling agent's profile
])
def test_nothing_else_under_home_is_read(tmp_path, rel):
    profile = tmp_path / "profiles" / "ceo"
    profile.mkdir(parents=True)
    target = tmp_path / rel
    _shot(target.parent, target.name, _XLSX if rel.endswith(".xlsx") else _PNG)
    link = _shot(tmp_path / "outbox", "x.txt", b"")  # a symlink out of the outbox is refused too
    link.unlink()
    link.symlink_to(target)
    text, attachments, uploads = turn.extract_media(f"MEDIA:{target}\nMEDIA:{link}", profile, tmp_path)
    assert attachments == uploads == [] and "MEDIA:" not in text and text.count("Couldn't attach") == 2


@pytest.mark.parametrize("where", ["outbox", "profile"])
def test_dot_paths_are_never_read(tmp_path, where):
    profile = tmp_path / "profiles" / "ceo"
    profile.mkdir(parents=True)
    base = tmp_path / "outbox" if where == "outbox" else profile
    hidden = [_shot(base, ".secrets.txt", b"token=abc"), _shot(base / ".hermes", "notes.md", b"# keys")]
    text, attachments, _ = turn.extract_media("\n".join(f"MEDIA:{p}" for p in hidden), profile, tmp_path)
    assert attachments == [] and "MEDIA:" not in text


def test_over_cap_media_is_left_in_the_text(tmp_path):
    shots = [_shot(tmp_path, f"{i}.png") for i in range(turn.MAX_ATTACHMENTS + 1)]
    text, attachments, _ = turn.extract_media("\n".join(f"MEDIA:{s}" for s in shots), tmp_path)
    assert _names(attachments) == [s.name for s in shots[: turn.MAX_ATTACHMENTS]]
    assert text.startswith(f"_(Couldn't attach {shots[-1].name}: a message carries at most 4 files")


def test_media_over_the_per_type_byte_cap_is_left_in_the_text(tmp_path):
    big = _shot(tmp_path, "big.png", _PNG + b"0" * turn.MAX_ATTACHMENT_BYTES)
    text, attachments, uploads = turn.extract_media(f"MEDIA:{big}", tmp_path)
    assert attachments == uploads == []
    assert text == "_(Couldn't attach big.png: it's 5.0 MB, over the 5.0 MB limit; it's saved in the agent's workspace at `big.png`.)_"


def test_media_past_the_outbound_budget_is_left_in_the_text(tmp_path, monkeypatch):
    """The reply POST crosses API Gateway's 10 MB body cap, so the batch is bounded by what the
    TRANSPORT carries, not by what storage accepts: over it the whole POST 413s and every
    attachment in the reply is lost, the screenshot beside the big file included."""
    monkeypatch.setattr(turn, "OUTBOUND_MEDIA_MAX_BYTES", len(_PNG) + 4)
    first, second = _shot(tmp_path, "first.png"), _shot(tmp_path, "second.png")
    notes = _shot(tmp_path, "notes.txt", b"x" * 64)

    text, attachments, uploads = turn.extract_media(f"MEDIA:{first}\nMEDIA:{second}\nMEDIA:{notes}", tmp_path)

    assert _names(attachments) == ["first.png"]
    # Past the budget a signed file goes to S3 directly instead of being dropped; text cannot
    # (the backend sniffs an upload from its head, and text has no head to sniff).
    assert [u["fileName"] for u in uploads] == ["second.png"]
    assert text.startswith("_(Couldn't attach notes.txt")


def test_the_budget_stays_under_the_gateways_body_cap():
    """Base64 is x4/3; API Gateway caps the body at 10 MB. If this ever stops holding, every
    reply at the budget 413s instead of arriving."""
    assert turn.OUTBOUND_MEDIA_MAX_BYTES * 4 / 3 < 10_000_000


@pytest.mark.parametrize(
    "name,data",
    [
        ("creds.png", b'{"token": "not a png"}'),  # nothing like the type it claims
        ("half.png", b"\x89PNGstill not one"),  # the right four bytes, the wrong eight
        ("sound.webp", b"RIFF\x00\x00\x00\x00WAVEfmt "),  # RIFF, but a WAV: no WEBP at offset 8
        ("short.webp", b"RIFF"),  # RIFF and nothing after it
        ("odd.gif", b"GIF8zzzzz"),  # GIF8, but neither 87a nor 89a
        ("fake.xlsx", b"not a zip container at all"),  # claims OOXML, isn't even a zip
        ("bad.txt", b"\xff\xfe not valid utf-8 \x80\x81"),  # claims text, isn't valid UTF-8
    ],
)
def test_bytes_that_do_not_match_the_extension_are_left_in_the_text(tmp_path, name, data):
    """The backend re-checks the signature and rejects the WHOLE request on a mismatch, which
    would take every other attachment in the reply down with it -- so this sniff is the
    backend's `_ATTACHMENT_SIGNATURES`, byte for byte. Anything looser here only moves the
    rejection to the one place that costs the whole reply."""
    fake = _shot(tmp_path, name, data)
    real = _shot(tmp_path, "real.png")

    text, attachments, _ = turn.extract_media(f"MEDIA:{fake}\nMEDIA:{real}", tmp_path)

    assert _names(attachments) == ["real.png"]
    assert text.startswith(f"_(Couldn't attach {name}: its contents aren't a real")


@pytest.mark.parametrize(
    "name,data",
    [
        ("real.gif", b"GIF89a" + b"\x00" * 10),
        ("old.gif", b"GIF87a" + b"\x00" * 10),
        ("real.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "),
        ("real.pdf", b"%PDF-1.7\ncontent"),
        ("real.jpg", b"\xff\xd8\xff\xe0 jfif"),
        ("real.xlsx", b"PK\x03\x04" + b"\x00" * 10),  # OOXML is a zip container
        ("real.docx", b"PK\x03\x04" + b"\x00" * 10),
        ("real.pptx", b"PK\x03\x04" + b"\x00" * 10),
        ("real.csv", "name,total\nfoo,1 — 日本語\n".encode()),
        ("real.txt", b"plain text, nothing fancy"),
        ("real.md", "# heading\n\nsome *markdown*".encode()),
    ],
)
def test_every_accepted_type_still_goes_up(tmp_path, name, data):
    """The other half of the tightened sniff: a genuine file of each type is not over-rejected."""
    path = _shot(tmp_path, name, data)
    text, attachments, _ = turn.extract_media(f"MEDIA:{path}", tmp_path)
    assert _names(attachments) == [name] and text == ""


def test_a_missing_media_path_is_ignored(tmp_path):
    assert turn.extract_media(f"MEDIA:{tmp_path / 'gone.png'}", tmp_path) == (
        "_(Couldn't attach gone.png: the file doesn't exist.)_", [], [])


def test_a_type_the_room_does_not_accept_is_ignored(tmp_path):
    script = _shot(tmp_path, "run.sh", b"#!/bin/sh\n")
    text, attachments, uploads = turn.extract_media(f"MEDIA:{script}", tmp_path)
    assert attachments == uploads == [] and text.startswith("_(Couldn't attach run.sh: .sh files can't be attached")


def test_a_reply_with_no_media_is_untouched(tmp_path):
    assert turn.extract_media("just words", tmp_path) == ("just words", [], [])


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
    assert replies[1]["text"] == "Here.\n_(Couldn't attach shot.png: the room refused the files.)_"
    assert "attachments" not in replies[1]
    assert replies[0]["eventId"] == replies[1]["eventId"]  # same idempotency key: one message, not two


@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_a_status_a_re_post_cannot_recover_from_is_not_retried(status, home, monkeypatch):
    """A second identical POST fails identically (401/403/404), and re-posting into a rate
    limit is the opposite of the instruction (429)."""
    assert len(_replies_when_attachments_get(status, home, monkeypatch)) == 1


def test_a_room_turn_is_told_where_a_sendable_file_goes():
    # Asked plainly on dev, an agent wrote the file loose in HOME -- refused by design -- because
    # nothing it reads by default said where attachable files go.
    prompt = turn.system_prompt(f.ingress(), "ceo")
    assert "save it inside your profile folder" in prompt and "mp4/webm/mov" in prompt
    assert str(turn.HOME / "outbox") not in prompt  # HERMES_HOME/outbox is never saved to S3
    assert "`MEDIA:<its absolute path>`" in prompt
    mail = turn.system_prompt(f.ingress(eventKind="agent_mail", targetAgentId="ceo"), "ceo")
    assert "MEDIA:" not in mail  # no room to attach to


@pytest.mark.parametrize("kind", ["agent_mail", "paperclip_run"])
def test_mail_and_paperclip_event_prompts_carry_the_repo_rule(kind):
    # Rooms and native Paperclip runs already did; these two shared the ~4 GB disk blind.
    prompt = turn.system_prompt(f.ingress(eventKind=kind, targetAgentId="ceo"), "ceo")
    assert turn._repo_rule("ceo") in prompt and "push the branch" in prompt


# ---- video (2026-09-26 prod: Friday's Remotion mp4 arrived as a raw `MEDIA:` line) -----------
_MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64


@pytest.mark.parametrize("name,data", [
    ("hero.mp4", _MP4), ("clip.mov", b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 8),
    ("clip.webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 12),
])
def test_a_video_is_uploaded_never_inlined(tmp_path, name, data):
    path = _shot(tmp_path, name, data)
    text, attachments, uploads = turn.extract_media(f"Rendered.\nMEDIA:{path}", tmp_path)
    assert text == "Rendered." and attachments == []
    assert [(u["fileName"], u["upload"]) for u in uploads] == [(name, path.resolve())]


def test_a_fake_video_gets_a_note(tmp_path):
    path = _shot(tmp_path, "hero.mp4", b"not a video at all, just text")
    text, attachments, uploads = turn.extract_media(f"MEDIA:{path}", tmp_path)
    assert attachments == uploads == [] and "aren't a real mp4" in text


def test_a_video_over_its_cap_gets_a_note_with_where_it_is(tmp_path, monkeypatch):
    monkeypatch.setitem(turn.ATTACHMENT_MAX_BYTES_BY_TYPE, "video/mp4", 10)
    path = _shot(tmp_path / "work" / "c1", "hero.mp4", _MP4)
    text, _, uploads = turn.extract_media(f"MEDIA:{path}", tmp_path)
    assert uploads == []
    assert text == "_(Couldn't attach hero.mp4: it's 0.0 MB, over the 0.0 MB limit; it's saved in the agent's workspace at `work/c1/hero.mp4`.)_"


def test_the_reply_post_names_an_uploaded_video(home, posted, monkeypatch):
    video = _shot(_profile_dir("ceo") / "work", "hero.mp4", _MP4)
    put = []
    monkeypatch.setattr(turn, "_upload_attachment", lambda room, item: put.append((room, item)) or {
        "attachmentId": "a1", "fileName": item["fileName"], "contentType": item["contentType"]})
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, f"Done.\nMEDIA:{video}", turn.ZERO_USAGE))

    turn.run_turn("ceo", f.ingress())

    reply = next(b for path, b in posted if path.startswith("agent-messages/"))
    assert reply["text"] == "Done." and "attachments" not in reply
    assert reply["uploadedAttachments"] == [{"attachmentId": "a1", "fileName": "hero.mp4", "contentType": "video/mp4"}]
    assert put[0][0] == reply["roomId"]


def test_a_failed_upload_becomes_a_note(home, posted, monkeypatch):
    video = _shot(_profile_dir("ceo") / "work", "hero.mp4", _MP4)
    monkeypatch.setattr(turn, "_upload_attachment", lambda room, item: None)
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, f"Done.\nMEDIA:{video}", turn.ZERO_USAGE))

    turn.run_turn("ceo", f.ingress())

    reply = next(b for path, b in posted if path.startswith("agent-messages/"))
    assert reply["text"] == "Done.\n_(Couldn't attach hero.mp4: the upload failed; it's saved in the agent's workspace at `work/hero.mp4`.)_"


def test_the_upload_lands_at_the_key_the_backend_adopts(monkeypatch, tmp_path):
    calls = []

    class _S3:
        def upload_file(self, src, bucket, key, ExtraArgs=None):
            calls.append((src, bucket, key, ExtraArgs))

    monkeypatch.setattr(turn.sync, "_s3", lambda: _S3())
    monkeypatch.setattr(turn.sync, "S3_BUCKET", "bucket")
    path = _shot(tmp_path, "hero.mp4", _MP4)
    entry = turn._upload_attachment("room-1", {"upload": path, "fileName": "hero.mp4", "contentType": "video/mp4"})
    src, bucket, key, extra = calls[0]
    assert key == f"{turn.sync.S3_PREFIX}/attachments/room-1/{entry['attachmentId']}.mp4"
    assert extra == {"ContentType": "video/mp4"} and src == str(path)


def test_an_outbox_file_too_big_to_send_is_still_kept_in_the_profile(tmp_path, monkeypatch):
    """2026-09-26 prod: the render lived only in HERMES_HOME/outbox, which S3 never sees."""
    monkeypatch.setitem(turn.ATTACHMENT_MAX_BYTES_BY_TYPE, "video/mp4", 10)
    profile = tmp_path / "profiles" / "friday"
    profile.mkdir(parents=True)
    video = _shot(tmp_path / "outbox", "hero.mp4", _MP4)
    text, _, uploads = turn.extract_media(f"MEDIA:{video}", profile, tmp_path)
    assert uploads == [] and (profile / "outbox" / "hero.mp4").read_bytes() == _MP4
    assert text.endswith("it's saved in the agent's workspace at `outbox/hero.mp4`.)_")


# ---- a Worker's task result (live dev bug, 5 Nov: a raw MEDIA: path in the task room) -------------
TASK = "85224fd1-0000-4000-8000-000000000000"


def test_task_result_media_uploads_to_the_task_room_and_strips_the_line(home, monkeypatch):
    work = _profile_dir("spec") / "work" / TASK
    work.mkdir(parents=True)
    (work / "agenda.docx").write_bytes(b"PK\x03\x04" + b"\x00" * 10)
    (work / "notes.md").write_text("# notes")
    (work / "bad.csv").write_bytes(b"\xff\xfe not utf-8")
    other = _profile_dir("spec") / "work" / "another-task"  # another conversation's file, same profile
    other.mkdir(parents=True)
    (other / "private.pdf").write_bytes(b"%PDF-1.7")
    (_profile_dir("spec") / "memories").mkdir(parents=True)
    (_profile_dir("spec") / "memories" / "MEMORY.md").write_text("private")
    put = []
    monkeypatch.setattr(turn, "_upload_attachment", lambda room, item: put.append((room, item["fileName"])) or {
        "attachmentId": "a1", "fileName": item["fileName"], "contentType": item["contentType"]})
    out = "\n".join(["Here is the agenda.", f"MEDIA:{work / 'agenda.docx'}", f"MEDIA:{work / 'notes.md'}",
                     f"MEDIA:{work / 'bad.csv'}", f"MEDIA:{other / 'private.pdf'}",
                     f"MEDIA:{_profile_dir('spec') / 'memories' / 'MEMORY.md'}", f"MEDIA:{work / 'gone.pdf'}"])

    text, files = turn.task_result_media("spec", TASK, out)

    room = turn.task_room_id(TASK)
    assert put == [(room, "agenda.docx"), (room, "notes.md")]  # text uploads too (#4817 review)
    assert [f["contentType"] for f in files] == [
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text/markdown"]
    assert "MEDIA:" not in text and str(home) not in text and text.startswith("Here is the agenda.")
    for name in ("bad.csv", "private.pdf", "MEMORY.md", "gone.pdf"):  # notes, never a raw path or the file
        assert name in text


def test_task_room_id_matches_the_backend():
    import uuid
    ns = uuid.uuid5(uuid.NAMESPACE_URL, "https://thelightbulb.company/ns/super-agent/task")  # tasks_schema.TASK_NS
    assert turn.task_room_id(TASK) == str(uuid.uuid5(ns, f"room:{TASK}"))


def test_a_result_without_media_is_untouched(monkeypatch):
    monkeypatch.setattr(turn, "_upload_attachment", lambda *a: pytest.fail("no upload"))
    assert turn.task_result_media("spec", TASK, "done") == ("done", [])


def test_task_result_outbox_only_on_a_task_vm(home, monkeypatch):
    """#4821 review: on a shared VM `HERMES_HOME/outbox/` holds other conversations' files."""
    monkeypatch.setattr(turn, "HOME", home)
    (home / "outbox").mkdir()
    (home / "outbox" / "shared.pdf").write_bytes(b"%PDF-1.7")
    monkeypatch.setattr(turn, "_upload_attachment", lambda room, item: {
        "attachmentId": "a1", "fileName": item["fileName"], "contentType": item["contentType"]})
    line = f"MEDIA:{home / 'outbox' / 'shared.pdf'}"
    monkeypatch.setattr(turn.task_mode, "active", lambda: False)
    text, files = turn.task_result_media("spec", TASK, line)
    assert files == [] and "shared.pdf" in text and "MEDIA:" not in text
    monkeypatch.setattr(turn.task_mode, "active", lambda: True)
    text, files = turn.task_result_media("spec", TASK, line)
    assert [f["fileName"] for f in files] == ["shared.pdf"]
