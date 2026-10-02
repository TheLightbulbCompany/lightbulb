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
    ".docker/config.json",
    ".aws/credentials",
    ".config/gh/hosts.yml",
    "repo/.npmrc",
    "repo/.aws/credentials",
    "repo/.config/gh/hosts.yml",
)


def test_reproducible_trees_are_push_excluded_only():
    """``is_reproducible`` is deliberately NOT part of ``EXCLUDE_GLOBS``: it is consulted by
    save_profile and by nothing on the way down (see the predicate's docstring)."""
    for rel in REPRODUCIBLE:
        assert sync.is_reproducible(rel) is True, rel
        assert sync.is_excluded(rel) is False, rel


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


def test_browser_screenshots_are_not_pushed():
    # ONE glob: is_excluded uses fnmatch, whose * matches "/", so this covers every depth.
    assert sync.is_excluded("cache/screenshots/browser_screenshot_abc.png")
    # Genuinely nested, not just flat -- the "every depth" claim above, actually exercised.
    assert sync.is_excluded("cache/screenshots/sub/browser_screenshot_abc.png")
    # Sibling negative: a similarly-named but different directory must NOT be swept up by the glob.
    assert sync.is_excluded("cache/other/x.png") is False


def test_nested_credential_copies_are_covered_whichever_pr_lands_first():
    """Overlap with the CLI-credential rail: its root-anchored ``.aws/*`` / ``.config/gh/*``
    make the root entries here a harmless duplicate (``is_excluded`` is an ``any()``), kept so
    this file does not depend on merge order. The NESTED forms are not duplicates at all --
    its prefixes are root-anchored and never match a copy inside a clone."""
    for rel in (".aws/credentials", "repo/.aws/credentials",
                ".config/gh/hosts.yml", "repo/deep/.config/gh/hosts.yml"):
        assert sync.is_excluded(rel) is True, rel


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


def test_restore_profile_downloads_everything_in_the_prefix(home, monkeypatch):
    """Fix round 1 (B1): the cold-start walk has NO reproducible filter. An object under one of
    those names got into S3 from a BACKEND writer -- the migration carries ``skills/**``
    verbatim, and the Files panel accepts any non-dot path -- and skipping it half-materialises
    a skill with no error anywhere."""
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    sync._restored.discard("a1")
    now = time.time()
    keys = ("config.yaml", ".revision", ".aws/credentials", ".config/gh/hosts.yml",
            "repo/src/index.ts", "skills/tool/node_modules/index.js", "dist/report.html")
    prefix = f"{sync.S3_PREFIX}/a1/"
    fake = FakeS3({prefix + key: ("x", now) for key in keys})
    monkeypatch.setattr(sync, "_s3", lambda: fake)

    sync.restore_profile("a1")
    assert sorted(key[len(prefix):] for key in fake.downloaded) == sorted(keys)


def test_pnpm_store_stays_local_across_restore_refresh_and_save(home, monkeypatch):
    """A dependency install must not turn the next cold start into thousands of S3 GETs."""
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    sync._restored.discard("a1")
    prefix = f"{sync.S3_PREFIX}/a1/"
    cache = "home/.local/share/pnpm/store/v3/files/00/package"
    kept = "home/.local/share/project/notes.txt"
    fake = FakeS3({prefix + cache: ("cached", time.time()),
                   prefix + kept: ("notes", time.time())})
    monkeypatch.setattr(sync, "_s3", lambda: fake)

    sync.restore_profile("a1")
    assert fake.downloaded == [prefix + kept]

    fake.downloaded.clear()
    fake.objects[prefix + cache] = ("new cache", time.time() + 1)
    assert sync._pull_changed_objects("a1") is True
    assert fake.downloaded == []

    profile = sync._profile_dir("a1")
    path = profile / cache
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("local cache")
    source = profile / "repo/src/index.ts"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("source")
    sync.save_profile("a1")
    assert fake.uploaded == [prefix + "repo/src/index.ts"]


def test_a_backend_written_build_dir_still_re_pulls_on_a_revision_bump(home, monkeypatch):
    """The recency walk honours EXCLUDE_GLOBS, which is exactly why the reproducible names are
    not in it: a Files-panel upload into ``dist/`` has to reach the microVM."""
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    prefix = f"{sync.S3_PREFIX}/a1/"
    fake = FakeS3({prefix + "dist/report.html": ("x", time.time())})
    monkeypatch.setattr(sync, "_s3", lambda: fake)

    assert sync._pull_changed_objects("a1") is True
    assert fake.downloaded == [prefix + "dist/report.html"]


def test_hermes_lock_sidecars_never_sync_in_either_direction(home, monkeypatch):
    """A lock file means "a process HERE holds this". Prod S3 carried two zero-byte ones next to
    state.db; restoring either onto a fresh microVM asserts a lock nobody holds. The restore leg
    needs its own check because that walk deliberately has no EXCLUDE_GLOBS filter (the test
    above) -- and an agent's own Cargo.lock/uv.lock must keep syncing."""
    assert sync.is_excluded("state.db.fts_rebuild.lock")
    assert sync.is_excluded("state.db.quarantine.lock")
    assert sync.is_lock_sidecar("state.db.quarantine.lock")
    for source in ("Cargo.lock", "repo/uv.lock", "repo/web/yarn.lock"):
        assert not sync.is_excluded(source), source
        assert not sync.is_lock_sidecar(source), source

    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    sync._restored.discard("a1")
    now = time.time()
    prefix = f"{sync.S3_PREFIX}/a1/"
    fake = FakeS3({prefix + key: ("x", now)
                   for key in ("state.db.quarantine.lock", "repo/Cargo.lock")})
    monkeypatch.setattr(sync, "_s3", lambda: fake)

    sync.restore_profile("a1")
    assert [key[len(prefix):] for key in fake.downloaded] == ["repo/Cargo.lock"]


def test_caches_backups_and_work_trees_stay_in_s3_until_a_turn_needs_them(home, monkeypatch):
    """Pulse's cold restore was 3,582 objects / 22.8 s, of which ~38 were the profile: the rest
    were Hermes tool caches, curator rollback blobs and twenty old conversations' work/ trees.
    A cold start pulls none of those; a conversation's work/ tree comes down when ITS turn starts
    (restore_subtree), once; caches never go up again either."""
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    sync._restored.discard("a1")
    sync._RESTORED_SUBTREES.clear()
    now = time.time()
    prefix = f"{sync.S3_PREFIX}/a1/"
    keys = ("config.yaml", "memories/notes.md", "cache/web/page.json", "cache/terminal-output/1.txt",
            ".curator_backups/blobs/x", "work/room-1/repo/main.py", "work/room-2/notes.txt")
    fake = FakeS3({prefix + key: ("x", now) for key in keys})
    monkeypatch.setattr(sync, "_s3", lambda: fake)

    sync.restore_profile("a1")
    assert sorted(k[len(prefix):] for k in fake.downloaded) == ["config.yaml", "memories/notes.md"]

    fake.downloaded.clear()
    profile = sync._profile_dir("a1")
    assert sync.restore_subtree("a1", profile / "work" / "room-1") == 1
    assert [k[len(prefix):] for k in fake.downloaded] == ["work/room-1/repo/main.py"]
    assert (profile / "work/room-1/repo/main.py").read_text() == "x"
    assert sync.restore_subtree("a1", profile / "work" / "room-1") == 0  # once per process

    fake.downloaded.clear()
    fake.uploaded = getattr(fake, "uploaded", [])
    (profile / "cache" / "web").mkdir(parents=True, exist_ok=True)
    (profile / "cache" / "web" / "new.json").write_text("{}")
    (profile / "work" / "room-1" / "repo" / "main.py").write_text("changed")
    assert sync.is_excluded("cache/web/new.json")
    sync.save_profile("a1")
    uploaded = [k[len(prefix):] for k in getattr(fake, "uploaded", [])]
    assert "work/room-1/repo/main.py" in uploaded and not any(k.startswith("cache/") for k in uploaded)
