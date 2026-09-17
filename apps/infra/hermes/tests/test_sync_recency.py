"""Ruling M6-R51: a revision bump re-pulls by RECENCY, and a save uploads only what the turn
changed.

The defect these pin (task-10b review, finding C1): the backend writes more than its five
BACKEND_OWNED_FILES into a profile prefix -- memories/USER.md and AGENTS.md from the
Instructions tab, any Files-panel edit, memories/ seeded by the migration job -- and a warm
microVM saw none of it, then silently reverted it by uploading its whole stale local tree at
the end of the next turn.
"""
import datetime as dt
import io
import time
from pathlib import Path

import pytest
from bridge import sync


def _when(epoch: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc)


class FakeS3:
    """An S3 with LastModified. ``objects`` maps a full key to (body, epoch seconds)."""

    def __init__(self, objects: dict[str, tuple[str, float]] | None = None):
        self.objects = dict(objects or {})
        self.downloaded: list[str] = []
        self.uploaded: list[str] = []
        self.clock = 1_000.0

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        outer = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": key, "LastModified": _when(when)}
                                      for key, (_, when) in sorted(outer.objects.items())
                                      if key.startswith(Prefix)]}]

        return Paginator()

    def download_file(self, bucket, key, dest):
        body, _ = self.objects[key]  # KeyError stands in for NoSuchKey
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_text(body)
        self.downloaded.append(key)

    def get_object(self, Bucket, Key):
        body, _ = self.objects[Key]  # KeyError stands in for NoSuchKey
        return {"Body": io.BytesIO(body.encode())}

    def upload_file(self, src, bucket, key):
        self.clock += 1
        self.objects[key] = (Path(src).read_bytes().decode("utf-8", "replace"), self.clock)
        self.uploaded.append(key)


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    fake = FakeS3()
    monkeypatch.setattr(sync, "_s3", lambda: fake)
    sync._restored.discard("a1")
    return fake


def _prefix() -> str:
    return f"{sync.S3_PREFIX}/a1/"


def test_a_revision_bump_pulls_only_objects_newer_than_our_copy(home, s3):
    s3.objects = {
        _prefix() + "memories/USER.md": ("new", 200.0),      # backend wrote it since our pull
        _prefix() + "memories/MEMORY.md": ("stale", 50.0),   # older than our copy
        _prefix() + "logs/hermes.log": ("noise", 300.0),     # excluded, however new
        _prefix() + "sessions/request_dump_1.json": ("dump", 300.0),
        _prefix() + "media/messaging/r1/a.png": ("img", 300.0),
    }
    sync._SYNCED["a1"] = {"memories/USER.md": 100.0, "memories/MEMORY.md": 100.0}

    sync._pull_changed_objects("a1")

    assert s3.downloaded == [_prefix() + "memories/USER.md"]
    assert (sync._profile_dir("a1") / "memories" / "USER.md").read_text() == "new"
    assert sync._SYNCED["a1"]["memories/USER.md"] == 200.0


def test_an_object_this_microvm_has_never_seen_is_pulled(home, s3):
    """The migration job seeds memories/ into a prefix a warm process already restored, and an
    owner can create a file the agent has no local copy of."""
    s3.objects = {_prefix() + "memories/seeded.md": ("from the migration", 10.0)}
    sync._SYNCED["a1"] = {}

    sync._pull_changed_objects("a1")

    assert s3.downloaded == [_prefix() + "memories/seeded.md"]


def test_a_cold_restore_still_downloads_everything(home, s3):
    s3.objects = {
        _prefix() + "config.yaml": ("model: x", 10.0),
        _prefix() + ".revision": ("r5", 11.0),
        _prefix() + "memories/USER.md": ("hi", 12.0),
    }
    sync._SYNCED.pop("a1", None)
    sync._MTIMES.pop("a1", None)

    sync.restore_profile("a1")

    assert sorted(s3.downloaded) == sorted(s3.objects)
    assert sync._SYNCED["a1"]["memories/USER.md"] == 12.0
    assert "memories/USER.md" in sync._MTIMES["a1"]
    assert sync._REVISIONS["a1"] == "r5"


def test_save_uploads_only_what_the_turn_changed(home, s3):
    s3.objects = {
        _prefix() + "memories/USER.md": ("owner text", 10.0),
        _prefix() + "memories/MEMORY.md": ("agent text", 10.0),
    }
    sync.restore_profile("a1")
    s3.uploaded.clear()

    sync.save_profile("a1")
    assert s3.uploaded == []  # nothing local moved: S3 owns every one of these files

    profile = sync._profile_dir("a1")
    (profile / "memories" / "MEMORY.md").write_text("agent text + this turn")
    (profile / "notes.md").write_text("brand new")
    (profile / "logs").mkdir()
    (profile / "logs" / "hermes.log").write_text("never leaves the microVM")
    sync.save_profile("a1")

    assert s3.uploaded == [_prefix() + "memories/MEMORY.md", _prefix() + "notes.md"]

    s3.uploaded.clear()
    sync.save_profile("a1")
    assert s3.uploaded == []  # and the same two are not uploaded twice


def test_a_backend_write_reaches_the_agent_and_survives_the_turns_save(home, s3):
    """The C1 regression, end to end: restore -> the backend writes AGENTS.md and bumps the
    revision -> the next turn's ensure_profile -> save_profile. The backend's bytes must be
    what the agent reads AND what is still in S3 afterwards."""
    s3.objects = {
        _prefix() + ".revision": ("r1", 10.0),
        _prefix() + "AGENTS.md": ("old instructions", 10.0),
        _prefix() + "memories/MEMORY.md": ("agent text", 10.0),
    }
    sync._REVISIONS.pop("a1", None)
    sync.restore_profile("a1")

    s3.objects[_prefix() + "AGENTS.md"] = ("new instructions", 500.0)
    s3.objects[_prefix() + ".revision"] = ("r2", 500.0)
    sync.ensure_profile("a1")

    profile = sync._profile_dir("a1")
    assert (profile / "AGENTS.md").read_text() == "new instructions"

    # the turn changes only its own memory file
    (profile / "memories" / "MEMORY.md").write_text("agent text + this turn")
    s3.uploaded.clear()
    sync.save_profile("a1")

    assert s3.uploaded == [_prefix() + "memories/MEMORY.md"]
    assert s3.objects[_prefix() + "AGENTS.md"][0] == "new instructions"


def test_a_backend_write_during_the_turn_is_picked_up_by_the_next_bump(home, s3):
    """The sync instant is per file, and a save advances it only for the files it uploaded --
    so the turn's own upload is not re-pulled next time, while a backend write that landed
    mid-turn (after this turn's ensure_profile, before its save) still is."""
    s3.objects = {
        _prefix() + ".revision": ("r1", 10.0),
        _prefix() + "memories/USER.md": ("old", 10.0),
        _prefix() + "memories/MEMORY.md": ("agent text", 10.0),
    }
    sync._REVISIONS.pop("a1", None)
    sync.restore_profile("a1")

    profile = sync._profile_dir("a1")
    (profile / "memories" / "MEMORY.md").write_text("agent text + this turn")
    # the owner saves the Instructions tab WHILE the turn is running
    s3.objects[_prefix() + "memories/USER.md"] = ("new", 900.0)
    s3.objects[_prefix() + ".revision"] = ("r2", 900.0)
    sync.save_profile("a1")

    s3.downloaded.clear()
    sync.ensure_profile("a1")  # next turn

    # (ensure_profile also re-pulls the named backend-owned files, .revision among them)
    assert _prefix() + "memories/USER.md" in s3.downloaded
    assert _prefix() + "memories/MEMORY.md" not in s3.downloaded  # our own upload, not re-pulled
    assert (profile / "memories" / "USER.md").read_text() == "new"


def test_a_pulled_file_is_not_pushed_straight_back_up(home, s3):
    """A recency pull rewrites the local file, which moves its mtime -- without recording that
    mtime the very next save would upload the backend's own bytes back at it, which is how the
    save side would re-introduce the clobber the pull side just fixed."""
    s3.objects = {_prefix() + "memories/USER.md": ("old", 10.0)}
    sync.restore_profile("a1")
    s3.objects[_prefix() + "memories/USER.md"] = ("new", 500.0)
    sync._pull_changed_objects("a1")

    s3.uploaded.clear()
    sync.save_profile("a1")
    assert s3.uploaded == []


def test_a_sqlite_write_that_only_moved_the_wal_still_goes_up(home, s3):
    """In WAL mode a write lands in the -wal sidecar and the main file's mtime does not move
    until a checkpoint, so an mtime-only comparison would drop a turn's session rows."""
    import sqlite3

    profile = sync._profile_dir("a1")
    (profile / "sessions").mkdir(parents=True)
    db = profile / "sessions" / "index.db"
    sqlite3.connect(db).close()
    sync._MTIMES["a1"] = {"sessions/index.db": db.stat().st_mtime_ns}
    sync._SYNCED["a1"] = {}

    wal = db.with_name(db.name + "-wal")
    wal.write_text("pages")
    time.sleep(0.01)

    sync.save_profile("a1")
    assert s3.uploaded == [_prefix() + "sessions/index.db"]


def test_the_recency_pull_leaves_mcp_tokens_to_the_seeded_only_rule(home, s3):
    """F2 / M6-R35 is NOT redundant under recency: Hermes rotates a token file in place, and a
    newer backend object must not replace a rotated local copy."""
    tokens = sync._profile_dir("a1") / "mcp-tokens"
    tokens.mkdir(parents=True)
    (tokens / "stripe.json").write_text('{"access_token": "already-rotated"}')
    s3.objects = {_prefix() + "mcp-tokens/stripe.json": ('{"access_token": "seed"}', 900.0)}
    sync._SYNCED["a1"] = {}

    sync._pull_changed_objects("a1")

    assert s3.downloaded == []
    assert (tokens / "stripe.json").read_text() == '{"access_token": "already-rotated"}'


def test_a_listing_failure_does_not_abort_ensure_profile(home, monkeypatch):
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")

    class ExplodingS3:
        def get_paginator(self, name):
            raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(sync, "_s3", lambda: ExplodingS3())
    sync._pull_changed_objects("a1")  # must not raise
