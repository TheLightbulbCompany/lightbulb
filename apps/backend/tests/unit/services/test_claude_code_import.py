import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from core.services import claude_code_import as cci


def _tar(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


SLUG = "-Users-me-repo"
CID = f"Users-me-repo-{hashlib.sha256(SLUG.encode()).hexdigest()[:10]}"

CLAUDE_HOME = {
    ".claude/CLAUDE.md": b"# me\n\n- prefers tabs\n",
    f".claude/projects/{SLUG}/memory/MEMORY.md": b"- [a](a.md)\n",
    f".claude/projects/{SLUG}/memory/fact.md": b"a fact\n",
    ".claude/skills/graphify/SKILL.md": b"---\nname: graphify\n---\n",
    ".claude/.credentials.json": b'{"token":"secret"}',
    ".claude/settings.local.json": b'{"key":"secret"}',
    f".claude/projects/{SLUG}/session.jsonl": b'{"type":"chat"}\n',
    ".claude/todos/x.json": b"{}",
    ".claude/commands/deploy.md": b"cmd\n",
}


def test_detect_dot_claude_prefix():
    assert cci.detect_claude_root(list(CLAUDE_HOME)) == ".claude/"


def test_detect_bare_layout():
    assert cci.detect_claude_root([n.removeprefix(".claude/") for n in CLAUDE_HOME]) == ""


def test_detect_rejects_real_backup_layout():
    names = [
        "20260101-openclaw-backup/manifest.json",
        "20260101-openclaw-backup/payload/posix/home/me/projects/x/memory/a.md",
    ]
    assert cci.detect_claude_root(names) is None


def test_detect_rejects_workspace_tar():
    # CLAUDE.md + skills WITHOUT project memory is a plain workspace shape
    assert cci.detect_claude_root(["CLAUDE.md", "skills/s/SKILL.md", "AGENTS.md"]) is None
    assert cci.detect_claude_root(["CLAUDE.md", "skills/s/SKILL.md"]) is None


def test_detect_accepts_the_dialogs_own_partial_archives():
    """The dialog says "include whichever exist": a ``.claude/``-prefixed archive with only
    CLAUDE.md, or only skills, is a Claude Code context."""
    assert cci.detect_claude_root([".claude/CLAUDE.md"]) == ".claude/"
    assert cci.detect_claude_root([".claude/skills/s/SKILL.md"]) == ".claude/"
    assert cci.detect_claude_root(["./.claude/CLAUDE.md", "./.claude/skills/s/SKILL.md"]) == ".claude/"


def test_detect_requires_md_under_memory():
    assert cci.detect_claude_root(["projects/x/memory/notes.txt"]) is None


def test_extract_allowlist(tmp_path: Path):
    raw = _tar(CLAUDE_HOME)
    norm = cci.extract_claude_archive(lambda: io.BytesIO(raw), tmp_path)
    assert norm.has_user_memory is True
    assert [(c.id, c.label, c.file_count) for c in norm.collections] == [(CID, SLUG, 2)]
    assert norm.skills == ["graphify"]
    assert (tmp_path / f"claude/memory/{CID}/fact.md").read_text() == "a fact\n"
    assert (tmp_path / "claude/CLAUDE.md").exists()
    assert (tmp_path / "claude/skills/graphify/SKILL.md").exists()
    staged = [str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()]
    assert not any(
        "credentials" in p or p.endswith(".jsonl") or "todos" in p or "settings" in p or "commands" in p for p in staged
    )
    assert norm.skipped["credentials"] == 2
    assert norm.skipped["transcripts"] == 1
    assert norm.skipped["commands"] == 1
    manifest = json.loads((tmp_path / "import-manifest.json").read_text())
    assert manifest["kind"] == "claude"
    assert manifest["collections"][0]["id"] == CID


def test_extract_flags_secrets_in_memory(tmp_path: Path):
    entries = dict(CLAUDE_HOME)
    entries[f".claude/projects/{SLUG}/memory/leak.md"] = b"key AKIAIOSFODNN7EXAMPLE\n"
    norm = cci.extract_claude_archive(lambda: io.BytesIO(_tar(entries)), tmp_path)
    assert any(f.pattern == "aws-secret-key" for f in norm.safety_flags)


def test_extract_traversal_member_aborts(tmp_path: Path):
    entries = dict(CLAUDE_HOME)
    entries[".claude/projects/x/memory/../../../evil.md"] = b"evil\n"
    with pytest.raises(cci.ClaudeImportError):
        cci.extract_claude_archive(lambda: io.BytesIO(_tar(entries)), tmp_path)
    assert not (tmp_path.parent / "evil.md").exists()


def test_extract_symlink_member_skipped(tmp_path: Path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in CLAUDE_HOME.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(f".claude/projects/{SLUG}/memory/link.md")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    norm = cci.extract_claude_archive(lambda: io.BytesIO(buf.getvalue()), tmp_path)
    assert norm.skipped["links"] == 1
    assert not (tmp_path / f"claude/memory/{CID}/link.md").exists()


def test_extract_oversized_md_skipped(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cci, "CLAUDE_MAX_MD_BYTES", 4)
    norm = cci.extract_claude_archive(lambda: io.BytesIO(_tar(CLAUDE_HOME)), tmp_path)
    assert norm.skipped["too_large"] >= 2


def test_extract_memory_cap(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cci, "CLAUDE_MEMORY_MAX_FILES", 1)
    with pytest.raises(cci.ClaudeImportError):
        cci.extract_claude_archive(lambda: io.BytesIO(_tar(CLAUDE_HOME)), tmp_path)


def _staged(tmp_path: Path) -> Path:
    staging = tmp_path / "workspace-import-abc"
    (staging / "claude/memory/projA-1234567890").mkdir(parents=True)
    (staging / "claude/memory/projA-1234567890/fact.md").write_text("fact\n")
    (staging / "claude/skills/graphify").mkdir(parents=True)
    (staging / "claude/skills/graphify/SKILL.md").write_text("skill\n")
    (staging / "claude/CLAUDE.md").write_text("global memory\n")
    return staging


def test_apply_copies_and_appends(tmp_path: Path):
    ws = tmp_path / "workspace-agent1"
    ws.mkdir()
    results = cci.apply_to_workspace(_staged(tmp_path), ws, None, None)
    assert (ws / "memory/imports/claude-code/projA-1234567890/fact.md").read_text() == "fact\n"
    assert (ws / "skills/graphify/SKILL.md").exists()
    # CLAUDE.md is the caller's to merge (context_import); nothing here writes USER.md.
    assert not (ws / "USER.md").exists()
    assert {r["status"] for r in results} == {"applied"}


def test_apply_memory_reimport_overwrites(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    staging = _staged(tmp_path)
    cci.apply_to_workspace(staging, ws, None, None)
    (staging / "claude/memory/projA-1234567890/fact.md").write_text("fresher\n")
    cci.apply_to_workspace(staging, ws, None, None)
    assert (ws / "memory/imports/claude-code/projA-1234567890/fact.md").read_text() == "fresher\n"


def test_apply_skill_conflict_skipped(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "skills/graphify").mkdir(parents=True)
    (ws / "skills/graphify/SKILL.md").write_text("mine\n")
    results = cci.apply_to_workspace(_staged(tmp_path), ws, None, None)
    skill = next(r for r in results if r["item"] == "skill:graphify")
    assert skill["status"] == "skipped_exists"
    assert (ws / "skills/graphify/SKILL.md").read_text() == "mine\n"


def test_apply_selection_filters(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    results = cci.apply_to_workspace(_staged(tmp_path), ws, [], [])
    assert not (ws / "memory").exists()
    assert not (ws / "skills").exists()
    assert results == []
