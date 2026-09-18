"""Hazards 4 and 5 of the local-exec persistence report: with ``terminal.backend: local`` the
agent's shell runs IN the profile dir, so ``save_profile``'s rglob and ``restore_profile``'s
per-object download loop now see whatever a build produces there -- and whatever credential
file a ``git``/``gh`` session writes.
"""
import time

from bridge import sync
from tests.test_sync_recency import FakeS3

REPRODUCIBLE = (
    "node_modules/left-pad/index.js",
    "repo/node_modules/.bin/tsc",
    ".venv/bin/python",
    "repo/.venv/lib/python3.12/site-packages/x.py",
    "venv/bin/activate",
    "__pycache__/mod.cpython-312.pyc",
    "repo/src/__pycache__/mod.pyc",
    ".next/server/app.js",
    "dist/bundle.js",
    "build/main.o",
    "target/debug/app",
    "coverage/lcov.info",
    ".turbo/cache/x.tar",
    ".pytest_cache/v/cache/lastfailed",
    ".mypy_cache/3.12/x.json",
    ".ruff_cache/content",
    ".cache/uv/wheel",
)

CREDENTIALS = (
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".config/gh/hosts.yml",
    ".aws/credentials",
    ".docker/config.json",
    "repo/.npmrc",
    "repo/.aws/credentials",
)


def test_reproducible_trees_are_excluded_in_both_directions():
    for rel in REPRODUCIBLE:
        assert sync.is_excluded(rel) is True, rel
        assert sync.is_reproducible(rel) is True, rel


def test_source_and_git_stay_synced():
    """``.git/`` is deliberately NOT excluded: a fresh clone's objects are packfiles, not
    thousands of loose objects, and dropping it means re-cloning on every cold start."""
    for rel in ("repo/src/index.ts", "repo/.git/objects/pack/pack-1.pack", "repo/.git/HEAD",
                "memories/MEMORY.md", "buildings/notes.md", "distributed/plan.md"):
        assert sync.is_excluded(rel) is False, rel
        assert sync.is_reproducible(rel) is False, rel


def test_agent_written_credentials_never_go_up():
    for rel in CREDENTIALS:
        assert sync.is_excluded(rel) is True, rel


def test_credentials_are_not_blocked_on_the_way_down():
    """The backend projects ``.aws/credentials``/``.config/gh/hosts.yml`` into the profile as
    BACKEND-OWNED files, exactly like the five in ``BACKEND_OWNED_FILES`` -- which are likewise
    in ``EXCLUDE_GLOBS`` and likewise still have to land on a cold start."""
    for rel in CREDENTIALS:
        assert sync.is_reproducible(rel) is False, rel


def test_save_profile_uploads_neither_build_output_nor_credentials(home, monkeypatch):
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    profile = sync._profile_dir("ceo")
    for rel in REPRODUCIBLE + CREDENTIALS + ("repo/src/index.ts",):
        path = profile / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")

    uploaded = []
    monkeypatch.setattr(
        sync, "_s3",
        lambda: type("S3", (), {"upload_file": staticmethod(lambda p, b, k: uploaded.append(k))})(),
    )
    sync.save_profile("ceo")
    assert uploaded == [f"{sync.S3_PREFIX}/ceo/repo/src/index.ts"]


def test_restore_profile_skips_build_output_but_still_pulls_backend_owned_files(home, monkeypatch):
    """The cold-start tree walk is where a ``node_modules`` already in S3 costs one GetObject
    per file, in front of the first reply. It must still pull the backend-owned names and the
    projected credentials -- ``restore_profile`` is NOT filtered by ``EXCLUDE_GLOBS`` whole."""
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    sync._restored.discard("a1")
    now = time.time()
    keys = ("config.yaml", ".revision", ".aws/credentials", ".config/gh/hosts.yml",
            "repo/src/index.ts", "node_modules/left-pad/index.js", "repo/.venv/bin/python")
    prefix = f"{sync.S3_PREFIX}/a1/"
    fake = FakeS3({prefix + key: ("x", now) for key in keys})
    monkeypatch.setattr(sync, "_s3", lambda: fake)

    sync.restore_profile("a1")
    assert sorted(key[len(prefix):] for key in fake.downloaded) == [
        ".aws/credentials", ".config/gh/hosts.yml", ".revision", "config.yaml", "repo/src/index.ts",
    ]
