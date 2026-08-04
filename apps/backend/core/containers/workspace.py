"""Direct EFS access for per-user OpenClaw workspaces.

Each user's workspace is stored at:
    {mount_path}/{user_id}/

OpenClaw runtime agent state lives under:
    {mount_path}/{user_id}/agents/{agent_name}/

Per-agent workspace files live at the upstream-convention siblings:
    {mount_path}/{user_id}/workspace-{agent_id}/

This module handles filesystem CRUD for agent workspaces on EFS.
No S3 or Docker involved -- plain file operations on a mounted volume.
"""

import asyncio
import base64
import io
import json
import logging
import mimetypes
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.config import settings
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

# Clerk user ids look like `user_xxxxxx`. Restrict to that character class so
# any path component can never contain `/`, `..`, or other traversal payloads
# when interpolated into an EFS path.
_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# Build/cache/VCS junk to hide from every directory listing, regardless of
# depth. Do NOT also hide dotfiles/dotdirs (e.g. a catch-all
# `name.startswith(".")` rule) plus `openclaw.json` / `.openclaw` — that
# suppresses agent-managed state (e.g., an MCP that writes into `.mcp/`
# inside its workspace) without giving the user any way to discover it.
# Paying users can deal with seeing their own files; only build artifacts
# and VCS metadata stay hidden.
_EXCLUDED_NAMES: set[str] = {
    "node_modules",
    "__pycache__",
    ".git",
}

# File extensions treated as plain text (returned as UTF-8 strings).
_TEXT_EXTENSIONS: set[str] = {
    ".md",
    ".txt",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
    ".bash",
    ".css",
    ".html",
    ".xml",
    ".csv",
    ".sql",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".r",
    ".lua",
    ".lobster",
    ".env",
    ".cfg",
    ".ini",
    ".conf",
    ".log",
}

# POSIX UID/GID for per-user EFS access points.
# OpenClaw containers run as node (uid 1000) via the access point.
_EFS_USER_UID = 1000
_EFS_USER_GID = 1000

_workspace: Optional["Workspace"] = None


def get_workspace() -> "Workspace":
    """Get the EFS workspace singleton."""
    global _workspace
    if _workspace is None:
        _workspace = Workspace(mount_path=settings.EFS_MOUNT_PATH)
    return _workspace


class WorkspaceError(Exception):
    """Raised when workspace filesystem operations fail."""

    def __init__(self, message: str, user_id: str = ""):
        super().__init__(message)
        self.user_id = user_id


class Workspace:
    """Direct EFS access for per-user OpenClaw workspaces."""

    def __init__(self, mount_path: str):
        self._mount = Path(mount_path)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def user_path(self, user_id: str) -> Path:
        """Return the root directory for a user's workspace."""
        if not user_id or "/" in user_id or ".." in user_id:
            raise WorkspaceError(f"Invalid user_id: {user_id!r}", user_id=user_id)
        return self._mount / user_id

    def _resolve_user_file(self, user_id: str, path: str) -> Path:
        """Resolve a user-relative path and validate it stays within bounds.

        Raises:
            WorkspaceError: If the path is malformed (e.g. contains an
                embedded null byte) or the resolved path escapes the user
                directory (path traversal attack).
        """
        user_dir = self.user_path(user_id).resolve()
        # A path carrying an embedded null byte makes ``resolve()`` raise a
        # raw ``ValueError`` (the OS rejects NUL in path components). Callers
        # only guard against WorkspaceError, so wrap it as one — otherwise an
        # untrusted path component (e.g. an agent_id from a URL) escapes as an
        # unhandled 500 instead of a clean rejection.
        try:
            resolved = (user_dir / path).resolve()
        except ValueError:
            raise WorkspaceError(
                f"Invalid path: {path!r}",
                user_id=user_id,
            )
        try:
            resolved.relative_to(user_dir)
        except ValueError:
            put_metric("workspace.path_traversal.attempt")
            raise WorkspaceError(
                f"Path traversal denied: {path!r}",
                user_id=user_id,
            )
        return resolved

    # ------------------------------------------------------------------
    # Directory operations
    # ------------------------------------------------------------------

    def ensure_user_dir(self, user_id: str) -> Path:
        """Create the user workspace directory if it does not exist.

        Returns:
            The Path to the user's workspace directory.

        Raises:
            WorkspaceError: On filesystem errors.
        """
        p = self.user_path(user_id)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Failed to create user directory for %s: %s", user_id, exc)
            raise WorkspaceError(
                f"Failed to create user directory for {user_id}: {exc}",
                user_id=user_id,
            ) from exc

        return p

    def list_agents(self, user_id: str) -> list[str]:
        """List agent names (subdirectories) under a user's agents/ dir.

        Returns:
            Sorted list of agent directory names, or empty list if the
            agents/ directory does not exist.
        """
        agents_dir = self.user_path(user_id) / "agents"
        if not agents_dir.exists():
            return []
        return sorted(d.name for d in agents_dir.iterdir() if d.is_dir())

    def list_workspace_agent_dirs(self, user_id: str) -> list[str]:
        """List agent_ids that have a ``workspace-{agent_id}`` directory.

        This reflects the set of agents with on-EFS workspace files (created
        by agent CRUD or catalog deploy), independent of OpenClaw's runtime
        ``agents/`` state. A just-deployed agent gets its ``workspace-{id}``
        dir immediately, but only appears under ``agents/`` after OpenClaw
        processes the new ``openclaw.json`` — so the deploy-provenance
        lookup must scan for these dirs. Layout follows upstream OpenClaw's
        per-agent convention (``~/.openclaw/workspace-<agentId>``) — never a
        ``workspaces/`` folder, which upstream doctor treats as retired
        plugin state and deletes.
        """
        user_root = self.user_path(user_id)
        if not user_root.exists():
            return []
        prefix = "workspace-"
        return sorted(d.name[len(prefix) :] for d in user_root.iterdir() if d.is_dir() and d.name.startswith(prefix))

    def list_directory(self, user_id: str, path: str) -> list[dict]:
        """List the contents of a directory in a user's workspace.

        System files/dirs defined in ``_EXCLUDED_NAMES`` (build artifacts
        + VCS metadata) are omitted from the results. Dotfiles and
        dotdirs are NOT hidden — agent-managed state belongs to the user.

        Args:
            user_id: The user whose workspace to inspect.
            path: Relative path within the user's workspace directory.

        Returns:
            List of entry dicts sorted dirs-first then alphabetically::

                {
                    "name": str,
                    "path": str,        # relative to user root
                    "type": "file"|"dir",
                    "size": int|None,   # None for directories
                    "modified_at": float,
                }

        Raises:
            WorkspaceError: If the path escapes the user directory, does not
                exist, or is not a directory.
        """
        resolved = self._resolve_user_file(user_id, path)
        if not resolved.exists() or not resolved.is_dir():
            raise WorkspaceError(
                f"Directory not found: {path!r}",
                user_id=user_id,
            )

        user_root = self.user_path(user_id).resolve()
        entries: list[dict] = []

        try:
            for entry in resolved.iterdir():
                name = entry.name
                # Skip build artifacts + VCS metadata. Agent-managed
                # dotdirs (e.g., an MCP's `.mcp/` install state) are
                # intentionally NOT hidden so the user can see what their
                # agents have written.
                if name in _EXCLUDED_NAMES:
                    continue

                stat = entry.stat()
                is_dir = entry.is_dir()
                rel_path = str(entry.resolve().relative_to(user_root))

                entries.append(
                    {
                        "name": name,
                        "path": rel_path,
                        "type": "dir" if is_dir else "file",
                        "size": None if is_dir else stat.st_size,
                        "modified_at": stat.st_mtime,
                    }
                )
        except OSError as exc:
            logger.error("Failed to list directory %r for %s: %s", path, user_id, exc)
            raise WorkspaceError(
                f"Failed to list directory {path!r} for {user_id}: {exc}",
                user_id=user_id,
            ) from exc

        # Dirs first, then alphabetically by name (case-insensitive).
        entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))
        return entries

    def read_file_info(self, user_id: str, path: str) -> dict:
        """Return metadata and optionally content for a file.

        Text files are returned as a UTF-8 string. Images are returned as a
        base64-encoded string. All other binary files have ``content=None``.

        Args:
            user_id: The user whose workspace to read from.
            path: Relative path within the user's workspace directory.

        Returns:
            Dict with the following fields::

                {
                    "name": str,
                    "path": str,          # relative to user root
                    "size": int,
                    "modified_at": float,
                    "mime_type": str|None,
                    "binary": bool,
                    "content": str|None,  # text, base64, or None
                }

        Raises:
            WorkspaceError: If the path escapes the user directory, does not
                exist, or is not a file.
        """
        resolved = self._resolve_user_file(user_id, path)
        if not resolved.exists() or not resolved.is_file():
            raise WorkspaceError(
                f"File not found: {path!r}",
                user_id=user_id,
            )

        user_root = self.user_path(user_id).resolve()
        stat = resolved.stat()
        rel_path = str(resolved.resolve().relative_to(user_root))
        mime_type, _ = mimetypes.guess_type(resolved.name)
        mime_type = mime_type or "application/octet-stream"
        suffix = resolved.suffix.lower()

        if suffix in _TEXT_EXTENSIONS:
            try:
                content = resolved.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # File has a text extension but contains non-UTF-8 bytes —
                # treat it as an opaque binary file.
                logger.warning(
                    "File %r for %s has text extension but is not valid UTF-8",
                    path,
                    user_id,
                )
            except OSError as exc:
                logger.error("Failed to read text file %r for %s: %s", path, user_id, exc)
                raise WorkspaceError(
                    f"Failed to read {path!r} for {user_id}: {exc}",
                    user_id=user_id,
                ) from exc
            else:
                return {
                    "name": resolved.name,
                    "path": rel_path,
                    "size": stat.st_size,
                    "modified_at": stat.st_mtime,
                    "mime_type": mime_type,
                    "binary": False,
                    "content": content,
                }

        if mime_type.startswith("image/"):
            try:
                raw = resolved.read_bytes()
                content = base64.b64encode(raw).decode("ascii")
            except OSError as exc:
                logger.error("Failed to read image %r for %s: %s", path, user_id, exc)
                raise WorkspaceError(
                    f"Failed to read {path!r} for {user_id}: {exc}",
                    user_id=user_id,
                ) from exc
            return {
                "name": resolved.name,
                "path": rel_path,
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
                "mime_type": mime_type,
                "binary": True,
                "content": content,
            }

        # Other binary file — return metadata only.
        return {
            "name": resolved.name,
            "path": rel_path,
            "size": stat.st_size,
            "modified_at": stat.st_mtime,
            "mime_type": mime_type,
            "binary": True,
            "content": None,
        }

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def read_file(self, user_id: str, path: str) -> str:
        """Read a text file from a user's workspace.

        Args:
            user_id: The user whose workspace to read from.
            path: Relative path within the user's workspace directory.

        Returns:
            File contents as a string.

        Raises:
            WorkspaceError: If the file does not exist, the path escapes
                the user directory, or a filesystem error occurs.
        """
        resolved = self._resolve_user_file(user_id, path)
        if not resolved.exists():
            raise WorkspaceError(
                f"File not found: {path!r}",
                user_id=user_id,
            )
        try:
            return resolved.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to read %r for %s: %s", path, user_id, exc)
            raise WorkspaceError(
                f"Failed to read {path!r} for {user_id}: {exc}",
                user_id=user_id,
            ) from exc

    def _chown_for_access_point(self, file_path: Path, user_id: str) -> None:
        """Set ownership to match per-user EFS access point POSIX user.

        Chowns the file and all parent directories up to the user root
        so the OpenClaw container (uid 1000 via access point) can read/write.
        Each user's access point is isolated — chowning to 1000:1000 does
        not grant cross-user access.
        """
        if settings.ENVIRONMENT == "local":
            return

        user_root = self.user_path(user_id).resolve()
        try:
            os.chown(file_path, _EFS_USER_UID, _EFS_USER_GID)
            parent = file_path.parent
            while parent >= user_root:
                os.chown(parent, _EFS_USER_UID, _EFS_USER_GID)
                if parent == user_root:
                    break
                parent = parent.parent
        except OSError as exc:
            logger.error("Failed to chown %s for user %s: %s", file_path, user_id, exc)
            raise WorkspaceError(
                f"Failed to set ownership on {file_path} for {user_id}: {exc}",
                user_id=user_id,
            ) from exc

    def write_file(self, user_id: str, path: str, content: str) -> None:
        """Write a text file into a user's workspace.

        Creates parent directories as needed. Sets ownership to 1000:1000
        so per-user EFS access points can read/write.

        Args:
            user_id: The user whose workspace to write into.
            path: Relative path within the user's workspace directory.
            content: Text content to write.

        Raises:
            WorkspaceError: If the path escapes the user directory or a
                filesystem error occurs.
        """
        resolved = self._resolve_user_file(user_id, path)
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            self._chown_for_access_point(resolved, user_id)
        except OSError as exc:
            put_metric("workspace.file.write.error")
            logger.error("Failed to write %r for %s: %s", path, user_id, exc)
            raise WorkspaceError(
                f"Failed to write {path!r} for {user_id}: {exc}",
                user_id=user_id,
            ) from exc

    def write_file_if_missing(self, user_id: str, path: str, content: str) -> bool:
        """Write a text file only if it does not already exist.

        Uses O_EXCL semantics so the existence check + create is atomic at
        the filesystem level — no race window between checking and writing.
        Mirrors OpenClaw's `writeFileIfMissing` (`agents/workspace.ts:183`)
        so any file pre-staged by Isol8 is honored by OpenClaw's own
        bootstrap on first agent run.

        Creates parent directories as needed. Sets ownership to 1000:1000
        so per-user EFS access points can read/write (skipped on local).

        Args:
            user_id: The user whose workspace to write into.
            path: Relative path within the user's workspace directory.
            content: Text content to write.

        Returns:
            True if the file was written, False if it already existed.

        Raises:
            WorkspaceError: If the path escapes the user directory or a
                non-EEXIST filesystem error occurs.
        """
        resolved = self._resolve_user_file(user_id, path)
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with open(resolved, "x", encoding="utf-8") as f:
                f.write(content)
        except FileExistsError:
            return False
        except OSError as exc:
            put_metric("workspace.file.write.error")
            logger.error("Failed to write_if_missing %r for %s: %s", path, user_id, exc)
            raise WorkspaceError(
                f"Failed to write_if_missing {path!r} for {user_id}: {exc}",
                user_id=user_id,
            ) from exc
        self._chown_for_access_point(resolved, user_id)
        return True

    def write_bytes(self, user_id: str, path: str, data: bytes) -> None:
        """Write binary data into a user's workspace.

        Creates parent directories as needed.

        Args:
            user_id: The user whose workspace to write into.
            path: Relative path within the user's workspace directory.
            data: Binary content to write.

        Raises:
            WorkspaceError: If the path escapes the user directory or a
                filesystem error occurs.
        """
        resolved = self._resolve_user_file(user_id, path)
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_bytes(data)
            self._chown_for_access_point(resolved, user_id)
        except OSError as exc:
            put_metric("workspace.file.write.error")
            logger.error("Failed to write %r for %s: %s", path, user_id, exc)
            raise WorkspaceError(
                f"Failed to write {path!r} for {user_id}: {exc}",
                user_id=user_id,
            ) from exc

    def delete_file(self, user_id: str, path: str) -> None:
        """Delete a file from a user's workspace.

        Idempotent: does not raise if the file is already absent.

        Args:
            user_id: The user whose workspace to delete from.
            path: Relative path within the user's workspace directory.

        Raises:
            WorkspaceError: If the path escapes the user directory or a
                filesystem error occurs.
        """
        resolved = self._resolve_user_file(user_id, path)
        if not resolved.exists():
            return
        try:
            resolved.unlink()
        except OSError as exc:
            logger.error("Failed to delete %r for %s: %s", path, user_id, exc)
            raise WorkspaceError(
                f"Failed to delete {path!r} for {user_id}: {exc}",
                user_id=user_id,
            ) from exc

    def delete_user_dir(self, user_id: str) -> None:
        """rm -rf the entire per-user EFS directory.

        Used by the e2e teardown endpoint. Idempotent: silently succeeds
        if the directory doesn't exist. Validates user_id via user_path()
        to defend against path traversal.
        """
        import shutil

        user_dir = self.user_path(user_id)
        if user_dir.exists():
            shutil.rmtree(user_dir)
            logger.info("Deleted EFS user directory for %s", user_id)

    def cleanup_agent_dirs(self, user_id: str, agent_id: str) -> None:
        """Best-effort `rm -rf` for an agent's on-EFS directories after delete.

        OpenClaw's `agents.delete` calls `movePathToTrash`, which on Linux
        Fargate falls back to renaming into `$HOME/.Trash` — a cross-device
        rename from EFS to the container overlay, which fails with EXDEV and
        is silently swallowed. Result: the agent's on-EFS directories leak
        on every delete. We reconcile by removing them from the backend.

        Idempotent and best-effort: missing dirs are ignored, failures are
        logged but never raised — the user's delete already succeeded on the
        OpenClaw side.
        """
        import shutil

        if not agent_id or "/" in agent_id or ".." in agent_id:
            logger.warning("cleanup_agent_dirs: refusing unsafe agent_id %r", agent_id)
            return

        user_root = self.user_path(user_id)
        # On-EFS roots OpenClaw writes to per agent:
        #   agents/{id}/      — agent/ + sessions/ subdirs (internal state)
        #   workspace-{id}/  — workspace files (per agents.defaults.workspace)
        targets = [
            user_root / "agents" / agent_id,
            user_root / f"workspace-{agent_id}",
        ]
        for target in targets:
            if not target.exists():
                continue
            try:
                shutil.rmtree(target)
                logger.info("Cleaned up agent dir %s for user %s", target, user_id)
            except OSError as exc:
                logger.warning(
                    "Failed to clean up agent dir %s for user %s: %s",
                    target,
                    user_id,
                    exc,
                )

    # ------------------------------------------------------------------
    # Agent catalog helpers
    # ------------------------------------------------------------------

    def extract_tarball_to_workspace(
        self,
        user_id: str,
        agent_id: str,
        tar_bytes: bytes,
    ) -> None:
        """Extract a workspace tarball into {mount}/{user_id}/workspace-{agent_id}/.

        Rejects path traversal via catalog_package.untar_to_directory.
        """
        # Deferred import avoids a circular import if catalog_package ever
        # grows a workspace dependency.
        from core.services.catalog_package import untar_to_directory

        target = self.user_path(user_id) / f"workspace-{agent_id}"
        target.mkdir(parents=True, exist_ok=True)
        untar_to_directory(io.BytesIO(tar_bytes), target)

        for path in target.rglob("*"):
            self._chown_for_access_point(path, user_id)
        self._chown_for_access_point(target, user_id)

    def read_template_sidecar(self, user_id: str, agent_id: str) -> dict | None:
        """Return parsed `.template` sidecar JSON for an agent, or None if missing/corrupt."""
        sidecar = self.user_path(user_id) / f"workspace-{agent_id}" / ".template"
        if not sidecar.exists():
            return None
        try:
            return json.loads(sidecar.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def write_template_sidecar(
        self,
        user_id: str,
        agent_id: str,
        content: dict,
    ) -> None:
        """Write a `.template` sidecar JSON for an agent created from a catalog template."""
        sidecar = self.user_path(user_id) / f"workspace-{agent_id}" / ".template"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(content))
        self._chown_for_access_point(sidecar, user_id)

    def read_openclaw_config(self, user_id: str) -> dict | None:
        """Return parsed openclaw.json for a user, or None if missing/corrupt."""
        path = self.user_path(user_id) / "openclaw.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def read_cron_jobs(self, user_id: str) -> list[dict]:
        """Return the user's cron job entries from ``cron/jobs.json``, or [].

        Schema: ``{"version": 1, "jobs": [{...}, ...]}`` — see
        ``openclaw/src/config/types.cron.ts`` for the run-time shape.
        Missing file or corrupt JSON degrades to ``[]`` (callers treat empty
        as "no cron jobs to carry").
        """
        path = self.user_path(user_id) / "cron" / "jobs.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(data, dict):
            return []
        jobs = data.get("jobs")
        return list(jobs) if isinstance(jobs, list) else []

    def agent_workspace_path(self, user_id: str, agent_id: str) -> Path:
        return self.user_path(user_id) / f"workspace-{agent_id}"


# Top-level key in OpenClaw's container-global ``credentials/oauth.json`` is
# the PROVIDER name; the in-container import (mergeOAuthFileIntoStore) builds
# profile ``<provider>:default`` from it. Provider MUST be "openai": the auth
# resolver filters profiles by the credential's provider field and there is NO
# runtime alias from the retired "openai-codex" to "openai" (only
# ``openclaw doctor`` migrates it), so a key of "openai-codex" is silently
# dropped. The deployed chatgpt model ref is ``openai/gpt-5.5`` (config.py),
# which resolves ``openai:default``.
_OAUTH_PROVIDER_KEY = "openai"


def _list_agent_dir_names(efs_root: str, user_id: str) -> list[str]:
    """Names of existing per-agent dirs under ``<EFS>/{user_id}/agents/``.

    Empty when the dir doesn't exist yet (e.g. first provision, before the
    container has booted and created agent dirs). Used to fan auth-profile
    staging *and* cleanup across the owner's real agents (Friday/Atlas/custom)
    discovered on disk — not only the implicit ``main`` — so callers that
    don't thread an explicit agent list (provider hot-swap, running redeploy,
    disconnect) still cover every agent.
    """
    agents_root = Path(efs_root) / user_id / "agents"
    try:
        return [d.name for d in agents_root.iterdir() if d.is_dir()]
    except (FileNotFoundError, NotADirectoryError):
        return []


async def pre_stage_auth_profile_store(*, user_id: str, oauth_tokens: dict) -> None:
    """Write the owner's ChatGPT OAuth credential to EFS before container start.

    Writes ONE container-global file
    ``<EFS>/users/{user_id}/credentials/oauth.json`` in OpenClaw's flat
    ``Record<provider, OAuthCredentials>`` shape, e.g.::

        {"openai": {"access": "...", "refresh": "...", "expires": 1730000000000}}

    OpenClaw 6.6 reads per-agent auth from a WAL SQLite store and IGNORES the
    legacy per-agent ``auth-profiles.json``. The in-container process imports
    this file into each agent's SQLite store on first load (building profile
    ``openai:default``), and a cross-agent ``(provider, profileId)`` refresh
    lock serializes token rotation across agents -- so a SINGLE shared file
    authenticates every agent. This replaces the per-agent fan-out, which
    broke under OpenAI's refresh-token rotation (``refresh_token_reused``): N
    independent copies each spent the same one-time refresh token. See
    project_openclaw_oauth_auth_model.

    The EFS access point chroots ``/users/{user_id}`` to
    ``/home/node/.openclaw``, so the file is read at
    ``/home/node/.openclaw/credentials/oauth.json`` =
    ``$STATE_DIR/credentials/oauth.json`` (``resolveOAuthPath`` upstream).

    Re-OAuth overwrites the file. NOTE: 6.6 imports it only into an ABSENT
    SQLite store row (mergeOAuthFileIntoStore runs only when
    loadPersistedAuthProfileStore returns null), so rewriting this file does NOT
    by itself update an already-seeded container. Callers that re-stage onto an
    existing container (switch/reconnect into chatgpt_oauth, reprovision) MUST
    also call ``clear_default_agent_auth_profile_row`` so the import re-fires on
    the next run. After first import the in-container OAuth manager refreshes the
    token in place.

    Args:
        user_id: the container owner id (org_id for orgs, user_id personal).
        oauth_tokens: dict with required keys ``access_token`` and
            ``refresh_token``, optional ``expires_at`` (ms epoch), ``email``,
            ``account_id``. Sourced from oauth_service.get_decrypted_tokens.

    Note: tokens are plaintext on EFS. Security boundary is EFS KMS
    encryption-at-rest + per-user access-point UID/GID isolation, NOT in-file
    encryption -- the container can't decrypt at boot.
    """
    if not _USER_ID_PATTERN.match(user_id):
        raise ValueError(f"user_id contains invalid characters: {user_id!r}")

    required = {"access_token", "refresh_token"}
    missing = required - set(oauth_tokens.keys())
    if missing:
        raise ValueError(f"oauth_tokens missing required keys: {sorted(missing)}")

    efs_root = os.environ.get("EFS_MOUNT_PATH") or settings.EFS_MOUNT_PATH
    if not efs_root:
        raise RuntimeError("EFS_MOUNT_PATH is empty - backend is misconfigured.")

    # Flat OAuthCredentials: keys are ``access``/``refresh``/``expires``, NOT
    # the DDB ``access_token``/``refresh_token``/``expires_at``. Extra keys
    # (``accountId``) are carried verbatim into the profile by the import.
    creds: dict = {
        "access": oauth_tokens["access_token"],
        "refresh": oauth_tokens["refresh_token"],
    }
    # ``expires`` is a ms-epoch number per upstream OAuthCredentials. When
    # absent, OpenClaw treats the access token as already-expired and refreshes
    # on first use -- fine for cold provision since DDB tokens may have aged.
    if oauth_tokens.get("expires_at") is not None:
        creds["expires"] = int(oauth_tokens["expires_at"])
    if oauth_tokens.get("email"):
        creds["email"] = oauth_tokens["email"]
    if oauth_tokens.get("account_id"):
        creds["accountId"] = oauth_tokens["account_id"]

    payload = json.dumps({_OAUTH_PROVIDER_KEY: creds}, indent=2)

    base = Path(efs_root) / user_id / "credentials"
    base.mkdir(parents=True, exist_ok=True)
    # Atomic write: .tmp + os.replace (crash-safe on POSIX, same filesystem) --
    # a re-OAuth that crashes mid-write must not leave OpenClaw corrupt JSON.
    oauth_path = base / "oauth.json"
    tmp_path = base / "oauth.json.tmp"
    tmp_path.write_text(payload)
    # File holds long-lived OAuth tokens -- owner-only.
    try:
        os.chmod(str(tmp_path), 0o600)
    except OSError:
        pass
    # OpenClaw runs as UID 1000 -- match ownership so its in-container import +
    # token refresh can read/write. Best-effort.
    try:
        os.chown(str(tmp_path), 1000, 1000)
    except (PermissionError, OSError):
        pass
    os.replace(str(tmp_path), str(oauth_path))
    for dir_path in (base, base.parent):
        try:
            os.chown(str(dir_path), 1000, 1000)
        except (PermissionError, OSError):
            pass

    logger.info(
        "pre_staged_oauth_json",
        extra={
            "user_id": user_id,
            "has_email": "email" in creds,
            "has_expires": "expires" in creds,
        },
    )


async def delete_auth_profile_store(*, user_id: str) -> None:
    """Remove the staged ChatGPT credential from EFS on OAuth disconnect/switch.

    Deletes the container-global ``credentials/oauth.json`` AND sweeps any
    legacy per-agent ``agents/*/agent/auth-profiles.json`` left by the pre-6.6
    fan-out, so a revoked owner can't re-import a stale token on the next boot
    and no agent keeps a usable refresh token on EFS. (The in-container SQLite
    store is the container's own state and is not touched here; a reprovision
    starts it fresh.)
    """
    if not _USER_ID_PATTERN.match(user_id):
        raise ValueError(f"user_id contains invalid characters: {user_id!r}")

    efs_root = os.environ.get("EFS_MOUNT_PATH") or settings.EFS_MOUNT_PATH
    if not efs_root:
        return  # No EFS configured (e.g. tests) -- nothing to delete.

    removed: list[str] = []
    oauth_path = Path(efs_root) / user_id / "credentials" / "oauth.json"
    try:
        if oauth_path.exists():
            oauth_path.unlink()
            removed.append("credentials/oauth.json")
    except OSError as e:
        logger.warning("delete_auth_profile_store: %s -- %s", oauth_path, e)

    # Sweep legacy per-agent files left by the pre-6.6 fan-out.
    for agent_id in dict.fromkeys(["main", *_list_agent_dir_names(efs_root, user_id)]):
        legacy = Path(efs_root) / user_id / "agents" / agent_id / "agent" / "auth-profiles.json"
        try:
            if legacy.exists():
                legacy.unlink()
                removed.append(f"agents/{agent_id}/agent/auth-profiles.json")
        except OSError as e:
            logger.warning("delete_auth_profile_store: %s -- %s", legacy, e)

    logger.info("deleted_oauth_credential", extra={"user_id": user_id, "removed": removed})


# The default agent's on-disk dir name. OpenClaw's DEFAULT agent (Isol8's
# Friday, config.py role=='ceo') is the single credential source; every other
# agent reads through it (mergeAuthProfileStores(MAIN, agent)). Its store lives
# at <EFS>/{user_id}/agents/main/agent/openclaw-agent.sqlite.
_DEFAULT_AGENT_DIR = "main"


def _delete_default_agent_auth_row_blocking(db_path: Path) -> tuple[int, str | None]:
    """Blocking SQLite delete of the default agent's ``primary`` store row.

    Isolates the synchronous sqlite3 work — which, with a busy-timeout, can block
    for seconds under lock contention — so the async caller can run it in a worker
    thread and keep the event loop free. Returns ``(deleted_rows, failure)``:
    ``failure`` is None on success, ``"no_table"`` when the store has no
    auth_profile_store table yet (benign), or ``"write_failed: …"`` for a genuine
    write error (e.g. SQLITE_BUSY past the timeout).
    """
    try:
        # ``timeout`` is SQLite's busy-timeout (seconds) — wait out the live
        # writer's lock rather than failing fast with SQLITE_BUSY.
        con = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            cur = con.execute("DELETE FROM auth_profile_store WHERE store_key = 'primary'")
            con.commit()
            return cur.rowcount, None
        finally:
            con.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return 0, "no_table"
        return 0, f"write_failed: {e}"


async def cleanup_deleted_agent(user_id: str, agent_id: str, *, workspace: "Workspace | None" = None) -> None:
    """Reclaim everything a deleted agent leaves behind: its EFS directories
    AND its Litestream replica prefix.

    Single chokepoint for every ``agents.delete`` path (chat, admin, failed
    catalog deploy). ``workspace`` overrides the singleton for callers that
    hold an injected instance (catalog_service). The replica half matters because the boot script restores
    from the S3 prefix rather than from openclaw.json — an orphaned prefix
    re-materializes the deleted agent's database on every subsequent boot,
    forever. Best-effort: the delete itself already succeeded upstream.
    """
    ws = workspace or get_workspace()
    try:
        await asyncio.to_thread(ws.cleanup_agent_dirs, user_id, agent_id)
    except Exception as exc:  # noqa: BLE001 — post-delete reclamation
        logger.warning("cleanup_deleted_agent: EFS cleanup failed for %s/%s: %s", user_id, agent_id, exc)
    from core.services.litestream_replicas import delete_replica_prefix

    await delete_replica_prefix(user_id, agent_id=agent_id)


# O6 local-SQLite layout: sentinel on the EFS durable spine asking the
# container's boot script to delete the default agent's auth_profile_store row
# (before gateway start) so a freshly staged credentials/oauth.json re-imports.
# Written when the backend cannot reach the store directly (it lives on
# container-local disk); consumed + deleted by the boot script, which skips
# missing DBs and tolerates a missing table (a fresh container has no row and
# imports the staged file anyway).
O6_CLEAR_AUTH_SENTINEL_FILENAME = "o6-clear-auth-profiles.json"


def _write_o6_clear_auth_sentinel(user_root: Path) -> None:
    # Written against the SAME efs_root the caller resolved (env override
    # included) rather than the Workspace singleton, which binds its mount
    # path from settings at first construction. The boot script runs as root,
    # so ownership is cosmetic — chown kept for fleet-convention parity.
    path = user_root / O6_CLEAR_AUTH_SENTINEL_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "agent_ids": [_DEFAULT_AGENT_DIR],
                "requested_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        os.chown(path, _EFS_USER_UID, _EFS_USER_GID)
    except OSError:
        pass


async def clear_default_agent_auth_profile_row(*, user_id: str) -> bool | str:
    """Delete the DEFAULT agent's persisted auth-profile store row so OpenClaw
    6.6 re-imports a freshly staged ``credentials/oauth.json`` on its next load.

    Why DELETE the row instead of emptying it: 6.6's ``mergeOAuthFileIntoStore``
    (the only importer of ``oauth.json``) runs ONLY when
    ``loadPersistedAuthProfileStore`` returns null — i.e. when there is no
    persisted store row at all. ``models.authLogout`` (what disconnect calls)
    deletes the *profiles* but saves the store, leaving a row of
    ``{"version":1,"profiles":{}}`` that still coerces to a non-null store and
    shadows the import forever. Deleting the row makes the store absent, so the
    next agent run re-imports the fresh token (the store is re-read per run and
    the auth-epoch resets reused sessions). See project_openclaw_oauth_auth_model.

    Targets ONLY the default agent (``main``): it is the single credential source
    and every other agent reads through it. Clearing non-default rows would make
    each agent re-import its own copy of the one-time refresh token →
    ``refresh_token_reused`` fan-out.

    The 6.6 store is rollback-journal SQLite on EFS (NOT WAL — #91247 moved it off
    WAL on NFS), so this short, lock-serialized DELETE is corruption-safe even
    while the container is running; the busy-timeout absorbs transient contention
    with the live writer.

    Returns True if a row was deleted, False on no-op (no store yet / no row /
    contention we couldn't win), or the string ``"restart_required"`` on the
    O6 local-SQLite layout (``settings.LITESTREAM_BUCKET`` set and no store on
    EFS): the row lives on container-local disk the backend cannot reach, so a
    sentinel is written to the EFS durable spine instead and only a container
    boot applies it — the CALLER decides whether to bounce a running container
    (never-booted containers consume the sentinel on first boot anyway).
    Never raises on a missing store or table.
    """
    if not _USER_ID_PATTERN.match(user_id):
        raise ValueError(f"user_id contains invalid characters: {user_id!r}")

    efs_root = os.environ.get("EFS_MOUNT_PATH") or settings.EFS_MOUNT_PATH
    if not efs_root:
        return False  # No EFS configured (e.g. tests without it) — nothing to do.

    db_path = Path(efs_root) / user_id / "agents" / _DEFAULT_AGENT_DIR / "agent" / "openclaw-agent.sqlite"
    if not db_path.exists():
        # A store present on EFS (the arm below) always wins: during an env's
        # cutover window, old-layout containers keep the hot in-place DELETE.
        if settings.LITESTREAM_BUCKET:
            _write_o6_clear_auth_sentinel(Path(efs_root) / user_id)
            logger.info(
                "clear_default_agent_auth_profile_row: wrote O6 sentinel for %s (store is container-local)",
                user_id,
            )
            return "restart_required"
        return False  # Container never booted / no per-agent store yet.

    # Run the blocking sqlite work (incl. the busy-timeout wait) off the event
    # loop so lock contention with the live writer can't stall the backend.
    deleted, failure = await asyncio.to_thread(_delete_default_agent_auth_row_blocking, db_path)

    if failure == "no_table":
        # Store file exists but the auth_profile_store table was never created
        # (container booted but no auth ever persisted). A true no-op: the boot
        # import seeds it. Quiet — NOT a failure.
        logger.info("clear_default_agent_auth_profile_row: no auth table yet for %s", user_id)
        return False
    if failure:
        # A genuine write failure — almost always SQLITE_BUSY: the live OpenClaw
        # process held the auth-store write lock past the busy-timeout. This is
        # NOT benign: the stale shadow row SURVIVES, so the freshly staged
        # oauth.json will NOT import and a switch/reconnect silently keeps the old
        # (often spent) token while reporting success. Surface it loudly +
        # metered so it's observable and retried, instead of masked as a no-op.
        logger.error(
            "clear_default_agent_auth_profile_row: write FAILED for %s — stale auth row "
            "survives; freshly staged oauth.json will NOT import until this is retried (%s)",
            user_id,
            failure,
        )
        put_metric("oauth.auth_row_clear_failed")
        return False

    logger.info(
        "cleared_default_agent_auth_row",
        extra={"user_id": user_id, "agent": _DEFAULT_AGENT_DIR, "deleted_rows": deleted},
    )
    return deleted > 0
