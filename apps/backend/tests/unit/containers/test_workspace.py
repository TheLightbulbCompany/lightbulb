"""Tests for Workspace (EFS-backed per-user OpenClaw workspaces).

Uses tmp_path fixture for real filesystem operations -- no mocking needed.
"""

import pytest

from core.config import settings
from core.containers.workspace import Workspace, WorkspaceError, _EXCLUDED_NAMES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_environment(monkeypatch):
    """Force ENVIRONMENT=local so os.chown is skipped on macOS."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")


@pytest.fixture
def workspace(tmp_path):
    """Create a Workspace backed by a temporary directory."""
    return Workspace(mount_path=str(tmp_path))


@pytest.fixture
def user_id():
    """A sample user ID for testing."""
    return "user_abc123"


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


class TestWorkspaceInit:
    """Test Workspace initialization."""

    def test_stores_mount_path(self, workspace, tmp_path):
        """Mount path is stored on the instance as a Path."""
        assert workspace._mount == tmp_path


# ---------------------------------------------------------------------------
# user_path
# ---------------------------------------------------------------------------


class TestUserPath:
    """Test user_path helper."""

    def test_returns_user_subdirectory(self, workspace, tmp_path, user_id):
        """user_path returns mount/{user_id}."""
        assert workspace.user_path(user_id) == tmp_path / user_id

    def test_with_special_chars(self, workspace, tmp_path):
        """User IDs with underscores and hyphens are preserved."""
        assert workspace.user_path("user_test-long_id-456") == tmp_path / "user_test-long_id-456"


# ---------------------------------------------------------------------------
# ensure_user_dir
# ---------------------------------------------------------------------------


class TestEnsureUserDir:
    """Test creating user workspace directories."""

    def test_creates_directory(self, workspace, user_id):
        """ensure_user_dir creates the user directory."""
        path = workspace.ensure_user_dir(user_id)
        assert path.is_dir()

    def test_returns_path(self, workspace, tmp_path, user_id):
        """ensure_user_dir returns the Path to the user directory."""
        path = workspace.ensure_user_dir(user_id)
        assert path == tmp_path / user_id

    def test_idempotent(self, workspace, user_id):
        """ensure_user_dir succeeds when directory already exists."""
        workspace.ensure_user_dir(user_id)
        path = workspace.ensure_user_dir(user_id)
        assert path.is_dir()


# ---------------------------------------------------------------------------
# list_agents
# ---------------------------------------------------------------------------


class TestListAgents:
    """Test listing agent directories."""

    def test_empty_when_no_agents_dir(self, workspace, user_id):
        """list_agents returns empty list when agents/ does not exist."""
        workspace.ensure_user_dir(user_id)
        assert workspace.list_agents(user_id) == []

    def test_empty_when_user_dir_missing(self, workspace, user_id):
        """list_agents returns empty list when user directory does not exist."""
        assert workspace.list_agents(user_id) == []

    def test_lists_agent_directories(self, workspace, user_id):
        """list_agents returns sorted names of subdirectories under agents/."""
        agents_dir = workspace.user_path(user_id) / "agents"
        (agents_dir / "zeta-agent").mkdir(parents=True)
        (agents_dir / "alpha-agent").mkdir(parents=True)

        result = workspace.list_agents(user_id)
        assert result == ["alpha-agent", "zeta-agent"]

    def test_ignores_files(self, workspace, user_id):
        """list_agents only returns directories, not files."""
        agents_dir = workspace.user_path(user_id) / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "my-agent").mkdir()
        (agents_dir / "README.md").write_text("ignore me")

        result = workspace.list_agents(user_id)
        assert result == ["my-agent"]


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class TestReadFile:
    """Test reading files from a user's workspace."""

    def test_reads_file_content(self, workspace, user_id):
        """read_file returns the text content of a file."""
        user_dir = workspace.ensure_user_dir(user_id)
        (user_dir / "agents").mkdir()
        (user_dir / "agents" / "SOUL.md").write_text("Hello, world!", encoding="utf-8")

        result = workspace.read_file(user_id, "agents/SOUL.md")
        assert result == "Hello, world!"

    def test_raises_on_missing_file(self, workspace, user_id):
        """read_file raises WorkspaceError when the file does not exist."""
        workspace.ensure_user_dir(user_id)

        with pytest.raises(WorkspaceError, match="File not found"):
            workspace.read_file(user_id, "agents/nonexistent.txt")

    def test_path_traversal_blocked(self, workspace, user_id):
        """read_file raises WorkspaceError on path traversal attempts."""
        workspace.ensure_user_dir(user_id)

        with pytest.raises(WorkspaceError, match="Path traversal denied"):
            workspace.read_file(user_id, "../../etc/passwd")

    def test_error_includes_user_id(self, workspace, user_id):
        """WorkspaceError from read_file includes the user_id."""
        workspace.ensure_user_dir(user_id)

        with pytest.raises(WorkspaceError) as exc_info:
            workspace.read_file(user_id, "missing.txt")
        assert exc_info.value.user_id == user_id


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class TestWriteFile:
    """Test writing files into a user's workspace."""

    def test_writes_content(self, workspace, user_id):
        """write_file creates a file with the given content."""
        workspace.ensure_user_dir(user_id)
        workspace.write_file(user_id, "test.txt", "hello")

        result = workspace.read_file(user_id, "test.txt")
        assert result == "hello"

    def test_creates_parent_directories(self, workspace, user_id):
        """write_file creates intermediate directories as needed."""
        workspace.ensure_user_dir(user_id)
        workspace.write_file(user_id, "agents/my-agent/SOUL.md", "I am helpful.")

        result = workspace.read_file(user_id, "agents/my-agent/SOUL.md")
        assert result == "I am helpful."

    def test_overwrites_existing_file(self, workspace, user_id):
        """write_file overwrites an existing file."""
        workspace.ensure_user_dir(user_id)
        workspace.write_file(user_id, "test.txt", "original")
        workspace.write_file(user_id, "test.txt", "updated")

        result = workspace.read_file(user_id, "test.txt")
        assert result == "updated"

    def test_path_traversal_blocked(self, workspace, user_id):
        """write_file raises WorkspaceError on path traversal attempts."""
        workspace.ensure_user_dir(user_id)

        with pytest.raises(WorkspaceError, match="Path traversal denied"):
            workspace.write_file(user_id, "../other_user/secret.txt", "bad")

    def test_error_includes_user_id(self, workspace, user_id):
        """WorkspaceError from write_file includes the user_id."""
        workspace.ensure_user_dir(user_id)

        with pytest.raises(WorkspaceError) as exc_info:
            workspace.write_file(user_id, "../../escape.txt", "bad")
        assert exc_info.value.user_id == user_id


# ---------------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------------


class TestDeleteFile:
    """Test deleting files from a user's workspace."""

    def test_deletes_existing_file(self, workspace, user_id):
        """delete_file removes the file from disk."""
        workspace.ensure_user_dir(user_id)
        workspace.write_file(user_id, "test.txt", "to be deleted")
        workspace.delete_file(user_id, "test.txt")

        with pytest.raises(WorkspaceError, match="File not found"):
            workspace.read_file(user_id, "test.txt")

    def test_idempotent_on_missing_file(self, workspace, user_id):
        """delete_file succeeds silently when the file does not exist."""
        workspace.ensure_user_dir(user_id)
        workspace.delete_file(user_id, "nonexistent.txt")  # Should not raise

    def test_path_traversal_blocked(self, workspace, user_id):
        """delete_file raises WorkspaceError on path traversal attempts."""
        workspace.ensure_user_dir(user_id)

        with pytest.raises(WorkspaceError, match="Path traversal denied"):
            workspace.delete_file(user_id, "../../etc/passwd")

    def test_error_includes_user_id(self, workspace, user_id):
        """WorkspaceError from delete_file includes the user_id."""
        workspace.ensure_user_dir(user_id)

        with pytest.raises(WorkspaceError) as exc_info:
            workspace.delete_file(user_id, "../../../escape.txt")
        assert exc_info.value.user_id == user_id


# ---------------------------------------------------------------------------
# WorkspaceError
# ---------------------------------------------------------------------------


class TestWorkspaceError:
    """Test custom exception."""

    def test_error_message(self):
        """WorkspaceError stores message."""
        err = WorkspaceError("something failed", user_id="user_123")
        assert str(err) == "something failed"
        assert err.user_id == "user_123"

    def test_error_without_user_id(self):
        """WorkspaceError defaults user_id to empty string."""
        err = WorkspaceError("generic failure")
        assert err.user_id == ""


# ---------------------------------------------------------------------------
# Chown local environment
# ---------------------------------------------------------------------------


class TestChownLocalEnvironment:
    """Tests for os.chown behavior in local environment."""

    def test_chown_skipped_in_local_environment(self, monkeypatch, tmp_path):
        """os.chown is not called when ENVIRONMENT=local."""
        monkeypatch.setattr(settings, "ENVIRONMENT", "local")
        ws = Workspace(mount_path=str(tmp_path))
        ws.write_file("test-user", "test.json", '{"test": true}')
        assert (tmp_path / "test-user" / "test.json").exists()


# ---------------------------------------------------------------------------
# Cleanup agent dirs (post agents.delete reconciliation)
# ---------------------------------------------------------------------------


class TestCleanupAgentDirs:
    """`cleanup_agent_dirs` is the backend's reconciliation step after
    `agents.delete`. OpenClaw's `moveToTrashBestEffort` silently fails on
    Linux containers (cross-device rename from EFS to local overlay), so we
    `rm -rf` the same dirs from the backend.
    """

    def test_removes_agents_subdir(self, workspace, user_id, tmp_path):
        agents_dir = tmp_path / user_id / "agents" / "research-assistant"
        agents_dir.mkdir(parents=True)
        (agents_dir / "agent").mkdir()
        (agents_dir / "sessions").mkdir()
        (agents_dir / "agent" / "state.json").write_text("{}")

        workspace.cleanup_agent_dirs(user_id, "research-assistant")

        assert not agents_dir.exists()

    def test_removes_workspaces_subdir(self, workspace, user_id, tmp_path):
        ws_dir = tmp_path / user_id / "workspaces" / "research-assistant"
        ws_dir.mkdir(parents=True)
        (ws_dir / "AGENTS.md").write_text("# Hello")

        workspace.cleanup_agent_dirs(user_id, "research-assistant")

        assert not ws_dir.exists()

    def test_removes_both_when_both_exist(self, workspace, user_id, tmp_path):
        agents_dir = tmp_path / user_id / "agents" / "ra"
        ws_dir = tmp_path / user_id / "workspaces" / "ra"
        agents_dir.mkdir(parents=True)
        ws_dir.mkdir(parents=True)
        (agents_dir / "x").write_text("x")
        (ws_dir / "y").write_text("y")

        workspace.cleanup_agent_dirs(user_id, "ra")

        assert not agents_dir.exists()
        assert not ws_dir.exists()

    def test_idempotent_when_dirs_missing(self, workspace, user_id):
        # Neither directory exists — must not raise.
        workspace.cleanup_agent_dirs(user_id, "ghost")

    def test_does_not_touch_other_agents(self, workspace, user_id, tmp_path):
        keep = tmp_path / user_id / "agents" / "main"
        keep.mkdir(parents=True)
        (keep / "agent").mkdir()
        delete = tmp_path / user_id / "agents" / "doomed"
        delete.mkdir(parents=True)

        workspace.cleanup_agent_dirs(user_id, "doomed")

        assert keep.exists()
        assert not delete.exists()

    def test_rejects_path_traversal_in_agent_id(self, workspace, user_id, tmp_path):
        # Sibling dir we must NOT delete.
        sibling = tmp_path / "other_user" / "agents" / "main"
        sibling.mkdir(parents=True)
        (sibling / "secret").write_text("don't touch")

        # Attempt to escape via `..` — should refuse and not touch the sibling.
        workspace.cleanup_agent_dirs(user_id, "../../other_user/agents/main")

        assert sibling.exists()
        assert (sibling / "secret").exists()

    def test_rejects_slash_in_agent_id(self, workspace, user_id, tmp_path):
        target = tmp_path / user_id / "agents" / "ra" / "agent"
        target.mkdir(parents=True)

        workspace.cleanup_agent_dirs(user_id, "ra/agent")

        # Slashes in agent_id are refused — the dir survives.
        assert target.exists()

    def test_rejects_empty_agent_id(self, workspace, user_id, tmp_path):
        agents_root = tmp_path / user_id / "agents"
        agents_root.mkdir(parents=True)
        (agents_root / "main").mkdir()

        workspace.cleanup_agent_dirs(user_id, "")

        # Empty agent_id must not nuke the entire agents/ dir.
        assert (agents_root / "main").exists()


# ---------------------------------------------------------------------------
# write_file_if_missing
# ---------------------------------------------------------------------------


class TestWriteFileIfMissing:
    """Test write-once semantics for the seed-files helper."""

    def test_writes_when_absent(self, workspace, user_id):
        """File doesn't exist → method writes it and returns True."""
        rel_path = "workspaces/abc/IDENTITY.md"
        wrote = workspace.write_file_if_missing(user_id, rel_path, "hello")
        assert wrote is True
        assert workspace.read_file(user_id, rel_path) == "hello"

    def test_returns_false_when_present(self, workspace, user_id):
        """File exists → method skips write and returns False."""
        rel_path = "workspaces/abc/IDENTITY.md"
        workspace.write_file(user_id, rel_path, "original")
        wrote = workspace.write_file_if_missing(user_id, rel_path, "replacement")
        assert wrote is False
        # Original content preserved.
        assert workspace.read_file(user_id, rel_path) == "original"

    def test_path_traversal_blocked(self, workspace, user_id):
        """Escape attempts via .. raise WorkspaceError, no file written."""
        with pytest.raises(WorkspaceError):
            workspace.write_file_if_missing(user_id, "../../../etc/passwd", "x")

    def test_creates_parent_directories(self, workspace, user_id):
        """Method creates intermediate directories on first write."""
        rel_path = "workspaces/some-uuid/nested/path/SOUL.md"
        wrote = workspace.write_file_if_missing(user_id, rel_path, "soul content")
        assert wrote is True
        assert workspace.read_file(user_id, rel_path) == "soul content"


# ---------------------------------------------------------------------------
# mcporter removal (post mcp-cutover)
# ---------------------------------------------------------------------------


class TestMcporterRemoved:
    """After the MCP cutover, `.mcporter/mcporter.json` is no longer part
    of the per-user workspace contract. The integrations router reads/writes
    `openclaw.json#mcp.servers` instead, so provisioning must not seed a
    legacy mcporter file and the tree must not silently hide it.
    """

    def test_excluded_names_no_longer_includes_mcporter(self):
        """`.mcporter` is gone from the workspace-tree exclusion set.

        We don't create the directory any more, but we also don't want to
        hide it: any orphan from a pre-cutover provision should surface as
        plain workspace content rather than mysteriously disappear.
        """
        assert ".mcporter" not in _EXCLUDED_NAMES

    def test_ensure_user_dir_does_not_create_mcporter(self, workspace, user_id):
        """Provisioning must not write a default mcporter.json any more."""
        user_dir = workspace.ensure_user_dir(user_id)
        assert user_dir.exists(), "User dir should exist"
        assert not (user_dir / ".mcporter").exists(), (
            "mcporter dir should not be created"
        )
        assert not (user_dir / ".mcporter" / "mcporter.json").exists()
