"""Direct EFS access for per-user OpenClaw workspaces.

Each user's workspace is stored at:
    {mount_path}/{user_id}/

Agent workspaces live under:
    {mount_path}/{user_id}/agents/{agent_name}/

This module handles filesystem CRUD for agent workspaces on EFS.
No S3 or Docker involved -- plain file operations on a mounted volume.
"""

import base64
import io
import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Optional

from core.config import settings
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

# Clerk user ids look like `user_xxxxxx`. Restrict to that character class so
# any path component can never contain `/`, `..`, or other traversal payloads
# when interpolated into an EFS path.
_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# System files/dirs to hide from every directory listing, regardless of depth.
# Anything that could legitimately exist as a user file at a deeper path (e.g.,
# a `state/` subdir inside a project) does NOT belong here — see
# routers/workspace_files.py for root-only exclusions.
_EXCLUDED_NAMES: set[str] = {
    "openclaw.json",
    ".openclaw",
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
            WorkspaceError: If the resolved path escapes the user directory
                (path traversal attack).
        """
        user_dir = self.user_path(user_id).resolve()
        resolved = (user_dir / path).resolve()
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
        """List agent_ids that have a workspace directory under workspaces/.

        This reflects the set of agents with on-EFS workspace files (created
        by agent CRUD or catalog deploy), independent of OpenClaw's runtime
        ``agents/`` state. A just-deployed agent lands in ``workspaces/``
        immediately, but only appears under ``agents/`` after OpenClaw
        processes the new ``openclaw.json`` — so the deploy-provenance
        lookup must scan this directory.
        """
        workspaces_dir = self.user_path(user_id) / "workspaces"
        if not workspaces_dir.exists():
            return []
        return sorted(d.name for d in workspaces_dir.iterdir() if d.is_dir())

    def list_directory(self, user_id: str, path: str) -> list[dict]:
        """List the contents of a directory in a user's workspace.

        Hidden entries (starting with '.') and system files/dirs defined in
        ``_EXCLUDED_NAMES`` are omitted from the results.

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
                # Skip hidden entries and excluded system names.
                if name.startswith(".") or name in _EXCLUDED_NAMES:
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
        #   workspaces/{id}/  — workspace files (per agents.defaults.workspace)
        targets = [
            user_root / "agents" / agent_id,
            user_root / "workspaces" / agent_id,
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
        """Extract a workspace tarball into {mount}/{user_id}/workspaces/{agent_id}/.

        Rejects path traversal via catalog_package.untar_to_directory.
        """
        # Deferred import avoids a circular import if catalog_package ever
        # grows a workspace dependency.
        from core.services.catalog_package import untar_to_directory

        target = self.user_path(user_id) / "workspaces" / agent_id
        target.mkdir(parents=True, exist_ok=True)
        untar_to_directory(io.BytesIO(tar_bytes), target)

        for path in target.rglob("*"):
            self._chown_for_access_point(path, user_id)
        self._chown_for_access_point(target, user_id)

    def read_template_sidecar(self, user_id: str, agent_id: str) -> dict | None:
        """Return parsed `.template` sidecar JSON for an agent, or None if missing/corrupt."""
        sidecar = self.user_path(user_id) / "workspaces" / agent_id / ".template"
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
        sidecar = self.user_path(user_id) / "workspaces" / agent_id / ".template"
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
        return self.user_path(user_id) / "workspaces" / agent_id


# OpenClaw's auth-profile store schema. Mirrors the upstream
# AuthProfileStore type defined in src/agents/auth-profiles/types.ts; the
# version constant is `AUTH_STORE_VERSION = 1` from
# src/agents/auth-profiles/constants.ts at the openclaw version we're
# pinned to (2026.5.7). Bump _AUTH_STORE_VERSION here when bumping
# openclaw-version.json#upstream past a release that changes the schema.
_AUTH_STORE_VERSION = 1

# Verbatim copy of upstream's CODEX_CLI_PROFILE_ID constant — same file
# as above. Used as the key in AuthProfileStore.profiles for ChatGPT OAuth
# credentials so OpenClaw's auth-profile resolver finds the row by its
# canonical id.
_CODEX_CLI_PROFILE_ID = "openai-codex:codex-cli"


async def pre_stage_auth_profile_store(*, user_id: str, oauth_tokens: dict) -> None:
    """Write the user's ChatGPT OAuth profile to EFS before container start.

    Writes ``<EFS>/users/{user_id}/agents/main/agent/auth-profiles.json``
    in OpenClaw's AuthProfileStore JSON shape so the openai-codex provider
    finds the credentials at boot. The EFS access-point chroots
    ``/users/{user_id}`` to ``/home/node/.openclaw`` inside the container,
    so the file is read at ``/home/node/.openclaw/agents/main/agent/auth-profiles.json``
    — the path OpenClaw's runtime probes for `agent="main"` (the implicit
    default agent declared in our openclaw.json).

    See ``docs/superpowers/specs/2026-04-29-chatgpt-oauth-auth-profiles-design.md``.

    Re-OAuth callers can re-invoke this helper; the file is overwritten,
    not merged. After provision, OpenClaw owns the file's lifecycle and
    refreshes the access token in-place using the long-lived refresh token.

    Args:
        user_id: our internal user_id.
        oauth_tokens: dict with required keys ``access_token`` and
            ``refresh_token``, optional keys ``expires_at`` (ms epoch) and
            ``email``. Sourced from oauth_service.get_decrypted_tokens.

    Note: tokens are written in plaintext to EFS. Security boundary is
    EFS encryption-at-rest (KMS) + per-user EFS access point UID/GID
    isolation, NOT in-file encryption — the container can't decrypt at
    boot, so encrypting here would just break OpenClaw.
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

    # OpenClaw's auth-profile resolver reads <agentDir>/auth-profiles.json
    # where agentDir for the implicit "main" agent is
    # <openclaw_home>/agents/main/agent/. Our openclaw.json declares the
    # main agent in agents.list (see config.py).
    base = Path(efs_root) / user_id / "agents" / "main" / "agent"
    base.mkdir(parents=True, exist_ok=True)

    profile: dict = {
        "type": "oauth",
        "provider": "openai-codex",
        "access": oauth_tokens["access_token"],
        "refresh": oauth_tokens["refresh_token"],
    }
    # `expires` is a ms-epoch number per upstream OAuthCredentials type.
    # When absent, OpenClaw treats the access token as already-expired and
    # refreshes on first use; that's correct behavior for cold provision
    # since our DDB tokens may have aged.
    if oauth_tokens.get("expires_at") is not None:
        profile["expires"] = int(oauth_tokens["expires_at"])
    if oauth_tokens.get("email"):
        profile["email"] = oauth_tokens["email"]

    payload = {
        "version": _AUTH_STORE_VERSION,
        "profiles": {_CODEX_CLI_PROFILE_ID: profile},
    }

    # Atomic write: write to .tmp + os.replace. Truncate-then-write isn't
    # crash-atomic — a re-OAuth that crashes mid-write would leave OpenClaw
    # with corrupt JSON. os.replace is atomic on POSIX (same filesystem).
    auth_path = base / "auth-profiles.json"
    tmp_path = base / "auth-profiles.json.tmp"
    tmp_path.write_text(json.dumps(payload, indent=2))

    # File contains long-lived OAuth tokens — owner-only.
    try:
        os.chmod(str(tmp_path), 0o600)
    except OSError:
        pass

    # OpenClaw container runs as UID 1000 — match ownership so the
    # in-container token-refresh logic can rotate the file. Best-effort;
    # ignore failures (e.g. local dev without root, test tmp_path).
    try:
        os.chown(str(tmp_path), 1000, 1000)
    except (PermissionError, OSError):
        pass

    os.replace(str(tmp_path), str(auth_path))

    # Chown the parent dirs (best-effort, separate from the file).
    for dir_path in (base, base.parent, base.parent.parent):
        try:
            os.chown(str(dir_path), 1000, 1000)
        except (PermissionError, OSError):
            pass

    logger.info(
        "pre_staged_auth_profile_store",
        extra={"user_id": user_id, "has_email": "email" in profile},
    )


async def delete_auth_profile_store(*, user_id: str) -> None:
    """Remove the staged auth-profiles.json from the user's EFS workspace.

    Paired with pre_stage_auth_profile_store. Called on OAuth disconnect
    so the container can no longer read tokens cold even if it doesn't
    restart.
    """
    if not _USER_ID_PATTERN.match(user_id):
        raise ValueError(f"user_id contains invalid characters: {user_id!r}")

    efs_root = os.environ.get("EFS_MOUNT_PATH") or settings.EFS_MOUNT_PATH
    if not efs_root:
        return  # No EFS configured (e.g. tests) — nothing to delete.

    auth_path = Path(efs_root) / user_id / "agents" / "main" / "agent" / "auth-profiles.json"
    try:
        auth_path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("delete_auth_profile_store: %s — %s", auth_path, e)
    else:
        logger.info("deleted_auth_profile_store", extra={"user_id": user_id})
