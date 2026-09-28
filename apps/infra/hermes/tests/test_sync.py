import hashlib
import json
import shutil
import threading
import time
from pathlib import Path

from bridge import sync


def test_request_dumps_logs_and_turn_context_never_leave_the_microvm():
    assert sync.is_excluded("sessions/request_dump_20260916T101500.json") is True
    assert sync.is_excluded("logs/hermes.log") is True
    assert sync.is_excluded(".lightbulb-turn/room:agent.json") is True
    assert sync.is_excluded("state.db-wal") is True
    assert sync.is_excluded("media/messaging/room-1/a1.png") is True
    assert sync.is_excluded("memories/MEMORY.md") is False
    assert sync.is_excluded("sessions/index.db") is False


def test_the_four_backend_owned_files_never_go_back_up(home, monkeypatch):
    """Task 13 (M4-R20): config.yaml/.env/SOUL.md/.revision are written by
    hermes_profiles.upload_profile/patch_profile_config/set_profile_config and re-pulled by
    ensure_profile on a changed revision -- save_profile must never upload the bridge's own local
    (possibly stale the instant a hot patch lands) copy back over a backend write. Row #15 /
    Ruling M6-R30 adds a fifth: lightbulb-facts.json (hermes_profiles.write_facts_to_profiles)."""
    assert sync.is_excluded("config.yaml") is True
    assert sync.is_excluded(".env") is True
    assert sync.is_excluded("SOUL.md") is True
    assert sync.is_excluded(".revision") is True
    assert sync.is_excluded("lightbulb-facts.json") is True

    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True, exist_ok=True)
    for name in ("config.yaml", ".env", "SOUL.md", ".revision", "lightbulb-facts.json", "memory.md"):
        (profile / name).write_text("x")

    uploaded = []
    monkeypatch.setattr(
        sync,
        "_s3",
        lambda: type("FakeS3", (), {"upload_file": staticmethod(lambda path, bucket, key: uploaded.append(key))})(),
    )
    sync.save_profile("ceo")
    assert uploaded == [f"{sync.S3_PREFIX}/ceo/memory.md"]


def test_s3_client_is_cached_across_calls(monkeypatch):
    """Minor 2 (fix round 2 continuation): _s3() built a fresh boto3.client("s3") on every call
    -- restore_root_auth/save_root_auth alone now cost 2-3 of these per turn
    (_root_auth_revision on both restore and save, plus the actual get/put).

    ``boto3`` itself isn't installed in this test environment (only ``botocore``, per the bridge
    test runner's deps) -- ``_s3()``'s own ``import boto3`` is stubbed via sys.modules instead of
    patching a real ``boto3.client``."""
    import sys
    import types

    monkeypatch.setattr(sync, "_S3_CLIENT", None)
    builds = []

    class _Fake:
        pass

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda *a, **k: (builds.append(1), _Fake())[1]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    first = sync._s3()
    second = sync._s3()
    assert first is second
    assert len(builds) == 1


def test_boot_secrets_come_from_secrets_manager_not_the_runtime_env():
    class FakeSM:
        def get_secret_value(self, SecretId):
            assert SecretId == "lightbulb/dev/hermes/org_test"
            return {"SecretString": json.dumps({"hookToken": "h", "gatewayToken": "g", "bedrockBearer": "b"})}

    assert sync.load_boot_secrets("lightbulb/dev/hermes/org_test", client=FakeSM()) == {
        "hookToken": "h", "gatewayToken": "g", "bedrockBearer": "b"
    }


# ---- fix round (minor 12): two turns for the same agent must not upload concurrently ----------
def test_save_profile_serializes_concurrent_calls_for_the_same_agent(home, monkeypatch):
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "memory.md").write_text("hi")

    active = 0
    max_active = 0
    lock = threading.Lock()

    class FakeS3:
        def upload_file(self, path, bucket, key):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1

    monkeypatch.setattr(sync, "_s3", lambda: FakeS3())

    threads = [threading.Thread(target=sync.save_profile, args=("ceo",)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_active == 1  # never more than one upload in flight for this agent


class _DeleteS3:
    """upload/delete_objects fake with S3's If-Match semantics: ``objects`` maps key -> ETag."""

    def __init__(self, objects):
        self.objects = dict(objects)
        self.deleted = []

    def upload_file(self, path, bucket, key):
        self.objects[key] = sync.s3_etag(Path(path))

    def delete_objects(self, Bucket, Delete):
        done, errors = [], []
        for obj in Delete["Objects"]:
            key = obj["Key"]
            if key not in self.objects:
                errors.append({"Key": key, "Code": "NoSuchKey"})
            elif obj.get("ETag") != self.objects[key]:
                errors.append({"Key": key, "Code": "PreconditionFailed"})
            else:
                del self.objects[key]
                self.deleted.append(key)
                done.append({"Key": key})
        return {"Deleted": done, "Errors": errors}


def _seed(monkeypatch, files, s3_etags):
    """A restored profile: every file local, baseline recorded, S3 holding ``s3_etags``."""
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.yaml").write_text("model: {}\n")          # every real profile has one
    for rel in files:
        (profile / rel).parent.mkdir(parents=True, exist_ok=True)
        (profile / rel).write_text("x")
    sync._MTIMES["ceo"] = {rel: sync._change_stamp(profile / rel) for rel in files}
    sync._SYNCED["ceo"] = {rel: time.time() for rel in files}
    sync._ETAGS["ceo"] = {rel: sync.s3_etag(profile / rel) for rel in files}
    fake = _DeleteS3({f"{sync.S3_PREFIX}/ceo/{rel}": etag for rel, etag in s3_etags.items()})
    monkeypatch.setattr(sync, "_s3", lambda: fake)
    return profile, fake


def test_a_file_the_agent_deleted_is_deleted_in_s3(home, monkeypatch):
    x = '"%s"' % hashlib.md5(b"x").hexdigest()
    files = ["keep.md", "repos/old/a.py", "repos/old/b.py"]
    profile, fake = _seed(monkeypatch, files, {rel: x for rel in files})
    shutil.rmtree(profile / "repos")
    sync.save_profile("ceo")
    assert sorted(fake.deleted) == [f"{sync.S3_PREFIX}/ceo/repos/old/a.py", f"{sync.S3_PREFIX}/ceo/repos/old/b.py"]
    assert set(sync._MTIMES["ceo"]) == {"keep.md"}          # forgotten, never deleted twice
    sync.save_profile("ceo")
    assert len(fake.deleted) == 2


def test_a_file_saved_this_process_is_deletable_too(home, monkeypatch):
    x = '"%s"' % hashlib.md5(b"x").hexdigest()
    profile, fake = _seed(monkeypatch, ["keep.md"], {"keep.md": x})
    (profile / "new.md").write_text("made this turn")
    sync.save_profile("ceo")                                    # uploaded: ETag recorded from the file
    (profile / "new.md").unlink()
    sync.save_profile("ceo")
    assert fake.deleted == [f"{sync.S3_PREFIX}/ceo/new.md"]


def test_a_backend_rewrite_survives_the_agents_delete(home, monkeypatch):
    x = '"%s"' % hashlib.md5(b"x").hexdigest()
    profile, fake = _seed(monkeypatch, ["keep.md", "notes.md"], {"keep.md": x, "notes.md": '"rewritten-by-files-panel"'})
    (profile / "notes.md").unlink()
    sync.save_profile("ceo")
    assert fake.deleted == [] and "notes.md" in sync._MTIMES["ceo"]   # kept for the re-pull


def test_a_profile_that_lost_its_config_deletes_nothing(home, monkeypatch):
    """Directories surviving (home/ is always recreated by restore) prove nothing; the
    backend-owned config.yaml gone means a wiped microVM, never an agent's rm."""
    x = '"%s"' % hashlib.md5(b"x").hexdigest()
    profile, fake = _seed(monkeypatch, ["a.md", "b.md"], {"a.md": x, "b.md": x})
    (profile / "home").mkdir()
    (profile / "config.yaml").unlink()
    for name in ("a.md", "b.md"):
        (profile / name).unlink()
    sync.save_profile("ceo")
    assert fake.deleted == []


def test_excluded_and_already_gone_paths_are_never_deleted(home, monkeypatch):
    x = '"%s"' % hashlib.md5(b"x").hexdigest()
    profile, fake = _seed(monkeypatch, ["keep.md", ".env", "gone.md"], {"keep.md": x, ".env": x})
    (profile / ".env").unlink()                                 # backend-owned: excluded
    (profile / "gone.md").unlink()                              # gone.md: NoSuchKey in S3
    sync.save_profile("ceo")
    assert fake.deleted == []
    assert "gone.md" not in sync._MTIMES["ceo"] and ".env" in sync._MTIMES["ceo"]


def test_s3_etag_matches_s3_for_single_and_multipart_uploads(tmp_path):
    small, edge = tmp_path / "small", tmp_path / "edge"
    small.write_bytes(b"x")
    edge.write_bytes(b"\0" * sync._MULTIPART_CHUNK)             # exactly 8 MiB: one-part multipart
    assert sync.s3_etag(small) == '"%s"' % hashlib.md5(b"x").hexdigest()
    part = hashlib.md5(b"\0" * sync._MULTIPART_CHUNK).digest()
    assert sync.s3_etag(edge) == '"%s-1"' % hashlib.md5(part).hexdigest()


def test_one_bad_file_never_blocks_the_rest_of_the_save(home, monkeypatch):
    """A .db that is not SQLite used to raise out of the pool, cancel every other upload and
    stay unsaved -- so every later save failed too and the agent's work was lost at the park."""
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    profile = sync._profile_dir("ceo")
    (profile / "repo").mkdir(parents=True)
    (profile / "repo" / "analytics.db").write_bytes(b"DUCK not sqlite")
    (profile / "repo" / "flaky.md").write_text("x")
    (profile / "repo" / "notes.md").write_text("x")
    uploaded = []

    def upload_file(path, bucket, key):
        if key.endswith("flaky.md"):
            raise RuntimeError("SlowDown")
        uploaded.append(key)

    monkeypatch.setattr(sync, "_s3", lambda: type("S3", (), {"upload_file": staticmethod(upload_file)})())
    sync.save_profile("ceo")
    p = f"{sync.S3_PREFIX}/ceo/repo/"
    assert sorted(uploaded) == [p + "analytics.db", p + "notes.md"]     # raw bytes for the non-sqlite db
    assert "repo/flaky.md" not in sync._MTIMES["ceo"]                    # retried on the next save
    assert not list(profile.rglob("*.s3snap"))


def test_live_git_locks_never_go_up_or_come_back():
    for rel in (".git/index.lock", "repos/app/.git/index.lock", "repos/app/.git/refs/heads/main.lock"):
        assert sync.is_excluded(rel) and sync._is_git_lock(rel)
    assert not sync.is_excluded("repos/app/.git/HEAD") and not sync._is_git_lock("repos/app/src/x.lock")


def test_a_real_sqlite_db_with_a_stale_snapshot_still_goes_up_as_a_snapshot(home, monkeypatch):
    import sqlite3

    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True)
    with sqlite3.connect(profile / "state.db") as conn:
        conn.execute("create table t (x)")
    (profile / "state.db.s3snap").write_bytes(b"garbage from an interrupted save")
    sent = {}
    monkeypatch.setattr(sync, "_s3", lambda: type("S3", (), {"upload_file": staticmethod(
        lambda path, bucket, key: sent.setdefault(key, Path(path).name))})())
    sync.save_profile("ceo")
    assert sent == {f"{sync.S3_PREFIX}/ceo/state.db": "state.db.s3snap"}   # the snapshot, never the live file
    assert not (profile / "state.db.s3snap").exists()
