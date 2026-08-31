"""Tests for the brain routes — owner surface over the memory-wiki vault (wiki/)."""

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from core.config import settings
from core.containers.workspace import Workspace

USER_ID = "user_test_abc"


def _auth(owner_id: str = USER_ID) -> AuthContext:
    """Build a minimal AuthContext for direct handler calls (personal mode)."""
    return AuthContext(user_id=owner_id)


@pytest.fixture(autouse=True)
def _local_environment(monkeypatch):
    """Force ENVIRONMENT=local so os.chown is skipped on macOS during tests."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch) -> Workspace:
    """Fresh Workspace rooted at tmp_path, patched into the brain router."""
    ws = Workspace(mount_path=str(tmp_path))
    monkeypatch.setattr("routers.brain.get_workspace", lambda: ws)
    return ws


@pytest.fixture()
def wiki_root(workspace: Workspace) -> Path:
    """A populated wiki vault: wiki/main/{index.md, topics/agents.md}."""
    root = workspace._mount / USER_ID / "wiki"
    (root / "main" / "topics").mkdir(parents=True)
    (root / "main" / "index.md").write_text("# Index\n", encoding="utf-8")
    (root / "main" / "topics" / "agents.md").write_text("# Agents\n", encoding="utf-8")
    return root


# ===========================================================================
# Path guards
# ===========================================================================


class TestBrainPathGuards:
    @pytest.mark.asyncio
    async def test_read_rejects_parent_traversal(self, workspace):
        from routers.brain import read_brain_file

        workspace.ensure_user_dir(USER_ID)
        (workspace._mount / USER_ID / "openclaw.json").write_text("{}", encoding="utf-8")
        with pytest.raises(HTTPException) as exc:
            await read_brain_file(path="../openclaw.json", auth=_auth())
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_read_rejects_absolute_path(self, workspace):
        from routers.brain import read_brain_file

        workspace.ensure_user_dir(USER_ID)
        with pytest.raises(HTTPException) as exc:
            await read_brain_file(path="/etc/passwd", auth=_auth())
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_read_rejects_nul_byte(self, workspace):
        from routers.brain import read_brain_file

        workspace.ensure_user_dir(USER_ID)
        with pytest.raises(HTTPException) as exc:
            await read_brain_file(path="foo\x00bar.md", auth=_auth())
        assert exc.value.status_code in (400, 403)

    @pytest.mark.asyncio
    async def test_write_rejects_parent_traversal(self, workspace):
        from routers.brain import BrainWriteRequest, write_brain_file

        workspace.ensure_user_dir(USER_ID)
        body = BrainWriteRequest(path="../openclaw.json", content="hacked")
        with pytest.raises(HTTPException) as exc:
            await write_brain_file(body=body, auth=_auth())
        assert exc.value.status_code == 400
        assert not (workspace._mount / USER_ID / "openclaw.json").exists()

    @pytest.mark.asyncio
    async def test_delete_rejects_parent_traversal(self, workspace):
        from routers.brain import delete_brain_file

        workspace.ensure_user_dir(USER_ID)
        target = workspace._mount / USER_ID / "openclaw.json"
        target.write_text("{}", encoding="utf-8")
        with pytest.raises(HTTPException) as exc:
            await delete_brain_file(path="../openclaw.json", auth=_auth())
        assert exc.value.status_code == 400
        assert target.exists()

    @pytest.mark.asyncio
    async def test_read_rejects_symlink_escape(self, workspace, wiki_root):
        """A symlink inside wiki/ pointing outside is rejected even without '..'."""
        import os

        from routers.brain import read_brain_file

        secret = workspace._mount / USER_ID / "openclaw.json"
        secret.write_text('{"secret": true}', encoding="utf-8")
        os.symlink("../openclaw.json", wiki_root / "evil.json")
        with pytest.raises(HTTPException) as exc:
            await read_brain_file(path="evil.json", auth=_auth())
        assert exc.value.status_code == 403


# ===========================================================================
# Tree
# ===========================================================================


class TestBrainTree:
    @pytest.mark.asyncio
    async def test_tree_empty_when_no_vault(self, workspace):
        """Missing wiki/ dir (plugin never wrote) returns an empty tree, not 404/500."""
        from routers.brain import list_brain_tree

        workspace.ensure_user_dir(USER_ID)
        tree = await list_brain_tree(auth=_auth())
        assert tree == {"files": []}

    @pytest.mark.asyncio
    async def test_tree_lists_nested_files_with_relative_paths(self, workspace, wiki_root):
        from routers.brain import list_brain_tree

        tree = await list_brain_tree(auth=_auth())
        paths = {f["path"] for f in tree["files"]}
        assert "main" in paths
        assert "main/index.md" in paths
        assert "main/topics" in paths
        assert "main/topics/agents.md" in paths
        # Paths are wiki-relative — never user-root-relative
        assert not any(p.startswith("wiki") for p in paths)
        entry = next(f for f in tree["files"] if f["path"] == "main/index.md")
        assert entry["type"] == "file"
        assert isinstance(entry["size"], int)
        assert isinstance(entry["modified_at"], float)

    @pytest.mark.asyncio
    async def test_tree_hides_only_our_lock_sidecars(self, workspace, wiki_root):
        """Only `.{name}.lb-lock` FILES are hidden — agent-written *.lock
        content and directories that happen to share the suffix stay visible."""
        from routers.brain import list_brain_tree

        (wiki_root / "main" / ".index.md.lb-lock").write_text("", encoding="utf-8")
        (wiki_root / "main" / "report.md.lock").write_text("agent data", encoding="utf-8")
        weird_dir = wiki_root / "main" / "weird.lb-lock"
        weird_dir.mkdir()
        (weird_dir / "child.md").write_text("x", encoding="utf-8")

        tree = await list_brain_tree(auth=_auth())
        paths = {f["path"] for f in tree["files"]}
        assert "main/index.md" in paths
        assert "main/.index.md.lb-lock" not in paths
        assert "main/report.md.lock" in paths  # real vault content
        assert "main/weird.lb-lock" in paths  # a dir, never filtered
        assert "main/weird.lb-lock/child.md" in paths  # no orphaned children

    @pytest.mark.asyncio
    async def test_tree_skips_symlink_escaping_user_root(self, workspace, wiki_root, tmp_path):
        """A symlink resolving outside the user root is dropped; the rest lists."""
        import os

        from routers.brain import list_brain_tree

        outside = tmp_path / "outside-user-root.txt"
        outside.write_text("secret", encoding="utf-8")
        os.symlink(outside, wiki_root / "main" / "escape.txt")

        tree = await list_brain_tree(auth=_auth())
        paths = {f["path"] for f in tree["files"]}
        assert "main/escape.txt" not in paths
        assert "main/index.md" in paths
        assert "main/topics/agents.md" in paths

    @pytest.mark.asyncio
    async def test_tree_skips_dangling_symlink(self, workspace, wiki_root):
        """A dangling symlink skips that entry — never empties the whole tree."""
        import os

        from routers.brain import list_brain_tree

        os.symlink("never-existed.md", wiki_root / "main" / "dangling.md")

        tree = await list_brain_tree(auth=_auth())
        paths = {f["path"] for f in tree["files"]}
        assert "main/dangling.md" not in paths
        assert "main/index.md" in paths
        assert "main/topics/agents.md" in paths


# ===========================================================================
# File round-trip
# ===========================================================================


class TestBrainFileRoundTrip:
    @pytest.mark.asyncio
    async def test_write_then_read_creates_parents(self, workspace):
        """PUT creates parent dirs (vault may not exist yet); GET returns content."""
        from routers.brain import BrainWriteRequest, read_brain_file, write_brain_file

        workspace.ensure_user_dir(USER_ID)
        body = BrainWriteRequest(path="main/topics/new-note.md", content="# New\nfact")
        result = await write_brain_file(body=body, auth=_auth())
        assert result["path"] == "main/topics/new-note.md"
        assert result["size"] == len(b"# New\nfact")
        assert isinstance(result["modified_at"], float)
        on_disk = workspace._mount / USER_ID / "wiki" / "main" / "topics" / "new-note.md"
        assert on_disk.read_text() == "# New\nfact"

        info = await read_brain_file(path="main/topics/new-note.md", auth=_auth())
        assert info["content"] == "# New\nfact"
        assert info["binary"] is False
        assert info["path"] == "main/topics/new-note.md"

    @pytest.mark.asyncio
    async def test_read_404_when_absent(self, workspace):
        from routers.brain import read_brain_file

        workspace.ensure_user_dir(USER_ID)
        (workspace._mount / USER_ID / "wiki").mkdir()
        with pytest.raises(HTTPException) as exc:
            await read_brain_file(path="ghost.md", auth=_auth())
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_oversize_write_413(self, workspace):
        from routers.brain import BrainWriteRequest, write_brain_file

        workspace.ensure_user_dir(USER_ID)
        body = BrainWriteRequest(path="big.md", content="a" * (10 * 1024 * 1024 + 1))
        with pytest.raises(HTTPException) as exc:
            await write_brain_file(body=body, auth=_auth())
        assert exc.value.status_code == 413

    @pytest.mark.asyncio
    async def test_chown_called_after_write(self, workspace):
        """The container (uid 1000) must be able to read the owner's edit."""
        from routers.brain import BrainWriteRequest, write_brain_file

        workspace.ensure_user_dir(USER_ID)
        with patch.object(Workspace, "_chown_for_access_point") as chown:
            body = BrainWriteRequest(path="main/note.md", content="x")
            await write_brain_file(body=body, auth=_auth())
        assert chown.called

    @pytest.mark.asyncio
    async def test_write_takes_per_file_lock_sidecar(self, workspace, monkeypatch):
        """The write acquires the fcntl sidecar lock (spied), on `.{name}.lb-lock`."""
        import routers.brain as brain_mod
        from routers.brain import BrainWriteRequest, write_brain_file

        real_open_lock_fd = brain_mod.open_lock_fd
        acquired: list[str] = []

        def spy(lock_dir, lock_path):
            acquired.append(lock_path)
            return real_open_lock_fd(lock_dir, lock_path)

        monkeypatch.setattr(brain_mod, "open_lock_fd", spy)
        workspace.ensure_user_dir(USER_ID)
        body = BrainWriteRequest(path="main/note.md", content="x")
        await write_brain_file(body=body, auth=_auth())
        assert len(acquired) == 1
        assert acquired[0].endswith("/wiki/main/.note.md.lb-lock")
        assert (workspace._mount / USER_ID / "wiki" / "main" / ".note.md.lb-lock").exists()


# ===========================================================================
# Delete
# ===========================================================================


class TestBrainDelete:
    @pytest.mark.asyncio
    async def test_delete_file_204(self, workspace, wiki_root):
        from routers.brain import delete_brain_file

        res = await delete_brain_file(path="main/index.md", auth=_auth())
        assert res.status_code == 204
        assert not (wiki_root / "main" / "index.md").exists()

    @pytest.mark.asyncio
    async def test_delete_refuses_directory(self, workspace, wiki_root):
        from routers.brain import delete_brain_file

        with pytest.raises(HTTPException) as exc:
            await delete_brain_file(path="main/topics", auth=_auth())
        assert exc.value.status_code == 400
        assert (wiki_root / "main" / "topics").is_dir()

    @pytest.mark.asyncio
    async def test_delete_missing_is_idempotent_204(self, workspace):
        from routers.brain import delete_brain_file

        workspace.ensure_user_dir(USER_ID)
        res = await delete_brain_file(path="never-existed.md", auth=_auth())
        assert res.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_removes_only_its_own_lock_sidecar(self, workspace, wiki_root):
        """Deleting a note reclaims `.{name}.lb-lock` — never an agent-written
        `{name}.lock`, which is real vault content."""
        from routers.brain import delete_brain_file

        sidecar = wiki_root / "main" / ".index.md.lb-lock"
        sidecar.write_text("", encoding="utf-8")
        agent_lock = wiki_root / "main" / "index.md.lock"
        agent_lock.write_text("agent data", encoding="utf-8")

        await delete_brain_file(path="main/index.md", auth=_auth())
        assert not sidecar.exists()
        assert agent_lock.read_text() == "agent data"
