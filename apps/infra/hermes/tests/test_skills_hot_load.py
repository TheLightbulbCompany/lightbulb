"""F5 (spec 02 §2.7/§3.4): a skill pulled into a warm microVM must reach the NEXT session's
system prompt. Hermes caches the skills prompt in-process keyed by dirs/tools/platform, not by
the manifest, so the bridge clears that cache once after a re-pull that touched ``skills/``."""
import sys
import types

import pytest

from bridge import sync

AGENT = "a1"


class FakeS3:
    def __init__(self, keys):
        self.keys = keys
        self.downloaded = []

    def get_paginator(self, name):
        keys = self.keys

        class Paginator:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": f"{Prefix}{k}"} for k in keys]}]

        return Paginator()

    def download_file(self, bucket, key, dest):
        self.downloaded.append(key)
        with open(dest, "w") as fh:
            fh.write("x")


@pytest.fixture
def fake_prompt_builder(monkeypatch, home):
    """Stand-in for Hermes's agent.prompt_builder: a process-global skills prompt cache keyed
    without the manifest, exactly the shape that makes a warm pull invisible."""
    cache = {}
    clears = []

    def build_skills_prompt():
        if "p" not in cache:
            skills_dir = sync._profile_dir(AGENT) / "skills"
            names = sorted(p.name for p in skills_dir.iterdir()) if skills_dir.is_dir() else []
            cache["p"] = "skills: " + ",".join(names)
        return cache["p"]

    def clear_skills_system_prompt_cache(clear_snapshot=False):
        clears.append(clear_snapshot)
        cache.clear()

    pkg = types.ModuleType("agent")
    mod = types.ModuleType("agent.prompt_builder")
    mod.clear_skills_system_prompt_cache = clear_skills_system_prompt_cache
    mod.build_skills_prompt = build_skills_prompt
    pkg.prompt_builder = mod
    monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setitem(sys.modules, "agent.prompt_builder", mod)
    return mod, clears


def _pull(monkeypatch, keys, revision):
    s3 = FakeS3(keys)
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_s3", lambda: s3)
    monkeypatch.setattr(sync, "_read_revision", lambda agent_id: revision)
    monkeypatch.setattr(sync, "_pull_new_mcp_token_files", lambda agent_id: None)
    monkeypatch.setattr(sync, "_pull_cli_credentials", lambda agent_id: None)
    sync.ensure_profile(AGENT)
    return s3


@pytest.fixture(autouse=True)
def _revisions():
    sync._REVISIONS.clear()
    sync._SKILLS_TOUCHED.clear()
    yield
    sync._REVISIONS.clear()
    sync._SKILLS_TOUCHED.clear()


def test_revision_pull_with_skill_change_clears_prompt_cache(monkeypatch, fake_prompt_builder):
    _, clears = fake_prompt_builder
    sync._REVISIONS[AGENT] = "1"
    _pull(monkeypatch, ["skills/x/SKILL.md"], "2")
    assert clears == [False]  # once; the disk snapshot is manifest-checked and stays


def test_pull_without_skill_change_does_not_clear(monkeypatch, fake_prompt_builder):
    _, clears = fake_prompt_builder
    sync._REVISIONS[AGENT] = "1"
    _pull(monkeypatch, ["memories/MEMORY.md"], "2")
    assert clears == []


def test_missing_hermes_module_is_ignored(monkeypatch, home):
    monkeypatch.setitem(sys.modules, "agent", None)  # import raises ImportError
    sync._REVISIONS[AGENT] = "1"
    _pull(monkeypatch, ["skills/x/SKILL.md"], "2")  # must not raise
    assert sync._REVISIONS[AGENT] == "2"


def test_new_session_sees_new_skill_old_session_prompt_unchanged(monkeypatch, fake_prompt_builder):
    pb, _ = fake_prompt_builder
    (sync._profile_dir(AGENT) / "skills" / "old").mkdir(parents=True)
    sync._REVISIONS[AGENT] = "1"
    old_session_prompt = pb.build_skills_prompt()  # stored on the session at build time

    _pull(monkeypatch, ["skills/new/SKILL.md"], "2")

    assert old_session_prompt == "skills: old"
    assert pb.build_skills_prompt() == "skills: new,old"


def test_incomplete_pull_that_wrote_a_skill_still_clears(monkeypatch, fake_prompt_builder):
    """A skill that landed must reach new sessions even if another key failed this turn."""
    _, clears = fake_prompt_builder
    sync._REVISIONS[AGENT] = "1"
    s3 = FakeS3(["skills/x/SKILL.md", "memories/bad.md"])
    real_download = s3.download_file

    def flaky(bucket, key, dest):
        if key.endswith("bad.md"):
            raise RuntimeError("throttled")
        real_download(bucket, key, dest)

    s3.download_file = flaky
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_s3", lambda: s3)
    monkeypatch.setattr(sync, "_read_revision", lambda agent_id: "2")
    monkeypatch.setattr(sync, "_pull_new_mcp_token_files", lambda agent_id: None)
    monkeypatch.setattr(sync, "_pull_cli_credentials", lambda agent_id: None)
    sync.ensure_profile(AGENT)
    assert sync._REVISIONS[AGENT] == "1"  # incomplete: revision not advanced
    assert clears == [False]
