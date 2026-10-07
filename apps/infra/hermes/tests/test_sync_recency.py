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


class NoSuchKey(Exception):
    """Shaped like the botocore ClientError boto3's download_file raises for a missing key, so
    sync._is_absent can tell it from a throttle."""

    def __init__(self, key: str):
        super().__init__(key)
        self.response = {"Error": {"Code": "404"}}  # per instance, like botocore's ClientError


class FakeS3:
    """An S3 with LastModified. ``objects`` maps a full key to (body, epoch seconds).

    Stamps are REAL clock values (``time.time()``), not a toy counter starting at 1000: the
    invariant under test is how the instant sync records compares with the object's own
    LastModified, and a toy clock 1.8e9 seconds in the past makes every such assertion pass by
    a margin no bug could cross.
    """

    def __init__(self, objects: dict[str, tuple[str, float]] | None = None):
        self.objects = dict(objects or {})
        self.downloaded: list[str] = []
        self.uploaded: list[str] = []
        #: called as (key, stamp) from inside upload_file -- the PUT's own window
        self.on_upload = None
        self.omit_last_modified = False

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        outer = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                contents = []
                for key, (_, when) in sorted(outer.objects.items()):
                    if not key.startswith(Prefix):
                        continue
                    entry = {"Key": key}
                    if not outer.omit_last_modified:
                        entry["LastModified"] = _when(when)
                    contents.append(entry)
                return [{"Contents": contents}]

        return Paginator()

    def _body(self, key: str) -> str:
        if key not in self.objects:
            raise NoSuchKey(key)
        return self.objects[key][0]

    def download_file(self, bucket, key, dest):
        body = self._body(key)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_text(body)
        self.downloaded.append(key)

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self._body(Key).encode())}

    def upload_file(self, src, bucket, key):
        stamp = time.time()  # real S3 stamps the object when the PUT lands
        self.objects[key] = (Path(src).read_bytes().decode("utf-8", "replace"), stamp)
        self.uploaded.append(key)
        if self.on_upload is not None:
            self.on_upload(key, stamp)


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


def test_a_bump_leaves_other_conversations_work_trees_in_s3_but_takes_a_restored_ones_edits(home, s3, monkeypatch):
    """Codex on #3794: the cold restore leaves work/ absent from _SYNCED, so this walk saw every old work
    object as new and brought the 940 MB straight back on the first bump. A tree this process restored
    for its turn still takes a Files-panel save; the others stay in S3 with the stale home caches."""
    s3.objects = {
        _prefix() + "work/room-1/notes.md": ("edited in the Files panel", 300.0),
        _prefix() + "work/room-2/clip.mp4": ("an old conversation's clip", 300.0),
        _prefix() + "home/.cache/pip/wheel": ("x", 300.0),
    }
    sync._SYNCED["a1"] = {}
    monkeypatch.setattr(sync, "_RESTORED_SUBTREES", {("a1", "work/room-1/")})

    sync._pull_changed_objects("a1")

    assert s3.downloaded == [_prefix() + "work/room-1/notes.md"]


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

    # save_profile uploads on a pool (_fan_out): completion order is not the walk order
    assert sorted(s3.uploaded) == [_prefix() + "memories/MEMORY.md", _prefix() + "notes.md"]

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
    s3.objects[_prefix() + "memories/USER.md"] = ("new", time.time())
    s3.objects[_prefix() + ".revision"] = ("r2", 900.0)
    before_save = time.time()
    sync.save_profile("a1")
    after_save = time.time()

    s3.downloaded.clear()
    sync.ensure_profile("a1")  # next turn

    # (ensure_profile also re-pulls the named backend-owned files, .revision among them)
    assert _prefix() + "memories/USER.md" in s3.downloaded
    assert (profile / "memories" / "USER.md").read_text() == "new"
    # and the save DID advance the instant for the file it uploaded -- bracketed by the real
    # clock, so this cannot pass vacuously the way a toy-clock comparison would. It is recorded
    # before the PUT (I1), hence `<= after_save`, never after it.
    assert before_save <= sync._SYNCED["a1"]["memories/MEMORY.md"] <= after_save


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
    assert sync._pull_changed_objects("a1") is False  # must not raise, must not claim success


# ---- fix round 1 -------------------------------------------------------------------------------
def test_a_backend_write_inside_the_put_window_is_still_pulled(home, s3):
    """I1: the sync instant must be read BEFORE the PUT. Read after, it lands past the object's
    real LastModified by the PUT's own latency plus any clock skew, and a backend write inside
    that gap is `<= synced[rel]` FOREVER -- skipped on every later bump, never retried, and
    overwritten the next time the agent touches the file."""
    s3.objects = {
        _prefix() + ".revision": ("r1", 10.0),
        _prefix() + "memories/USER.md": ("agent v1", 10.0),
    }
    sync._REVISIONS.pop("a1", None)
    sync.restore_profile("a1")

    profile = sync._profile_dir("a1")
    (profile / "memories" / "USER.md").write_text("agent v2")

    def owner_saves_inside_the_put(key, stamp):
        if key.endswith("USER.md"):
            s3.objects[key] = ("owner edit", stamp + 0.001)
            s3.objects[_prefix() + ".revision"] = ("r2", stamp + 0.001)
            time.sleep(0.05)  # PUT latency: a post-PUT clock read would land past that edit

    s3.on_upload = owner_saves_inside_the_put
    sync.save_profile("a1")

    sync.ensure_profile("a1")
    assert (profile / "memories" / "USER.md").read_text() == "owner edit"


def test_a_failed_pull_does_not_advance_the_revision(home, s3):
    """I2: a bump announces content, and there is no second bump until the next backend write --
    so recording a revision whose content never landed strands the agent on stale instructions
    for the life of the microVM."""
    s3.objects = {
        _prefix() + ".revision": ("r2", 10.0),
        _prefix() + "AGENTS.md": ("new instructions", 500.0),
    }
    sync._REVISIONS["a1"] = "r1"

    listings = iter([RuntimeError("throttled")])
    real_paginator = s3.get_paginator

    def flaky(name):
        blow_up = next(listings, None)
        if blow_up is not None:
            raise blow_up
        return real_paginator(name)

    s3.get_paginator = flaky

    sync.ensure_profile("a1")
    assert sync._REVISIONS["a1"] == "r1"
    assert not (sync._profile_dir("a1") / "AGENTS.md").exists()

    sync.ensure_profile("a1")  # the next turn retries, with no new bump to prompt it
    assert sync._REVISIONS["a1"] == "r2"
    assert (sync._profile_dir("a1") / "AGENTS.md").read_text() == "new instructions"


def test_a_missing_backend_owned_object_is_not_a_failed_pull(home, s3):
    """The other half of I2: a profile mid-provision (or one predating Task 7) legitimately has
    no SOUL.md, and a 404 must not pin its revision forever."""
    s3.objects = {
        _prefix() + ".revision": ("r2", 10.0),
        _prefix() + "memories/USER.md": ("hi", 500.0),
    }
    sync._REVISIONS["a1"] = "r1"

    sync.ensure_profile("a1")

    assert sync._REVISIONS["a1"] == "r2"


def test_one_unreadable_key_does_not_mask_every_key_after_it(home, s3):
    """I2: the try used to wrap the whole listing, so one poison key (an S3 directory marker,
    say) aborted every later object on every future bump, permanently and silently."""
    s3.objects = {
        _prefix() + "memories/": ("", 500.0),  # directory marker
        _prefix() + "memories/poison.md": ("x", 500.0),
        _prefix() + "memories/USER.md": ("new", 500.0),
    }
    real_download = s3.download_file

    def flaky(bucket, key, dest):
        if key.endswith("poison.md"):
            raise RuntimeError("IsADirectoryError")
        real_download(bucket, key, dest)

    s3.download_file = flaky

    assert sync._pull_changed_objects("a1") is False
    assert (sync._profile_dir("a1") / "memories" / "USER.md").read_text() == "new"
    assert _prefix() + "memories/" not in s3.downloaded


def test_a_live_sqlite_file_is_never_swapped_under_a_running_turn(home, s3):
    """I3: the walk can now reach every key in the prefix, and the Files-panel write route takes
    any relative path -- renaming a foreign file over the inode a concurrent turn holds open is
    textbook SQLite corruption."""
    profile = sync._profile_dir("a1")
    (profile / "sessions").mkdir(parents=True)
    (profile / "sessions" / "index.db").write_text("live session rows")
    s3.objects = {_prefix() + "sessions/index.db": ("foreign", 900.0)}

    sync._pull_changed_objects("a1")

    assert s3.downloaded == []
    assert (profile / "sessions" / "index.db").read_text() == "live session rows"


def test_an_unsaved_local_change_is_not_clobbered_by_the_pull(home, s3):
    """I3: last-writer-wins stays honest for in-flight work -- the turn's own save is the later
    write, so the pull leaves it alone and takes the backend's copy on the bump after that."""
    s3.objects = {_prefix() + "memories/USER.md": ("old", 10.0)}
    sync.restore_profile("a1")
    profile = sync._profile_dir("a1")
    (profile / "memories" / "USER.md").write_text("mid-turn agent edit")

    s3.objects[_prefix() + "memories/USER.md"] = ("owner edit", time.time())
    sync._pull_changed_objects("a1")
    assert (profile / "memories" / "USER.md").read_text() == "mid-turn agent edit"

    sync.save_profile("a1")
    s3.objects[_prefix() + "memories/USER.md"] = ("owner edit", time.time() + 1)
    sync._pull_changed_objects("a1")
    assert (profile / "memories" / "USER.md").read_text() == "owner edit"


def test_a_listing_with_no_last_modified_downloads_rather_than_skips(home, s3):
    """m2: "unknown" has to fail OPEN. Failing closed is silent, and the thing it silences is
    the one mechanism that carries backend writes."""
    s3.objects = {_prefix() + "memories/USER.md": ("new", 500.0)}
    s3.omit_last_modified = True
    sync._SYNCED["a1"] = {"memories/USER.md": time.time()}

    sync._pull_changed_objects("a1")

    assert s3.downloaded == [_prefix() + "memories/USER.md"]


def test_a_change_stamp_never_raises_and_aborts_the_whole_save(home):
    """m1: SQLite drops -wal/-shm on the last connection's close, so a stat can lose its race
    with a checkpoint -- and save_profile, unlike the pull, is not wrapped best-effort, so the
    raise skipped every remaining file in the turn's save."""
    profile = sync._profile_dir("a1")
    profile.mkdir(parents=True, exist_ok=True)
    db = profile / "index.db"
    db.write_text("db")

    assert sync._change_stamp(db) == db.stat().st_mtime_ns
    assert sync._change_stamp(profile / "checkpointed-away.db") == 0
