"""Tests for Workspace.list_directory() and Workspace.read_file_info()."""

import base64
import struct
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from core.config import settings
from core.containers.workspace import Workspace, WorkspaceError

USER_ID = "user_test_abc"
AGENT_ID = "agent-abc-123"


def _auth(owner_id: str = USER_ID) -> AuthContext:
    """Build an active workspace context for direct handler calls."""
    return AuthContext(user_id=f"human_{owner_id}", org_id=owner_id, org_role="org:admin")


@pytest.fixture(autouse=True)
def _ecs_owner_by_default():
    """Every route now checks ``runtime_transport.is_agentcore_owner`` (a real DynamoDB read when
    unmocked) before touching EFS. Default the whole module to the ECS runtime -- these tests are
    the EFS contract, which must stay byte-for-byte unchanged; ``TestAgentCoreProfileBranch``
    below opts in."""
    with patch("routers.workspace_files.runtime_transport.is_agentcore_owner", AsyncMock(return_value=False)):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Workspace:
    """Return a Workspace rooted at tmp_path."""
    return Workspace(mount_path=str(tmp_path))


def _user_root(tmp_path: Path) -> Path:
    return tmp_path / USER_ID


def _minimal_png() -> bytes:
    """Return a valid 1×1 white PNG as bytes."""
    # Build raw IDAT data: one filter byte + one RGB pixel (white)
    raw = b"\x00\xff\xff\xff"
    compressed = zlib.compress(raw)

    def chunk(name: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)
        return length + name + data + crc

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )
    return png


def _minimal_pdf() -> bytes:
    """Return a tiny syntactically-valid (single blank page) PDF as bytes."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 1 1]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
        b"%%EOF"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_environment(monkeypatch):
    """Force ENVIRONMENT=local so os.chown is skipped on macOS during tests."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")


@pytest.fixture()
def workspace(tmp_path: Path) -> Workspace:
    """Fresh Workspace using a tmp directory."""
    return _make_workspace(tmp_path)


@pytest.fixture()
def populated_workspace(tmp_path: Path) -> tuple[Workspace, Path]:
    """Workspace pre-populated with sample files and dirs.

    Layout under {tmp_path}/{USER_ID}/:
        agents/
            my-agent/
                notes.md   (text)
                data.bin   (binary, non-image)
        docs/
            readme.txt     (text)
        image.png          (1×1 PNG)
        doc.pdf            (tiny single-page PDF)
        script.py          (text)
        openclaw.json      (visible — paying users see their own config)
        .hidden_file       (visible — agent-managed dotfiles are no longer hidden)
        node_modules/      (excluded — build artifact)
        __pycache__/       (excluded — build artifact)
    """
    ws = _make_workspace(tmp_path)
    root = tmp_path / USER_ID

    # Visible files/dirs
    (root / "agents" / "my-agent").mkdir(parents=True)
    (root / "agents" / "my-agent" / "notes.md").write_text("# Notes\nHello!", encoding="utf-8")
    (root / "agents" / "my-agent" / "data.bin").write_bytes(b"\x00\x01\x02\x03")
    (root / "docs").mkdir()
    (root / "docs" / "readme.txt").write_text("Hello world", encoding="utf-8")
    (root / "image.png").write_bytes(_minimal_png())
    (root / "doc.pdf").write_bytes(_minimal_pdf())
    (root / "script.py").write_text('print("hi")\n', encoding="utf-8")
    (root / "build.lobster").write_text("start -> build -> done\n", encoding="utf-8")

    # Visible-but-was-previously-hidden items
    (root / "openclaw.json").write_text("{}", encoding="utf-8")
    (root / ".hidden_file").write_text("secret", encoding="utf-8")
    # Excluded items (build artifacts only)
    (root / "node_modules").mkdir()
    (root / "__pycache__").mkdir()

    return ws, root


# ===========================================================================
# TestListDirectory
# ===========================================================================


class TestListDirectory:
    def test_list_agent_root(self, populated_workspace):
        """list_directory on user root returns expected visible entries.

        After the 2026-05-10 filter trim, dotfiles and ``openclaw.json`` are
        no longer suppressed — only build artifacts and VCS metadata are.
        """
        ws, root = populated_workspace
        entries = ws.list_directory(USER_ID, "")
        names = {e["name"] for e in entries}
        # Visible items — including dotfiles + openclaw.json now
        assert "agents" in names
        assert "docs" in names
        assert "image.png" in names
        assert "script.py" in names
        assert "openclaw.json" in names
        assert ".hidden_file" in names
        # Excluded items: build artifacts only
        assert "node_modules" not in names
        assert "__pycache__" not in names

    def test_list_shows_openclaw_runtime_dirs(self, tmp_path):
        """OpenClaw runtime dirs (state/, skills/, canvas/, identity/, memory/)
        are visible in the tree — we don't proactively hide them.

        Rationale: hiding only at the workspace root would create a
        depth-dependent exclusion that's easy to misapply. Users seeing
        OpenClaw's runtime dirs isn't harmful; if clutter becomes a real
        problem, revisit.
        """
        ws = _make_workspace(tmp_path)
        root = tmp_path / USER_ID / "workspace-main"
        root.mkdir(parents=True)
        (root / "SOUL.md").write_text("soul", encoding="utf-8")
        (root / "memory").mkdir()
        (root / "state").mkdir()
        (root / "skills").mkdir()
        (root / "canvas").mkdir()
        (root / "identity").mkdir()

        entries = ws.list_directory(USER_ID, "workspace-main")
        names = {e["name"] for e in entries}
        assert "SOUL.md" in names
        assert "memory" in names
        assert "state" in names
        assert "skills" in names
        assert "canvas" in names
        assert "identity" in names

    def test_list_subdirectory(self, populated_workspace):
        """list_directory on a sub-path works correctly."""
        ws, root = populated_workspace
        entries = ws.list_directory(USER_ID, "agents/my-agent")
        names = {e["name"] for e in entries}
        assert "notes.md" in names
        assert "data.bin" in names

    def test_entry_fields_file(self, populated_workspace):
        """File entry has required fields with correct types."""
        ws, root = populated_workspace
        entries = ws.list_directory(USER_ID, "docs")
        assert len(entries) == 1
        entry = entries[0]
        assert entry["name"] == "readme.txt"
        assert entry["type"] == "file"
        assert isinstance(entry["size"], int)
        assert entry["size"] > 0
        assert isinstance(entry["modified_at"], float)
        assert entry["path"] == "docs/readme.txt"

    def test_dirs_have_null_size(self, populated_workspace):
        """Directory entries have size=None."""
        ws, root = populated_workspace
        entries = ws.list_directory(USER_ID, "")
        dir_entries = [e for e in entries if e["type"] == "dir"]
        assert all(e["size"] is None for e in dir_entries)

    def test_sort_order_dirs_first(self, populated_workspace):
        """Directories appear before files; ties broken alphabetically."""
        ws, root = populated_workspace
        entries = ws.list_directory(USER_ID, "")
        types = [e["type"] for e in entries]
        # All dirs must precede all files
        seen_file = False
        for t in types:
            if t == "file":
                seen_file = True
            else:
                assert not seen_file, "A dir appeared after a file"

    def test_sort_order_alphabetical_within_group(self, populated_workspace):
        """Within each group (dirs / files) entries are alphabetically sorted."""
        ws, root = populated_workspace
        entries = ws.list_directory(USER_ID, "")
        dir_names = [e["name"].lower() for e in entries if e["type"] == "dir"]
        file_names = [e["name"].lower() for e in entries if e["type"] == "file"]
        assert dir_names == sorted(dir_names)
        assert file_names == sorted(file_names)

    def test_path_traversal_blocked(self, populated_workspace):
        """Traversal outside the user root raises WorkspaceError."""
        ws, _ = populated_workspace
        with pytest.raises(WorkspaceError):
            ws.list_directory(USER_ID, "../../etc")

    def test_nonexistent_directory(self, workspace):
        """Listing a path that does not exist raises WorkspaceError."""
        (workspace._mount / USER_ID).mkdir(parents=True, exist_ok=True)
        with pytest.raises(WorkspaceError):
            workspace.list_directory(USER_ID, "nonexistent_dir")

    def test_excludes_system_files(self, populated_workspace):
        """All entries in _EXCLUDED_NAMES are hidden from listings."""
        from core.containers.workspace import _EXCLUDED_NAMES

        ws, root = populated_workspace
        entries = ws.list_directory(USER_ID, "")
        names = {e["name"] for e in entries}
        for excluded in _EXCLUDED_NAMES:
            assert excluded not in names, f"{excluded!r} should be excluded"

    def test_path_is_relative_to_user_root(self, populated_workspace):
        """Entry 'path' values are relative to the user's workspace root."""
        ws, root = populated_workspace
        entries = ws.list_directory(USER_ID, "agents/my-agent")
        for entry in entries:
            assert not entry["path"].startswith("/"), "path must be relative"
            assert entry["path"].startswith("agents/my-agent/")

    def test_iterdir_oserror_raises_workspace_error(self, populated_workspace):
        """OSError during iterdir() is wrapped in WorkspaceError."""
        ws, _ = populated_workspace
        with patch.object(Path, "iterdir", side_effect=OSError("NFS stale handle")):
            with pytest.raises(WorkspaceError, match="Failed to list directory"):
                ws.list_directory(USER_ID, "")


# ===========================================================================
# TestReadFileInfo
# ===========================================================================


class TestReadFileInfo:
    def test_read_text_file(self, populated_workspace):
        """Text file is returned with content as a UTF-8 string."""
        ws, _ = populated_workspace
        info = ws.read_file_info(USER_ID, "docs/readme.txt")
        assert info["name"] == "readme.txt"
        assert info["binary"] is False
        assert info["content"] == "Hello world"
        assert info["size"] == len("Hello world".encode())
        assert isinstance(info["modified_at"], float)
        assert info["path"] == "docs/readme.txt"

    def test_read_text_file_mime_type(self, populated_workspace):
        """Text file has a recognisable mime_type or None (acceptable)."""
        ws, _ = populated_workspace
        info = ws.read_file_info(USER_ID, "script.py")
        assert info["binary"] is False
        assert info["content"] == 'print("hi")\n'
        # mime_type may vary by platform but should not raise

    def test_read_image_file(self, populated_workspace):
        """Image file is returned base64-encoded with binary=True."""
        ws, _ = populated_workspace
        info = ws.read_file_info(USER_ID, "image.png")
        assert info["name"] == "image.png"
        assert info["binary"] is True
        assert info["mime_type"] == "image/png"
        assert info["content"] is not None
        # Verify it round-trips to valid PNG bytes
        decoded = base64.b64decode(info["content"])
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n"

    def test_read_pdf_file(self, populated_workspace):
        """PDF is returned base64-encoded with binary=True, like images —
        the frontend points its native PDF renderer at it via an object URL.
        """
        ws, _ = populated_workspace
        info = ws.read_file_info(USER_ID, "doc.pdf")
        assert info["name"] == "doc.pdf"
        assert info["binary"] is True
        assert info["mime_type"] == "application/pdf"
        assert info["content"] is not None
        decoded = base64.b64decode(info["content"])
        assert decoded.startswith(b"%PDF-")

    def test_read_binary_non_image(self, populated_workspace):
        """Non-image binary file returns content=None, binary=True."""
        ws, _ = populated_workspace
        info = ws.read_file_info(USER_ID, "agents/my-agent/data.bin")
        assert info["binary"] is True
        assert info["content"] is None

    def test_path_traversal_blocked(self, populated_workspace):
        """Traversal outside the user root raises WorkspaceError."""
        ws, _ = populated_workspace
        with pytest.raises(WorkspaceError):
            ws.read_file_info(USER_ID, "../../etc/passwd")

    def test_nonexistent_file(self, workspace):
        """Reading a non-existent path raises WorkspaceError."""
        (workspace._mount / USER_ID).mkdir(parents=True, exist_ok=True)
        with pytest.raises(WorkspaceError):
            workspace.read_file_info(USER_ID, "ghost.txt")

    def test_directory_raises(self, populated_workspace):
        """Passing a directory path raises WorkspaceError (not a file)."""
        ws, _ = populated_workspace
        with pytest.raises(WorkspaceError):
            ws.read_file_info(USER_ID, "docs")

    def test_markdown_file(self, populated_workspace):
        """Markdown (.md) is treated as text."""
        ws, _ = populated_workspace
        info = ws.read_file_info(USER_ID, "agents/my-agent/notes.md")
        assert info["binary"] is False
        assert "Notes" in info["content"]

    def test_lobster_file_is_text(self, populated_workspace):
        """.lobster workflows are text so the frontend graph viewer gets content.

        Without this the file falls through to the binary branch and the
        workspace viewer shows "Binary file — preview not available," which
        makes the .lobster Graph/Code tabs unreachable.
        """
        ws, _ = populated_workspace
        info = ws.read_file_info(USER_ID, "build.lobster")
        assert info["binary"] is False
        assert "build" in info["content"]

    def test_result_path_is_relative(self, populated_workspace):
        """'path' field in result is relative to user workspace root."""
        ws, _ = populated_workspace
        info = ws.read_file_info(USER_ID, "script.py")
        assert not info["path"].startswith("/")
        assert info["path"] == "script.py"

    def test_non_utf8_text_extension_falls_back_to_binary(self, tmp_path):
        """A .py file with non-UTF-8 bytes falls through to binary result."""
        ws = _make_workspace(tmp_path)
        root = tmp_path / USER_ID
        root.mkdir(parents=True)
        # Write raw bytes that are not valid UTF-8.
        (root / "bad.py").write_bytes(b"\x80\x81\x82\xff")
        info = ws.read_file_info(USER_ID, "bad.py")
        assert info["binary"] is True
        assert info["content"] is None

    def test_mime_type_fallback(self, populated_workspace):
        """Unknown extension gets application/octet-stream instead of None."""
        ws, root = populated_workspace
        info = ws.read_file_info(USER_ID, "agents/my-agent/data.bin")
        assert info["mime_type"] == "application/octet-stream"

    def test_known_mime_type_preserved(self, populated_workspace):
        """Known extension retains its real mime_type."""
        ws, _ = populated_workspace
        info = ws.read_file_info(USER_ID, "image.png")
        assert info["mime_type"] == "image/png"


# ===========================================================================
# TestConfigFilesEndpoint / TestConfigFileReadEndpoint
# ===========================================================================


class TestConfigFilesEndpoint:
    """Tests for GET /workspace/{agent_id}/config-files."""

    def test_returns_only_allowlisted_files(self, tmp_path):
        """Only allowlisted files that exist on disk are returned."""
        ws = _make_workspace(tmp_path)
        agent_dir = tmp_path / USER_ID / f"workspace-{AGENT_ID}"
        agent_dir.mkdir(parents=True)
        (agent_dir / "SOUL.md").write_text("I am helpful", encoding="utf-8")
        (agent_dir / "MEMORY.md").write_text("Remember this", encoding="utf-8")
        (agent_dir / "sessions").mkdir()  # should be excluded
        (agent_dir / "secret.json").write_text("{}", encoding="utf-8")  # not allowlisted

        from routers.workspace_files import _list_config_files

        result = _list_config_files(ws, USER_ID, AGENT_ID)
        names = {f["name"] for f in result}
        assert names == {"SOUL.md", "MEMORY.md"}
        assert all(f["type"] == "file" for f in result)

    def test_empty_when_no_agent_dir(self, tmp_path):
        """Returns empty list when agent dir doesn't exist."""
        ws = _make_workspace(tmp_path)
        (tmp_path / USER_ID).mkdir(parents=True)
        from routers.workspace_files import _list_config_files

        result = _list_config_files(ws, USER_ID, AGENT_ID)
        assert result == []

    def test_file_entries_have_required_fields(self, tmp_path):
        """Each entry has name, path, type, size, modified_at."""
        ws = _make_workspace(tmp_path)
        agent_dir = tmp_path / USER_ID / f"workspace-{AGENT_ID}"
        agent_dir.mkdir(parents=True)
        (agent_dir / "SOUL.md").write_text("# Soul", encoding="utf-8")
        from routers.workspace_files import _list_config_files

        result = _list_config_files(ws, USER_ID, AGENT_ID)
        assert len(result) == 1
        entry = result[0]
        assert entry["name"] == "SOUL.md"
        assert entry["path"] == "SOUL.md"
        assert entry["type"] == "file"
        assert isinstance(entry["size"], int)
        assert isinstance(entry["modified_at"], float)

    def test_reads_from_workspaces_dir_not_agents_dir(self, tmp_path):
        """Config files are expected under workspace-{agent_id}/, not agents/{agent_id}/.

        With the workspace-normalization change, every agent (main and custom)
        stores its SOUL.md and siblings under workspace-{agent_id}/. The old
        agents/{agent_id}/ dir now only holds OpenClaw runtime metadata.
        """
        ws = _make_workspace(tmp_path)
        # Put SOUL.md ONLY in the new location
        new_dir = tmp_path / USER_ID / f"workspace-{AGENT_ID}"
        new_dir.mkdir(parents=True)
        (new_dir / "SOUL.md").write_text("new layout", encoding="utf-8")

        # Also put a decoy in the OLD location — it must NOT be read
        old_dir = tmp_path / USER_ID / "agents" / AGENT_ID
        old_dir.mkdir(parents=True)
        (old_dir / "SOUL.md").write_text("DECOY", encoding="utf-8")

        from routers.workspace_files import _list_config_files

        result = _list_config_files(ws, USER_ID, AGENT_ID)
        names = {f["name"] for f in result}
        assert names == {"SOUL.md"}
        # And confirm we actually read from the new location by checking size
        assert result[0]["size"] == len(b"new layout")


class TestConfigFileReadEndpoint:
    """Tests for GET /workspace/{agent_id}/config-file."""

    def test_reads_allowlisted_file(self, tmp_path):
        """Can read an allowlisted config file."""
        ws = _make_workspace(tmp_path)
        agent_dir = tmp_path / USER_ID / f"workspace-{AGENT_ID}"
        agent_dir.mkdir(parents=True)
        (agent_dir / "SOUL.md").write_text("I am helpful", encoding="utf-8")
        info = ws.read_file_info(USER_ID, f"workspace-{AGENT_ID}/SOUL.md")
        assert info["content"] == "I am helpful"
        assert info["binary"] is False

    @pytest.mark.asyncio
    async def test_read_rejects_non_allowlisted_path(self, workspace, monkeypatch):
        """read_config_file returns 400 for a path not in the allowlist."""
        from routers.workspace_files import read_config_file

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        workspace.ensure_user_dir(USER_ID)
        with pytest.raises(HTTPException) as exc:
            await read_config_file(agent_id=AGENT_ID, path="secret.json", auth=_auth())
        assert exc.value.status_code == 400
        assert "allowlist" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_read_rejects_traversal_in_path(self, workspace, monkeypatch):
        """read_config_file rejects a path with traversal sequences."""
        from routers.workspace_files import read_config_file

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        workspace.ensure_user_dir(USER_ID)
        with pytest.raises(HTTPException) as exc:
            await read_config_file(agent_id=AGENT_ID, path="../../etc/passwd", auth=_auth())
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_read_rejects_traversal_in_agent_id(self, workspace, monkeypatch):
        """read_config_file rejects a traversal sequence in agent_id."""
        from routers.workspace_files import read_config_file

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        workspace.ensure_user_dir(USER_ID)
        with pytest.raises(HTTPException) as exc:
            await read_config_file(agent_id="../other", path="SOUL.md", auth=_auth())
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_list_rejects_traversal_in_agent_id(self, workspace, monkeypatch):
        """list_config_files returns 400 for an agent_id containing '..'."""
        from routers.workspace_files import list_config_files

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        workspace.ensure_user_dir(USER_ID)
        with pytest.raises(HTTPException) as exc:
            await list_config_files(agent_id="../../other", auth=_auth())
        assert exc.value.status_code == 400


# ===========================================================================
# TestWriteFileEndpoint
# ===========================================================================


class TestWriteFileEndpoint:
    """Tests for the _write_file helper used by the PUT endpoint."""

    def test_write_workspace_file(self, tmp_path):
        """Writing a workspace file creates it on disk."""
        ws = _make_workspace(tmp_path)
        user_root = tmp_path / USER_ID
        user_root.mkdir(parents=True)
        ws_dir = user_root / f"workspace-{AGENT_ID}"
        ws_dir.mkdir(parents=True)

        from routers.workspace_files import _write_file

        _write_file(ws, USER_ID, AGENT_ID, "plan.md", "# My Plan", "workspace")
        assert (ws_dir / "plan.md").read_text() == "# My Plan"

    def test_write_config_file_allowlisted(self, tmp_path):
        """Writing an allowlisted config file succeeds."""
        ws = _make_workspace(tmp_path)
        agent_dir = tmp_path / USER_ID / f"workspace-{AGENT_ID}"
        agent_dir.mkdir(parents=True)

        from routers.workspace_files import _write_file

        _write_file(ws, USER_ID, AGENT_ID, "SOUL.md", "I am kind", "config")
        assert (agent_dir / "SOUL.md").read_text() == "I am kind"

    def test_write_config_file_not_allowlisted_raises(self, tmp_path):
        """Writing a non-allowlisted config file raises ValueError."""
        ws = _make_workspace(tmp_path)
        (tmp_path / USER_ID / f"workspace-{AGENT_ID}").mkdir(parents=True)

        from routers.workspace_files import _write_file

        with pytest.raises(ValueError, match="not in allowlist"):
            _write_file(ws, USER_ID, AGENT_ID, "secret.json", "{}", "config")

    def test_config_tab_writes_under_workspaces_subtree(self, tmp_path):
        """tab='config' writes into workspace-{id}/, not agents/{id}/."""
        ws = _make_workspace(tmp_path)
        ws_dir = tmp_path / USER_ID / f"workspace-{AGENT_ID}"
        ws_dir.mkdir(parents=True)

        from routers.workspace_files import _write_file

        written = _write_file(ws, USER_ID, AGENT_ID, "SOUL.md", "hello", "config")
        assert written == f"workspace-{AGENT_ID}/SOUL.md"
        assert (ws_dir / "SOUL.md").read_text() == "hello"
        # And confirm nothing was written into the old agents/{id}/ path
        assert not (tmp_path / USER_ID / "agents" / AGENT_ID / "SOUL.md").exists()

    def test_bootstrap_md_not_in_allowlist(self):
        """BOOTSTRAP.md is not a real OpenClaw-seeded file — drop it from the allowlist."""
        from routers.workspace_files import CONFIG_ALLOWLIST

        assert "BOOTSTRAP.md" not in CONFIG_ALLOWLIST
        # The 7 files OpenClaw actually seeds (per
        # desktop/openclaw/src/agents/workspace.ts):
        assert CONFIG_ALLOWLIST == {
            "SOUL.md",
            "MEMORY.md",
            "TOOLS.md",
            "IDENTITY.md",
            "USER.md",
            "HEARTBEAT.md",
            "AGENTS.md",
        }

    def test_write_creates_parent_dirs(self, tmp_path):
        """Writing to a nested path creates intermediate directories."""
        ws = _make_workspace(tmp_path)
        (tmp_path / USER_ID / f"workspace-{AGENT_ID}").mkdir(parents=True)

        from routers.workspace_files import _write_file

        _write_file(ws, USER_ID, AGENT_ID, "deep/nested/file.txt", "hello", "workspace")
        assert (tmp_path / USER_ID / f"workspace-{AGENT_ID}" / "deep" / "nested" / "file.txt").read_text() == "hello"

    @pytest.mark.asyncio
    async def test_endpoint_rejects_traversal_in_agent_id(self, workspace, monkeypatch):
        """PUT endpoint returns 400 for traversal in agent_id."""
        from fastapi import HTTPException
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        workspace.ensure_user_dir(USER_ID)
        body = WriteFileRequest(path="plan.md", content="x", tab="workspace")
        with pytest.raises(HTTPException) as exc:
            await write_workspace_file(agent_id="../other", body=body, auth=_auth())
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_endpoint_rejects_invalid_tab(self, workspace, monkeypatch):
        """PUT endpoint returns 400 for a tab value that is neither workspace nor config."""
        from fastapi import HTTPException
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        workspace.ensure_user_dir(USER_ID)
        body = WriteFileRequest(path="plan.md", content="x", tab="bogus")
        with pytest.raises(HTTPException) as exc:
            await write_workspace_file(agent_id=AGENT_ID, body=body, auth=_auth())
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_endpoint_rejects_non_allowlisted_config_filename(self, workspace, monkeypatch):
        """PUT endpoint returns 400 when writing a non-allowlisted config file."""
        from fastapi import HTTPException
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        workspace.ensure_user_dir(USER_ID)
        body = WriteFileRequest(path="secret.json", content="{}", tab="config")
        with pytest.raises(HTTPException) as exc:
            await write_workspace_file(agent_id=AGENT_ID, body=body, auth=_auth())
        assert exc.value.status_code == 400

    def test_write_rejects_traversal_in_path(self, tmp_path):
        """`..` in path raises ValueError (regression for allowlist bypass)."""
        ws = _make_workspace(tmp_path)
        (tmp_path / USER_ID / f"workspace-{AGENT_ID}").mkdir(parents=True)

        from routers.workspace_files import _write_file

        with pytest.raises(ValueError, match=r"\.\."):
            _write_file(
                ws,
                USER_ID,
                AGENT_ID,
                "../../agents/" + AGENT_ID + "/SOUL.md",
                "hacked",
                "workspace",
            )

    def test_write_rejects_absolute_path(self, tmp_path):
        """Absolute paths are rejected."""
        ws = _make_workspace(tmp_path)
        (tmp_path / USER_ID / f"workspace-{AGENT_ID}").mkdir(parents=True)

        from routers.workspace_files import _write_file

        with pytest.raises(ValueError, match="relative"):
            _write_file(ws, USER_ID, AGENT_ID, "/etc/passwd", "x", "workspace")

    def test_write_rejects_empty_path(self, tmp_path):
        """Empty path is rejected."""
        ws = _make_workspace(tmp_path)
        (tmp_path / USER_ID / f"workspace-{AGENT_ID}").mkdir(parents=True)

        from routers.workspace_files import _write_file

        with pytest.raises(ValueError):
            _write_file(ws, USER_ID, AGENT_ID, "", "x", "workspace")

    def test_write_rejects_dot_only_path(self, tmp_path):
        """Dot-only paths (., ./, .) are rejected with ValueError."""
        ws = _make_workspace(tmp_path)
        (tmp_path / USER_ID / f"workspace-{AGENT_ID}").mkdir(parents=True)

        from routers.workspace_files import _write_file

        for bad in [".", "./", "./.", "././"]:
            with pytest.raises(ValueError, match=r"dot-only"):
                _write_file(ws, USER_ID, AGENT_ID, bad, "x", "workspace")

    def test_write_rejects_oversized_content(self, tmp_path):
        """Content over 10MB is rejected."""
        ws = _make_workspace(tmp_path)
        (tmp_path / USER_ID / f"workspace-{AGENT_ID}").mkdir(parents=True)

        from routers.workspace_files import _write_file

        big = "a" * (10 * 1024 * 1024 + 1)
        with pytest.raises(ValueError, match="10MB"):
            _write_file(ws, USER_ID, AGENT_ID, "big.txt", big, "workspace")

    @pytest.mark.asyncio
    async def test_endpoint_rejects_traversal_in_path(self, workspace, monkeypatch):
        """PUT endpoint returns 400 for `..` in body.path (regression test)."""
        from fastapi import HTTPException
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        workspace.ensure_user_dir(USER_ID)
        body = WriteFileRequest(
            path=f"../../agents/{AGENT_ID}/SOUL.md",
            content="hacked",
            tab="workspace",
        )
        with pytest.raises(HTTPException) as exc:
            await write_workspace_file(agent_id=AGENT_ID, body=body, auth=_auth())
        assert exc.value.status_code == 400

    def test_write_rejects_symlink_escape_workspace(self, tmp_path):
        """Symlink in workspace-{id}/ pointing outside must be rejected."""
        import os

        ws = _make_workspace(tmp_path)
        ws_dir = tmp_path / USER_ID / f"workspace-{AGENT_ID}"
        ws_dir.mkdir(parents=True)
        agent_dir = tmp_path / USER_ID / "agents" / AGENT_ID
        agent_dir.mkdir(parents=True)
        (agent_dir / "SOUL.md").write_text("original", encoding="utf-8")

        # Symlink workspace-{id}/evil -> ../../agents/{id}/SOUL.md
        os.symlink(
            "../../agents/" + AGENT_ID + "/SOUL.md",
            ws_dir / "evil",
        )

        from routers.workspace_files import _write_file

        with pytest.raises(ValueError, match="escapes"):
            _write_file(ws, USER_ID, AGENT_ID, "evil", "hacked", "workspace")

        # SOUL.md must be untouched
        assert (agent_dir / "SOUL.md").read_text() == "original"

    def test_write_rejects_symlink_escape_config(self, tmp_path):
        """Symlink in workspace-{id}/ pointing outside must be rejected for config tab."""
        import os

        ws = _make_workspace(tmp_path)
        agent_dir = tmp_path / USER_ID / f"workspace-{AGENT_ID}"
        agent_dir.mkdir(parents=True)
        target = tmp_path / USER_ID / "elsewhere.txt"
        target.write_text("original", encoding="utf-8")

        # Symlink workspace-{id}/SOUL.md -> ../../elsewhere.txt
        os.symlink("../../elsewhere.txt", agent_dir / "SOUL.md")

        from routers.workspace_files import _write_file

        with pytest.raises(ValueError, match="escapes"):
            _write_file(ws, USER_ID, AGENT_ID, "SOUL.md", "hacked", "config")

        assert target.read_text() == "original"

    def test_write_allows_nested_dirs_within_workspace(self, tmp_path):
        """Subtree check allows legitimate nested paths inside the workspace."""
        ws = _make_workspace(tmp_path)
        (tmp_path / USER_ID / f"workspace-{AGENT_ID}").mkdir(parents=True)

        from routers.workspace_files import _write_file

        written = _write_file(ws, USER_ID, AGENT_ID, "deep/nested/notes.md", "ok", "workspace")
        assert written == f"workspace-{AGENT_ID}/deep/nested/notes.md"
        assert (tmp_path / USER_ID / f"workspace-{AGENT_ID}" / "deep" / "nested" / "notes.md").read_text() == "ok"


# ===========================================================================
# TestUploadPath
# ===========================================================================


class TestUploadPath:
    """Verify upload destination path construction."""

    def test_upload_writes_to_agent_workspace(self, tmp_path):
        """Uploads should go to workspace-{agent_id}/uploads/."""
        ws = _make_workspace(tmp_path)
        ws_dir = tmp_path / USER_ID / f"workspace-{AGENT_ID}" / "uploads"
        (tmp_path / USER_ID).mkdir(parents=True)

        dest_path = f"workspace-{AGENT_ID}/uploads/test.pdf"
        ws.write_bytes(USER_ID, dest_path, b"fake pdf content")
        assert (ws_dir / "test.pdf").read_bytes() == b"fake pdf content"

    def test_agent_visible_path(self):
        """Agent-visible path should include workspace-{agent_id}."""
        agent_id = "my-agent"
        filename = "data.csv"
        dest_path = f"workspace-{agent_id}/uploads/{filename}"
        agent_path = f".openclaw/{dest_path}"
        assert agent_path == f".openclaw/workspace-{agent_id}/uploads/{filename}"


# ===========================================================================
# TestWorkspaceTreeRoundTrip
# ===========================================================================


class TestWorkspaceTreeRoundTrip:
    """Verify list → read uses the same agent-relative path contract."""

    @pytest.mark.asyncio
    async def test_tree_path_feeds_back_to_read(self, workspace, monkeypatch):
        """A path returned by the tree endpoint reads successfully via the file endpoint."""
        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        ws_dir = workspace._mount / USER_ID / f"workspace-{AGENT_ID}"
        ws_dir.mkdir(parents=True)
        (ws_dir / "plan.md").write_text("# Plan\nstep 1", encoding="utf-8")
        (ws_dir / "uploads").mkdir()
        (ws_dir / "uploads" / "data.csv").write_text("a,b\n1,2", encoding="utf-8")

        from routers.workspace_files import list_workspace_tree, read_workspace_file

        root = await list_workspace_tree(agent_id=AGENT_ID, path="", auth=_auth())
        paths = {f["path"] for f in root["files"]}
        # Paths must be agent-relative (no `workspace-{agent_id}/` prefix)
        assert paths == {"plan.md", "uploads"}
        # One level per request: a child directory is listed by its agent-relative path
        child = await list_workspace_tree(agent_id=AGENT_ID, path="uploads", auth=_auth())
        assert [f["path"] for f in child["files"]] == ["uploads/data.csv"]

        # Every file path should read successfully via the file endpoint
        for entry in root["files"] + child["files"]:
            if entry["type"] == "file":
                info = await read_workspace_file(
                    agent_id=AGENT_ID,
                    path=entry["path"],
                    auth=_auth(),
                )
                assert info["name"] == entry["name"]
                assert info["size"] == entry["size"]

    @pytest.mark.asyncio
    async def test_write_then_tree_then_read(self, workspace, monkeypatch):
        """After saveWorkspaceFile writes a workspace file, tree lists it and read returns content."""
        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        (workspace._mount / USER_ID / f"workspace-{AGENT_ID}").mkdir(parents=True)

        from routers.workspace_files import (
            WriteFileRequest,
            list_workspace_tree,
            read_workspace_file,
            write_workspace_file,
        )

        body = WriteFileRequest(path="notes.txt", content="hello world", tab="workspace")
        result = await write_workspace_file(agent_id=AGENT_ID, body=body, auth=_auth())
        assert result["status"] == "ok"
        # Backend returned path is user-root-relative; tree will show agent-relative
        assert result["path"] == f"workspace-{AGENT_ID}/notes.txt"

        tree = await list_workspace_tree(agent_id=AGENT_ID, path="", auth=_auth())
        assert any(f["path"] == "notes.txt" and f["type"] == "file" for f in tree["files"])

        info = await read_workspace_file(agent_id=AGENT_ID, path="notes.txt", auth=_auth())
        assert info["content"] == "hello world"


# ===========================================================================
# TestReadEndpointTraversal — P1 path-traversal hardening on the READ endpoints
# ===========================================================================


class TestReadEndpointTraversal:
    """The read endpoints (tree + file) historically lacked the subtree
    containment the write path already enforced, so ``path=../<other_agent>/...``
    read across workspaces within the tenant. These tests lock in the guard.
    """

    VICTIM_AGENT = "agent-victim-999"

    def _two_agents(self, workspace):
        """Create the attacker (AGENT_ID) + victim agent workspaces; seed a
        secret file in the victim. Returns the user root Path."""
        root = workspace._mount / USER_ID
        (root / f"workspace-{AGENT_ID}").mkdir(parents=True)
        victim = root / f"workspace-{self.VICTIM_AGENT}"
        victim.mkdir(parents=True)
        (victim / "secret.txt").write_text("cross-tenant-secret", encoding="utf-8")
        return root

    @pytest.mark.asyncio
    async def test_read_rejects_parent_traversal_in_path(self, workspace, monkeypatch):
        """read_workspace_file rejects a `..` path that would escape the agent subtree."""
        from routers.workspace_files import read_workspace_file

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        self._two_agents(workspace)
        with pytest.raises(HTTPException) as exc:
            await read_workspace_file(
                agent_id=AGENT_ID,
                path=f"../{self.VICTIM_AGENT}/secret.txt",
                auth=_auth(),
            )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_tree_rejects_parent_traversal_in_path(self, workspace, monkeypatch):
        """list_workspace_tree rejects a `..` path that would escape the agent subtree."""
        from routers.workspace_files import list_workspace_tree

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        self._two_agents(workspace)
        with pytest.raises(HTTPException) as exc:
            await list_workspace_tree(
                agent_id=AGENT_ID,
                path=f"../{self.VICTIM_AGENT}",
                auth=_auth(),
            )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_read_rejects_symlink_escape_to_sibling_agent(self, workspace, monkeypatch):
        """A symlink inside the agent workspace pointing at a sibling agent's file
        is rejected (403) even though the request path has no `..`."""
        import os

        from routers.workspace_files import read_workspace_file

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        root = self._two_agents(workspace)
        os.symlink(
            f"../{self.VICTIM_AGENT}/secret.txt",
            root / f"workspace-{AGENT_ID}" / "evil",
        )
        with pytest.raises(HTTPException) as exc:
            await read_workspace_file(agent_id=AGENT_ID, path="evil", auth=_auth())
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_read_allows_legit_nested_path(self, workspace, monkeypatch):
        """Positive control: a normal nested path inside the agent subtree reads fine."""
        from routers.workspace_files import read_workspace_file

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        root = self._two_agents(workspace)
        nested = root / f"workspace-{AGENT_ID}" / "deep"
        nested.mkdir()
        (nested / "ok.txt").write_text("fine", encoding="utf-8")
        info = await read_workspace_file(agent_id=AGENT_ID, path="deep/ok.txt", auth=_auth())
        assert info["content"] == "fine"

    @pytest.mark.asyncio
    async def test_read_500_does_not_leak_internal_path(self, workspace, monkeypatch):
        """An unexpected WorkspaceError returns a generic 500, not the EFS path."""
        from routers.workspace_files import read_workspace_file

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        root = self._two_agents(workspace)
        (root / f"workspace-{AGENT_ID}" / "notes.txt").write_text("hi", encoding="utf-8")
        leaky = "/mnt/efs/users/" + USER_ID + "/workspace-secret-internal-path boom"
        monkeypatch.setattr(workspace, "read_file_info", lambda *a, **k: (_ for _ in ()).throw(WorkspaceError(leaky)))
        with pytest.raises(HTTPException) as exc:
            await read_workspace_file(agent_id=AGENT_ID, path="notes.txt", auth=_auth())
        assert exc.value.status_code == 500
        assert "/mnt/efs" not in exc.value.detail
        assert exc.value.detail == "Failed to read workspace file"

    @pytest.mark.asyncio
    async def test_tree_500_does_not_leak_internal_path(self, workspace, monkeypatch):
        """An unexpected WorkspaceError on list returns a generic 500, not the EFS path."""
        from routers.workspace_files import list_workspace_tree

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        self._two_agents(workspace)
        leaky = "/mnt/efs/users/" + USER_ID + "/workspace-secret-internal-path boom"
        monkeypatch.setattr(workspace, "list_directory", lambda *a, **k: (_ for _ in ()).throw(WorkspaceError(leaky)))
        with pytest.raises(HTTPException) as exc:
            await list_workspace_tree(agent_id=AGENT_ID, path="", auth=_auth())
        assert exc.value.status_code == 500
        assert "/mnt/efs" not in exc.value.detail
        assert exc.value.detail == "Failed to list workspace directory"

    @pytest.mark.asyncio
    async def test_tree_cannot_descend_into_symlinked_sibling(self, workspace, monkeypatch):
        """Level-by-level listing: the escaping symlink is dropped from the root
        listing, and asking for it as a subdirectory is refused (guarding only
        the root path is insufficient)."""
        import os

        from routers.workspace_files import list_workspace_tree

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        root = self._two_agents(workspace)
        (root / f"workspace-{AGENT_ID}" / "notes.txt").write_text("legit", encoding="utf-8")
        # workspace-<attacker>/link -> ../workspace-<victim> (a sibling agent workspace)
        os.symlink(f"../workspace-{self.VICTIM_AGENT}", root / f"workspace-{AGENT_ID}" / "link")

        tree = await list_workspace_tree(agent_id=AGENT_ID, path="", auth=_auth())
        paths = {f["path"] for f in tree["files"]}
        assert "notes.txt" in paths  # legit content still listed
        assert "link" not in paths
        with pytest.raises(HTTPException) as exc:
            await list_workspace_tree(agent_id=AGENT_ID, path="link", auth=_auth())
        assert exc.value.status_code == 403
        assert self.VICTIM_AGENT not in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_tree_nonrecursive_omits_symlink_escape(self, workspace, monkeypatch):
        """The non-recursive listing drops an immediate symlink that escapes the subtree."""
        import os

        from routers.workspace_files import list_workspace_tree

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        root = self._two_agents(workspace)
        (root / f"workspace-{AGENT_ID}" / "notes.txt").write_text("legit", encoding="utf-8")
        os.symlink(f"../workspace-{self.VICTIM_AGENT}", root / f"workspace-{AGENT_ID}" / "link")

        tree = await list_workspace_tree(agent_id=AGENT_ID, path="", auth=_auth())
        names = {f["name"] for f in tree["files"]}
        assert "notes.txt" in names
        assert "link" not in names


class TestTreeListingOffLoop:
    """The listing runs on EFS (NFS). On 2026-09-12 the old recursive walk of a
    ~114k-entry workspace ran ~150s ON THE EVENT LOOP, freezing /health and every
    request; the endpoint is now one directory per call, off the loop."""

    @pytest.mark.asyncio
    async def test_listing_does_not_block_event_loop(self, workspace, monkeypatch):
        import asyncio
        import time

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        ws_dir = workspace._mount / USER_ID / f"workspace-{AGENT_ID}"
        ws_dir.mkdir(parents=True)
        (ws_dir / "a.md").write_text("x", encoding="utf-8")
        real = workspace.list_directory

        def stalled_nfs_listing(*args, **kwargs):
            time.sleep(0.3)  # one stalled NFS round trip
            return real(*args, **kwargs)

        monkeypatch.setattr(workspace, "list_directory", stalled_nfs_listing)
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        from routers.workspace_files import list_workspace_tree

        tree = await list_workspace_tree(agent_id=AGENT_ID, path="", auth=_auth())
        beat.cancel()
        assert [f["path"] for f in tree["files"]] == ["a.md"]
        assert ticks >= 5, "the event loop stalled while the EFS listing ran"

    def test_no_recursive_mode(self):
        import inspect

        from routers.workspace_files import list_workspace_tree

        assert "recursive" not in inspect.signature(list_workspace_tree).parameters


# ===========================================================================
# TestAgentCoreProfileBranch (Task 10-B, M6-R48)
# ===========================================================================


class TestAgentCoreProfileBranch:
    """On an AgentCore row every route serves the agent's S3 Hermes profile.

    The migration copies EFS into the profile and leaves the EFS tree behind, so without this
    branch the Files panel and the Instructions tab read and WRITE a stale tree nobody reads --
    a green save with no effect. The EFS tree below is deliberately populated with DIFFERENT
    content in one test, to prove the branch never reaches it.
    """

    @pytest.fixture(autouse=True)
    def _agentcore_owner(self):
        with patch("routers.workspace_files.runtime_transport.is_agentcore_owner", AsyncMock(return_value=True)):
            yield

    @pytest.fixture()
    def s3(self):
        import boto3
        from moto import mock_aws

        from core.services import hermes_profiles

        hermes_profiles._SHARED_S3_CLIENT = None
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="test-replicas")
            with patch.object(settings, "OWNER_STATE_BUCKET", "test-replicas"):
                yield client
        hermes_profiles._SHARED_S3_CLIENT = None

    @staticmethod
    def _prefix() -> str:
        return f"{USER_ID}/hermes/{AGENT_ID}/"

    def _put(self, s3, key: str, body: bytes) -> None:
        s3.put_object(Bucket="test-replicas", Key=f"{self._prefix()}{key}", Body=body)

    def _get(self, s3, key: str) -> bytes:
        return s3.get_object(Bucket="test-replicas", Key=f"{self._prefix()}{key}")["Body"].read()

    # -- Instructions tab -------------------------------------------------

    @pytest.mark.asyncio
    async def test_reads_soul_md_from_the_profile_not_the_stale_efs_copy(self, s3, workspace, monkeypatch):
        from routers.workspace_files import read_config_file

        monkeypatch.setattr("routers.workspace_files.get_workspace", lambda: workspace)
        efs_dir = workspace._mount / USER_ID / f"workspace-{AGENT_ID}"
        efs_dir.mkdir(parents=True)
        (efs_dir / "SOUL.md").write_text("STALE EFS COPY", encoding="utf-8")
        self._put(s3, "SOUL.md", b"# Ada\n\nthe live profile\n")

        info = await read_config_file(agent_id=AGENT_ID, path="SOUL.md", auth=_auth())

        assert info["content"] == "# Ada\n\nthe live profile\n"
        assert info["name"] == "SOUL.md"

    @pytest.mark.asyncio
    async def test_writes_soul_md_through_the_profile_writer_and_bumps_the_revision(self, s3):
        from core.services import hermes_profiles
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        self._put(s3, ".revision", b"before")
        body = WriteFileRequest(
            path="SOUL.md", content=f"{hermes_profiles.SOUL_GENERATED_MARKER}\n\n# Ada\n\nowner text\n", tab="config"
        )

        result = await write_workspace_file(agent_id=AGENT_ID, body=body, auth=_auth())

        assert result == {"status": "ok", "path": "SOUL.md"}
        # The marker is stripped on save: the file is the owner's from now on, so a later rename
        # (runtime_rpc._refresh_agentcore_soul) must not regenerate over it.
        stored = self._get(s3, "SOUL.md").decode()
        assert stored == "# Ada\n\nowner text\n"
        assert not hermes_profiles.is_generated_soul(stored)
        assert self._get(s3, ".revision") != b"before"

    @pytest.mark.asyncio
    async def test_user_md_maps_onto_the_hermes_user_profile_memory(self, s3):
        from routers.workspace_files import WriteFileRequest, read_config_file, write_workspace_file

        self._put(s3, "memories/USER.md", b"# The owner\n")

        info = await read_config_file(agent_id=AGENT_ID, path="USER.md", auth=_auth())
        assert info["content"] == "# The owner\n"
        assert info["path"] == "USER.md"  # reported under the name the tab asked for

        await write_workspace_file(
            agent_id=AGENT_ID,
            body=WriteFileRequest(path="USER.md", content="# Edited\n", tab="config"),
            auth=_auth(),
        )
        assert self._get(s3, "memories/USER.md") == b"# Edited\n"

    @pytest.mark.asyncio
    async def test_identity_md_reads_a_stub_and_refuses_a_save(self, s3):
        from routers.workspace_files import WriteFileRequest, read_config_file, write_workspace_file

        info = await read_config_file(agent_id=AGENT_ID, path="IDENTITY.md", auth=_auth())
        assert "SOUL.md" in info["content"]
        assert info["binary"] is False

        with pytest.raises(HTTPException) as exc:
            await write_workspace_file(
                agent_id=AGENT_ID,
                body=WriteFileRequest(path="IDENTITY.md", content="nope", tab="config"),
                auth=_auth(),
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "unsupported_on_agentcore"

    @pytest.mark.asyncio
    async def test_config_files_lists_only_the_names_with_a_hermes_home(self, s3):
        from routers.workspace_files import list_config_files

        self._put(s3, "AGENTS.md", b"# Agents\n")
        self._put(s3, "SOUL.md", b"# Ada\n")
        self._put(s3, "memories/USER.md", b"# Owner\n")
        self._put(s3, "config.yaml", b"model: {}\n")  # backend-owned, never a config-tab row

        result = await list_config_files(agent_id=AGENT_ID, auth=_auth())

        assert [f["path"] for f in result["files"]] == ["AGENTS.md", "SOUL.md", "USER.md"]
        assert result["files"][1]["size"] == len(b"# Ada\n")

    @pytest.mark.asyncio
    async def test_config_names_with_no_settled_hermes_path_are_refused_not_silently_written(self, s3):
        """MEMORY.md/TOOLS.md/HEARTBEAT.md: a write with nowhere real to land is the defect this
        branch exists to kill, so it is a 422, never a green save."""
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        for name in ("MEMORY.md", "TOOLS.md", "HEARTBEAT.md"):
            with pytest.raises(HTTPException) as exc:
                await write_workspace_file(
                    agent_id=AGENT_ID,
                    body=WriteFileRequest(path=name, content="x", tab="config"),
                    auth=_auth(),
                )
            assert exc.value.status_code == 422
            assert exc.value.detail["code"] == "unsupported_on_agentcore"

    # -- Files panel ------------------------------------------------------

    @pytest.mark.asyncio
    async def test_tree_lists_the_profile_without_the_env_file_or_mcp_tokens(self, s3):
        from routers.workspace_files import list_workspace_tree

        self._put(s3, "SOUL.md", b"# Ada\n")
        self._put(s3, ".env", b"ANTHROPIC_API_KEY=sk-live\n")
        self._put(s3, ".revision", b"rev")
        self._put(s3, "mcp-tokens/github.json", b"{}")
        self._put(s3, "skills/foo/SKILL.md", b"# Foo\n")

        result = await list_workspace_tree(agent_id=AGENT_ID, path="", auth=_auth())

        assert [f["path"] for f in result["files"]] == ["skills", "SOUL.md"]

    @pytest.mark.asyncio
    async def test_file_read_refuses_the_secret_store_as_a_plain_miss(self, s3):
        from routers.workspace_files import read_workspace_file

        self._put(s3, ".env", b"ANTHROPIC_API_KEY=sk-live\n")

        with pytest.raises(HTTPException) as exc:
            await read_workspace_file(agent_id=AGENT_ID, path=".env", auth=_auth())
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_workspace_write_lands_in_the_profile(self, s3):
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        result = await write_workspace_file(
            agent_id=AGENT_ID,
            body=WriteFileRequest(path="notes/plan.md", content="# Plan\n", tab="workspace"),
            auth=_auth(),
        )

        assert result == {"status": "ok", "path": "notes/plan.md"}
        assert self._get(s3, "notes/plan.md") == b"# Plan\n"

    @pytest.mark.asyncio
    async def test_workspace_write_refuses_a_backend_owned_file(self, s3):
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        with pytest.raises(HTTPException) as exc:
            await write_workspace_file(
                agent_id=AGENT_ID,
                body=WriteFileRequest(path="config.yaml", content="model: {}\n", tab="workspace"),
                auth=_auth(),
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "unsupported_on_agentcore"

    @pytest.mark.asyncio
    async def test_workspace_write_refuses_a_traversal_path(self, s3):
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        with pytest.raises(HTTPException) as exc:
            await write_workspace_file(
                agent_id=AGENT_ID,
                body=WriteFileRequest(path="../other-agent/SOUL.md", content="pwned", tab="workspace"),
                auth=_auth(),
            )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_the_branch_never_touches_efs(self, s3, workspace, monkeypatch):
        """The whole point: an AgentCore row must not reach the workspace object at all."""
        from routers.workspace_files import list_workspace_tree

        def _boom():
            raise AssertionError("an AgentCore row must never reach EFS")

        monkeypatch.setattr("routers.workspace_files.get_workspace", _boom)
        self._put(s3, "SOUL.md", b"# Ada\n")

        result = await list_workspace_tree(agent_id=AGENT_ID, path="", auth=_auth())
        assert [f["path"] for f in result["files"]] == ["SOUL.md"]
