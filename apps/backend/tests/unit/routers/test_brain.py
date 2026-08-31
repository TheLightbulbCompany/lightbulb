"""Tests for the brain routes — owner surface over the memory-wiki vault (wiki/)."""

import io
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient

from core.auth import AuthContext
from core.config import settings
from core.containers.workspace import Workspace

USER_ID = "user_test_abc"


def _auth(owner_id: str = USER_ID) -> AuthContext:
    """Build a minimal AuthContext for direct handler calls (personal mode)."""
    return AuthContext(user_id=owner_id)


def _upload(name: str, data: bytes) -> UploadFile:
    """Build an UploadFile for a direct handler call (no TestClient/multipart)."""
    return UploadFile(io.BytesIO(data), filename=name)


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
    async def test_write_400_when_parent_component_is_a_file(self, workspace, wiki_root):
        """F7: PUT "main/index.md/child.md" when "main/index.md" already
        exists as a file — open_lock_fd's os.makedirs raises a raw
        FileExistsError; must 400, not an unhandled 500."""
        from routers.brain import BrainWriteRequest, write_brain_file

        body = BrainWriteRequest(path="main/index.md/child.md", content="x")
        with pytest.raises(HTTPException) as exc:
            await write_brain_file(body=body, auth=_auth())
        assert exc.value.status_code == 400
        assert (wiki_root / "main" / "index.md").read_text() == "# Index\n"

    @pytest.mark.asyncio
    async def test_write_400_when_target_is_a_directory(self, workspace, wiki_root):
        """F7: PUT whose path names an existing directory — os.replace raises
        IsADirectoryError (wrapped as WorkspaceError); must 400, not 500."""
        from routers.brain import BrainWriteRequest, write_brain_file

        body = BrainWriteRequest(path="main", content="x")
        with pytest.raises(HTTPException) as exc:
            await write_brain_file(body=body, auth=_auth())
        assert exc.value.status_code == 400
        assert (wiki_root / "main").is_dir()

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
    async def test_delete_takes_per_file_lock_sidecar(self, workspace, wiki_root, monkeypatch):
        """F4: delete must take the same fcntl sidecar layer as write — the
        module docstring's "serializes BACKEND writers" claim otherwise frays
        on delete in the two-instance deploy window. Spied the same way as
        test_write_takes_per_file_lock_sidecar."""
        import routers.brain as brain_mod
        from routers.brain import delete_brain_file

        real_open_lock_fd = brain_mod.open_lock_fd
        acquired: list[str] = []

        def spy(lock_dir, lock_path):
            acquired.append(lock_path)
            return real_open_lock_fd(lock_dir, lock_path)

        monkeypatch.setattr(brain_mod, "open_lock_fd", spy)
        await delete_brain_file(path="main/index.md", auth=_auth())
        assert len(acquired) == 1
        assert acquired[0].endswith("/wiki/main/.index.md.lb-lock")
        # The sidecar is reclaimed once the delete completes — no orphan left.
        assert not (wiki_root / "main" / ".index.md.lb-lock").exists()

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


# ===========================================================================
# Upload (drop-zone)
# ===========================================================================


class TestBrainUpload:
    @pytest.mark.asyncio
    async def test_upload_lands_under_wiki_inbox(self, workspace):
        from routers.brain import upload_brain_files

        workspace.ensure_user_dir(USER_ID)
        result = await upload_brain_files(files=[_upload("notes.md", b"hello")], auth=_auth())
        assert result["uploaded"] == [{"filename": "notes.md", "path": "inbox/notes.md", "size": 5}]
        on_disk = workspace._mount / USER_ID / "wiki" / "inbox" / "notes.md"
        assert on_disk.read_bytes() == b"hello"

    @pytest.mark.asyncio
    async def test_upload_collision_gets_dash_one_suffix(self, workspace, wiki_root):
        from routers.brain import upload_brain_files

        (wiki_root / "inbox").mkdir()
        (wiki_root / "inbox" / "notes.md").write_text("existing", encoding="utf-8")

        result = await upload_brain_files(files=[_upload("notes.md", b"new")], auth=_auth())

        assert result["uploaded"][0]["path"] == "inbox/notes-1.md"
        assert (wiki_root / "inbox" / "notes.md").read_text() == "existing"
        assert (wiki_root / "inbox" / "notes-1.md").read_bytes() == b"new"

    @pytest.mark.asyncio
    async def test_upload_second_collision_gets_dash_two_suffix(self, workspace, wiki_root):
        from routers.brain import upload_brain_files

        (wiki_root / "inbox").mkdir()
        (wiki_root / "inbox" / "notes.md").write_text("a", encoding="utf-8")
        (wiki_root / "inbox" / "notes-1.md").write_text("b", encoding="utf-8")

        result = await upload_brain_files(files=[_upload("notes.md", b"new")], auth=_auth())

        assert result["uploaded"][0]["path"] == "inbox/notes-2.md"

    @pytest.mark.asyncio
    async def test_upload_multiple_files_in_one_request(self, workspace):
        from routers.brain import upload_brain_files

        workspace.ensure_user_dir(USER_ID)
        result = await upload_brain_files(files=[_upload("a.md", b"1"), _upload("b.md", b"22")], auth=_auth())
        paths = {u["path"] for u in result["uploaded"]}
        assert paths == {"inbox/a.md", "inbox/b.md"}

    @pytest.mark.asyncio
    async def test_upload_rejects_too_many_files(self, workspace):
        from routers.brain import upload_brain_files

        workspace.ensure_user_dir(USER_ID)
        files = [_upload(f"f{i}.md", b"x") for i in range(11)]
        with pytest.raises(HTTPException) as exc:
            await upload_brain_files(files=files, auth=_auth())
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_upload_rejects_oversize_file(self, workspace):
        from routers.brain import upload_brain_files

        workspace.ensure_user_dir(USER_ID)
        big = b"a" * (10 * 1024 * 1024 + 1)
        with pytest.raises(HTTPException) as exc:
            await upload_brain_files(files=[_upload("big.md", big)], auth=_auth())
        assert exc.value.status_code == 400
        assert not (workspace._mount / USER_ID / "wiki" / "inbox" / "big.md").exists()

    @pytest.mark.asyncio
    async def test_upload_chown_called(self, workspace):
        """The container (uid 1000) must be able to read owner-dropped files."""
        from routers.brain import upload_brain_files

        workspace.ensure_user_dir(USER_ID)
        with patch.object(Workspace, "_chown_for_access_point") as chown:
            await upload_brain_files(files=[_upload("a.md", b"x")], auth=_auth())
        assert chown.called

    @pytest.mark.asyncio
    async def test_upload_guard_runs_before_mutation_on_symlinked_wiki(self, workspace):
        """F3: a compromised container could plant `wiki` as a symlink
        escaping the user root. _ensure_within_subtree alone doesn't catch
        this (it resolves the subtree root through the same symlink) — the
        actual content write is still refused by _resolve_user_file's
        user-root pin (500), but that guard must run BEFORE any mkdir/lock-
        file mutation: no inbox dir or lock sidecar may land in the escaped
        (other tenant's) directory."""
        import os

        from routers.brain import upload_brain_files

        workspace.ensure_user_dir(USER_ID)
        other_owner_dir = workspace._mount / "other_owner"
        other_owner_dir.mkdir()
        os.symlink(other_owner_dir, workspace._mount / USER_ID / "wiki")

        with pytest.raises(HTTPException) as exc:
            await upload_brain_files(files=[_upload("notes.md", b"hello")], auth=_auth())
        assert exc.value.status_code == 500

        # No mutation landed inside the escaped directory: no inbox/, no
        # lock sidecar — the guard ran before either mkdir call.
        assert list(other_owner_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_upload_sanitizes_traversal_in_filename(self, workspace):
        """A path-y filename is reduced to its basename — no escaping inbox/."""
        from routers.brain import upload_brain_files

        workspace.ensure_user_dir(USER_ID)
        result = await upload_brain_files(files=[_upload("../../evil.md", b"x")], auth=_auth())
        assert result["uploaded"][0]["path"] == "inbox/evil.md"
        assert (workspace._mount / USER_ID / "wiki" / "inbox" / "evil.md").exists()
        assert not (workspace._mount / "evil.md").exists()


# ===========================================================================
# Upload hardening — T3 review round 1
# ===========================================================================


class TestBrainUploadHardening:
    @pytest.mark.asyncio
    async def test_upload_rejects_dot_filename_400_and_writes_nothing(self, workspace):
        """T3 review F1: a lone '.' must never land on wiki/inbox itself."""
        from routers.brain import upload_brain_files

        workspace.ensure_user_dir(USER_ID)
        with pytest.raises(HTTPException) as exc:
            await upload_brain_files(files=[_upload(".", b"x")], auth=_auth())
        assert exc.value.status_code == 400
        inbox = workspace._mount / USER_ID / "wiki" / "inbox"
        # The bug: os.replace(tmp, inbox) turned the directory into a file.
        # Rejected before any fs work runs, so inbox/ isn't even created yet.
        assert not inbox.exists()

        # And the drop-zone survives to serve a normal upload right after —
        # the original bug bricked every later upload once `inbox` became a file.
        result = await upload_brain_files(files=[_upload("notes.md", b"hello")], auth=_auth())
        assert result["uploaded"][0]["path"] == "inbox/notes.md"

    @pytest.mark.asyncio
    async def test_upload_rejects_dotdot_filename_400(self, workspace):
        from routers.brain import upload_brain_files

        workspace.ensure_user_dir(USER_ID)
        with pytest.raises(HTTPException) as exc:
            await upload_brain_files(files=[_upload("..", b"x")], auth=_auth())
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_upload_rejects_empty_filename_400(self, workspace):
        """An empty original name falls back to '_sanitize_filename's "upload"
        default today — this covers the case where sanitization itself could
        ever yield an empty string (e.g. an all-unsafe-char name reduced to
        nothing)."""
        from routers.brain import _validate_upload_filename

        with pytest.raises(HTTPException) as exc:
            _validate_upload_filename("")
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_upload_rejects_oversize_filename_400_not_500(self, workspace):
        """T3 review F2/M2: a 300-byte filename must 400, never ENAMETOOLONG-500."""
        from routers.brain import upload_brain_files

        workspace.ensure_user_dir(USER_ID)
        long_name = ("a" * 297) + ".md"  # 300 bytes total
        assert len(long_name.encode("utf-8")) == 300
        with pytest.raises(HTTPException) as exc:
            await upload_brain_files(files=[_upload(long_name, b"x")], auth=_auth())
        assert exc.value.status_code == 400
        assert not (workspace._mount / USER_ID / "wiki" / "inbox" / long_name).exists()

    @pytest.mark.asyncio
    async def test_upload_rejects_trailing_newline_filename(self, workspace):
        """T3 review F4: a trailing '\\n' survives `_sanitize_filename` (its
        regex uses '$', which matches before a trailing newline) — reject it
        explicitly rather than let it into a filename and a log line."""
        from routers.brain import upload_brain_files

        workspace.ensure_user_dir(USER_ID)
        with pytest.raises(HTTPException) as exc:
            await upload_brain_files(files=[_upload("notes.md\n", b"x")], auth=_auth())
        assert exc.value.status_code == 400
        assert not (workspace._mount / USER_ID / "wiki" / "inbox" / "notes.md\n").exists()

    @pytest.mark.asyncio
    async def test_upload_rejects_lb_lock_suffix_filename(self, workspace):
        """F8: a name ending `.lb-lock` would upload 200, but the tree filter
        (_strip_wiki_prefix) hides any `.{name}.lb-lock` file — the owner
        would never see it landed."""
        from routers.brain import upload_brain_files

        workspace.ensure_user_dir(USER_ID)
        with pytest.raises(HTTPException) as exc:
            await upload_brain_files(files=[_upload(".notes.md.lb-lock", b"x")], auth=_auth())
        assert exc.value.status_code == 400
        assert not (workspace._mount / USER_ID / "wiki" / "inbox" / ".notes.md.lb-lock").exists()

    @pytest.mark.asyncio
    async def test_concurrent_same_name_uploads_both_survive_with_distinct_names(self, workspace):
        """T3 review M3 (controller ruling): concurrent uploads of the same
        filename must never race the dedupe probe — both survive with
        distinct names and intact bytes, serialized under the one inbox-wide
        lock (_locked_inbox_upload), never the previous outside-any-lock
        probe-then-write shape that let both resolve to "notes.md" and one
        payload silently vanish."""
        import asyncio

        from routers.brain import upload_brain_files

        workspace.ensure_user_dir(USER_ID)

        async def _do_upload(payload: bytes) -> dict:
            r = await upload_brain_files(files=[_upload("notes.md", payload)], auth=_auth())
            return r["uploaded"][0]

        results = await asyncio.gather(_do_upload(b"AAA"), _do_upload(b"BBB"))

        paths = sorted(u["path"] for u in results)
        assert paths == ["inbox/notes-1.md", "inbox/notes.md"]
        inbox = workspace._mount / USER_ID / "wiki" / "inbox"
        contents = {(inbox / "notes.md").read_bytes(), (inbox / "notes-1.md").read_bytes()}
        assert contents == {b"AAA", b"BBB"}


class TestBrainUploadHttp:
    """One real end-to-end request through the FastAPI app (T3 review F8) —
    proves the route is mounted at /api/v1/container/brain/upload, that
    File(...) binds the `files` field, and that auth is wired, none of which
    the direct-handler-call tests above can prove by themselves."""

    @pytest.fixture()
    def client(self, workspace):
        from core.auth import get_current_user
        from main import app

        async def _override():
            return _auth()

        app.dependency_overrides[get_current_user] = _override
        try:
            with TestClient(app) as c:
                yield c
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    def test_upload_via_real_multipart_request(self, client, workspace):
        workspace.ensure_user_dir(USER_ID)
        response = client.post(
            "/api/v1/container/brain/upload",
            headers={"Authorization": "Bearer test"},
            files=[("files", ("notes.md", b"hello", "text/markdown"))],
        )
        assert response.status_code == 200
        assert response.json() == {"uploaded": [{"filename": "notes.md", "path": "inbox/notes.md", "size": 5}]}
        on_disk = workspace._mount / USER_ID / "wiki" / "inbox" / "notes.md"
        assert on_disk.read_bytes() == b"hello"
