"""Cold restore and save move many objects: sequential per-file S3 I/O ran at ~10-16 objects/s
(Forge: 92,139 objects in 5,749 s on 2026-09-20). Both loops now fan out over a bounded pool,
and restore stops pulling back reinstallable home caches an older image already pushed."""

import threading

import pytest
from bridge import sync
from tests.test_sync_recency import FakeS3


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    fake = FakeS3()
    monkeypatch.setattr(sync, "_s3", lambda: fake)
    sync._restored.discard("a1")
    sync._SYNCED.pop("a1", None)
    sync._MTIMES.pop("a1", None)
    return fake


def _p(rel: str) -> str:
    return f"{sync.S3_PREFIX}/a1/{rel}"


def test_restore_runs_downloads_concurrently(home, s3, monkeypatch):
    """Two downloads must be in flight at once -- a sequential loop deadlocks on the barrier."""
    s3.objects = {_p("a.md"): ("a", 1.0), _p("b.md"): ("b", 2.0)}
    barrier = threading.Barrier(2, timeout=5)
    inner = s3.download_file

    def download(bucket, key, dest):
        barrier.wait()
        inner(bucket, key, dest)

    monkeypatch.setattr(s3, "download_file", download)
    sync.restore_profile("a1")
    assert sorted(s3.downloaded) == [_p("a.md"), _p("b.md")]


def test_restore_records_baselines_and_seeds_revision_after_every_download(home, s3):
    s3.objects = {_p("config.yaml"): ("model: x", 10.0), _p(".revision"): ("r9", 11.0)}
    s3.objects.update({_p(f"memories/{i}.md"): (str(i), 12.0) for i in range(50)})

    sync.restore_profile("a1")

    assert len(s3.downloaded) == 52
    assert set(sync._MTIMES["a1"]) == {k.split("/a1/", 1)[1] for k in s3.objects}
    assert sync._SYNCED["a1"]["memories/7.md"] == 12.0
    assert sync._REVISIONS["a1"] == "r9"
    # a restore never re-uploads what it just downloaded
    sync.save_profile("a1")
    assert s3.uploaded == []


def test_restore_skips_stale_reinstallable_home_caches_but_keeps_backend_owned_home_files(home, s3):
    s3.objects = {
        _p("home/.local/share/uv/python/cpython-3.12/bin/python3"): ("x", 1.0),
        # a `uv tool install` env: its ~/.local/bin entry point needs it after a cold start
        _p("home/.local/share/uv/tools/ruff/bin/ruff"): ("tool", 1.0),
        _p("home/.cache/pip/wheel"): ("x", 1.0),
        _p("home/.npm/_cacache/index"): ("x", 1.0),
        _p("home/.local/share/pnpm/store/v3/x"): ("x", 1.0),
        # backend-projected CLI credential under the same .local/share parent: must restore
        _p("home/.local/share/gogcli/credentials.json"): ("tok", 1.0),
        _p(".env"): ("K=v", 1.0),
        _p("skills/tool/build/index.js"): ("migrated skill", 1.0),
        _p(".run/repo/src/app.py"): ("uncommitted work", 1.0),
    }

    sync.restore_profile("a1")

    assert sorted(s3.downloaded) == sorted(
        [
            _p("home/.local/share/gogcli/credentials.json"),
            _p("home/.local/share/uv/tools/ruff/bin/ruff"),
            _p(".env"),
            _p("skills/tool/build/index.js"),
            _p(".run/repo/src/app.py"),
        ]
    )


def test_save_runs_uploads_concurrently_and_never_pushes_uv(home, s3, monkeypatch):
    sync.restore_profile("a1")
    root = sync._profile_dir("a1")
    (root / "memories").mkdir(parents=True, exist_ok=True)
    (root / "memories" / "a.md").write_text("a")
    (root / "memories" / "b.md").write_text("b")
    uv = root / "home" / ".local" / "share" / "uv" / "python"
    uv.mkdir(parents=True)
    (uv / "python3").write_text("binary")
    barrier = threading.Barrier(2, timeout=5)
    inner = s3.upload_file

    def upload(src, bucket, key):
        barrier.wait()
        inner(src, bucket, key)

    monkeypatch.setattr(s3, "upload_file", upload)
    sync.save_profile("a1")
    assert sorted(s3.uploaded) == [_p("memories/a.md"), _p("memories/b.md")]
    assert set(sync._MTIMES["a1"]) >= {"memories/a.md", "memories/b.md"}


def test_a_failed_restore_raises_without_downloading_the_rest(home, s3, monkeypatch):
    """The first error cancels every transfer not yet started instead of draining the queue."""
    s3.objects = {_p(f"f/{i:05d}.md"): ("x", 1.0) for i in range(2000)}

    # First download fails; the rest block briefly, so the failure is seen and the queue cancelled
    # while at most one pool's worth is in flight. (Instant failures let the workers drain all 2000
    # before the main thread cancels anything -- that made this flaky.)
    first = threading.Lock()

    def throttled(bucket, key, dest):
        if first.acquire(blocking=False):
            raise RuntimeError("SlowDown")
        threading.Event().wait(0.5)

    monkeypatch.setattr(s3, "download_file", throttled)
    calls = []
    real = sync._fan_out

    def counting(fn, items):
        def wrapped(item):
            calls.append(item)
            return fn(item)

        return real(wrapped, items)

    monkeypatch.setattr(sync, "_fan_out", counting)
    with pytest.raises(RuntimeError, match="SlowDown"):
        sync.restore_profile("a1")
    assert len(calls) <= 2 * sync._SYNC_WORKERS
    assert "a1" not in sync._restored
