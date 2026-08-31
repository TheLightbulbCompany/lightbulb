"""REST endpoints for browsing agent workspace files on EFS."""

import logging
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.containers import get_workspace
from core.containers.workspace import WorkspaceError

logger = logging.getLogger(__name__)

router = APIRouter()


# NOTE: _validate_relative_path, _ensure_within_subtree and _collect_recursive
# are imported by routers/brain.py — its security posture depends on these.
def _validate_relative_path(path: str) -> None:
    """Reject empty paths, absolute paths, dot-only paths, and any segment equal to '..'.

    Raises ValueError with a message suitable for surfacing as a 400 detail.
    """
    if not path or path.startswith(("/", "\\")):
        raise ValueError("path must be a non-empty relative path")

    normalized = path.replace("\\", "/")
    # Reject dot-only paths: '.', './', './.', etc. collapse to empty parts.
    segments = [s for s in normalized.split("/") if s != ""]
    if not segments or all(s == "." for s in segments):
        raise ValueError("path must not be a dot-only path")
    if any(s == ".." for s in segments):
        raise ValueError("path must not contain '..' segments")
    # Existing PurePosixPath-based checks are still useful as belt-and-suspenders
    parts = PurePosixPath(path).parts
    if any(part in ("..", "") for part in parts):
        raise ValueError("path must not contain '..' segments")


def _ensure_within_subtree(workspace, owner_id: str, full_path: str, subtree: str) -> None:
    """Resolve full_path (following symlinks) and verify it remains within
    {user_root}/{subtree}.

    This blocks symlink-based escapes that would otherwise pass
    `Workspace._resolve_user_file`, which only pins writes to the user root.

    Raises:
        ValueError: if the resolved target escapes the subtree.
    """
    user_root = workspace.user_path(owner_id).resolve()
    subtree_root = (user_root / subtree).resolve()
    # strict=False lets us resolve paths whose leaf doesn't exist yet (the file
    # being created). Intermediate symlinks in the parent chain are still
    # resolved.
    target = (user_root / full_path).resolve(strict=False)
    try:
        target.relative_to(subtree_root)
    except ValueError:
        raise ValueError(f"path escapes agent subtree: {full_path!r}")


def _agent_workspace_path(owner_id: str, agent_id: str) -> str:
    """Build the relative path to an agent's workspace within the user dir."""
    if "/" in agent_id or "\\" in agent_id or ".." in agent_id:
        raise HTTPException(status_code=400, detail="Invalid agent_id")
    return f"workspace-{agent_id}"


def _guard_read_subpath(workspace, owner_id: str, agent_base: str, full_path: str, path: str) -> None:
    """Path-traversal guard for the workspace READ endpoints.

    The write path already validates via _write_file; the read endpoints
    historically relied on Workspace._resolve_user_file, which only pins to the
    user root (not the per-agent subtree) — so `path=../<other_agent>/...` read
    across workspaces within the tenant. This mirrors the write path: reject
    '..' segments and confirm the resolved target stays inside the agent's own
    workspace subtree (which also blocks symlink escapes). `path` may be empty
    for the tree root.
    """
    if path:
        try:
            _validate_relative_path(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    try:
        _ensure_within_subtree(workspace, owner_id, full_path, agent_base)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")


def _within_subtree(workspace, owner_id: str, rel_path: str, subtree: str) -> bool:
    """Boolean form of _ensure_within_subtree for filtering listing entries."""
    try:
        _ensure_within_subtree(workspace, owner_id, rel_path, subtree)
        return True
    except ValueError:
        return False


def _collect_recursive(workspace, owner_id: str, path: str, entries: list, subtree: str, max_depth: int = 10):
    """Recursively collect file entries up to max_depth, staying inside ``subtree``.

    Guarding only the root path isn't enough: list_directory resolves child paths
    relative to the user root, not the agent subtree, so a symlinked subdir like
    ``workspace-agent-a/link -> ../agent-b`` would otherwise let the walk
    enumerate a sibling workspace. We re-check every entry (files included, so a
    symlinked file is dropped too) and never descend into an escaping dir.
    """
    if max_depth <= 0:
        return
    try:
        items = workspace.list_directory(owner_id, path)
    except WorkspaceError:
        return
    for item in items:
        if not _within_subtree(workspace, owner_id, item["path"], subtree):
            continue
        entries.append(item)
        if item["type"] == "dir":
            _collect_recursive(workspace, owner_id, item["path"], entries, subtree, max_depth - 1)


def _strip_agent_prefix(entries: list[dict], agent_id: str) -> list[dict]:
    """Rewrite entry paths from user-root-relative to agent-workspace-relative.

    Input paths look like `workspace-{agent_id}/foo/bar.md`. Output paths
    strip the `workspace-{agent_id}/` prefix so they match the contract
    expected by the read/write endpoints.
    """
    prefix = f"workspace-{agent_id}/"
    out = []
    for entry in entries:
        p = entry["path"]
        if p == f"workspace-{agent_id}":
            # The agent root itself — present when the workspace dir is listed at depth 0.
            new_path = ""
        elif p.startswith(prefix):
            new_path = p[len(prefix) :]
        else:
            # Shouldn't happen — log and keep as-is.
            logger.warning("Unexpected workspace path %r for agent %s", p, agent_id)
            new_path = p
        out.append({**entry, "path": new_path})
    return out


# The 7 config files OpenClaw seeds in every agent workspace.
# Source: desktop/openclaw/src/agents/workspace.ts. These are the only
# filenames the Config tab treats as first-class personality/config files;
# writes via tab="config" are restricted to this set.
CONFIG_ALLOWLIST: set[str] = {
    "SOUL.md",
    "MEMORY.md",
    "TOOLS.md",
    "IDENTITY.md",
    "USER.md",
    "HEARTBEAT.md",
    "AGENTS.md",
}

MAX_WRITE_SIZE = 10 * 1024 * 1024  # 10 MB


def _list_config_files(workspace, owner_id: str, agent_id: str) -> list[dict]:
    """List only allowlisted config files from the agent's workspace.

    Config files (SOUL.md, MEMORY.md, etc.) live inside workspace-{agent_id}/
    alongside the agent's working files. They are NOT stored in agents/{agent_id}/
    — that directory holds OpenClaw runtime metadata (sessions, models) which
    is intentionally unreachable from the file viewer.
    """
    if "/" in agent_id or "\\" in agent_id or ".." in agent_id:
        return []
    user_root = workspace.user_path(owner_id)
    agent_dir = user_root / f"workspace-{agent_id}"
    if not agent_dir.exists() or not agent_dir.is_dir():
        return []
    results = []
    for name in sorted(CONFIG_ALLOWLIST):
        fpath = agent_dir / name
        if fpath.exists() and fpath.is_file():
            stat = fpath.stat()
            results.append(
                {
                    "name": name,
                    "path": name,
                    "type": "file",
                    "size": stat.st_size,
                    "modified_at": stat.st_mtime,
                }
            )
    return results


@router.get("/workspace/{agent_id}/tree")
async def list_workspace_tree(
    agent_id: str,
    path: str = Query("", description="Subdirectory path relative to agent workspace"),
    recursive: bool = Query(False, description="List all files recursively"),
    auth: AuthContext = Depends(get_current_user),
):
    """List files and directories in an agent's workspace."""
    owner_id = resolve_owner_id(auth)
    workspace = get_workspace()

    agent_base = _agent_workspace_path(owner_id, agent_id)
    full_path = f"{agent_base}/{path}" if path else agent_base
    _guard_read_subpath(workspace, owner_id, agent_base, full_path, path)

    if recursive:
        all_entries: list = []
        _collect_recursive(workspace, owner_id, full_path, all_entries, agent_base)
        return {"files": _strip_agent_prefix(all_entries, agent_id)}
    else:
        try:
            entries = workspace.list_directory(owner_id, full_path)
        except WorkspaceError as exc:
            if "not found" in str(exc).lower():
                raise HTTPException(status_code=404, detail=str(exc))
            if "traversal" in str(exc).lower():
                raise HTTPException(status_code=403, detail="Access denied")
            logger.exception("Failed to list workspace directory for owner %s", owner_id)
            raise HTTPException(status_code=500, detail="Failed to list workspace directory")

        # Drop immediate entries that resolve outside the agent subtree (e.g. a
        # symlink to a sibling workspace) so they're neither shown nor clickable.
        entries = [e for e in entries if _within_subtree(workspace, owner_id, e["path"], agent_base)]
        return {"files": _strip_agent_prefix(entries, agent_id)}


@router.get("/workspace/{agent_id}/file")
async def read_workspace_file(
    agent_id: str,
    path: str = Query(..., description="File path relative to agent workspace"),
    auth: AuthContext = Depends(get_current_user),
):
    """Read a file's content and metadata from an agent's workspace."""
    owner_id = resolve_owner_id(auth)
    workspace = get_workspace()

    agent_base = _agent_workspace_path(owner_id, agent_id)
    full_path = f"{agent_base}/{path}"
    _guard_read_subpath(workspace, owner_id, agent_base, full_path, path)

    try:
        info = workspace.read_file_info(owner_id, full_path)
    except WorkspaceError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        if "traversal" in str(exc).lower():
            raise HTTPException(status_code=403, detail="Access denied")
        logger.exception("Failed to read workspace file for owner %s", owner_id)
        raise HTTPException(status_code=500, detail="Failed to read workspace file")

    return info


@router.get("/workspace/{agent_id}/config-files")
async def list_config_files(
    agent_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    """List allowlisted agent config files (SOUL.md, MEMORY.md, etc.)."""
    owner_id = resolve_owner_id(auth)
    workspace = get_workspace()
    if "/" in agent_id or "\\" in agent_id or ".." in agent_id:
        raise HTTPException(status_code=400, detail="Invalid agent_id")
    return {"files": _list_config_files(workspace, owner_id, agent_id)}


@router.get("/workspace/{agent_id}/config-file")
async def read_config_file(
    agent_id: str,
    path: str = Query(..., description="Config filename (must be allowlisted)"),
    auth: AuthContext = Depends(get_current_user),
):
    """Read a single allowlisted agent config file."""
    owner_id = resolve_owner_id(auth)
    if path not in CONFIG_ALLOWLIST:
        raise HTTPException(status_code=400, detail=f"File not in allowlist: {path}")
    if "/" in agent_id or "\\" in agent_id or ".." in agent_id:
        raise HTTPException(status_code=400, detail="Invalid agent_id")
    workspace = get_workspace()
    full_path = f"workspace-{agent_id}/{path}"
    try:
        info = workspace.read_file_info(owner_id, full_path)
    except WorkspaceError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))
    return info


class WriteFileRequest(BaseModel):
    path: str
    content: str
    tab: str  # "workspace" or "config"


def _write_file(workspace, owner_id: str, agent_id: str, path: str, content: str, tab: str) -> str:
    """Write a file into the agent's workspace. Returns the user-root-relative path.

    Both tabs ("workspace" and "config") write under the same subtree:
    workspace-{agent_id}/. The tab controls the allowlist gate only — the
    config tab is restricted to the 7 personality files (SOUL.md etc.), the
    workspace tab accepts arbitrary relative paths.
    """
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_WRITE_SIZE:
        raise ValueError(f"content exceeds {MAX_WRITE_SIZE // (1024 * 1024)}MB limit")

    if "/" in agent_id or "\\" in agent_id or ".." in agent_id:
        raise ValueError(f"Invalid agent_id: {agent_id!r}")

    _validate_relative_path(path)

    if tab == "config":
        if path not in CONFIG_ALLOWLIST:
            raise ValueError(f"File not in allowlist: {path}")
    elif tab != "workspace":
        raise ValueError(f"Invalid tab: {tab!r}")

    subtree = f"workspace-{agent_id}"
    full_path = f"{subtree}/{path}"
    _ensure_within_subtree(workspace, owner_id, full_path, subtree)

    workspace.write_file(owner_id, full_path, content)
    return full_path


@router.put("/workspace/{agent_id}/file")
async def write_workspace_file(
    agent_id: str,
    body: WriteFileRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Write a file to the agent's workspace or config directory."""
    owner_id = resolve_owner_id(auth)
    if "/" in agent_id or "\\" in agent_id or ".." in agent_id:
        raise HTTPException(status_code=400, detail="Invalid agent_id")
    if body.tab not in ("workspace", "config"):
        raise HTTPException(status_code=400, detail="tab must be 'workspace' or 'config'")

    workspace = get_workspace()
    try:
        written_path = _write_file(workspace, owner_id, agent_id, body.path, body.content, body.tab)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except WorkspaceError as exc:
        if "traversal" in str(exc).lower():
            raise HTTPException(status_code=403, detail="Access denied")
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info("Wrote %s for user %s (tab=%s)", written_path, owner_id, body.tab)
    return {"status": "ok", "path": written_path}
