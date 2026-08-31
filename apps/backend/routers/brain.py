"""Owner routes over the memory-wiki vault at {user_root}/wiki/ on EFS.

The vault is written by the container-side memory-wiki plugin (usually under
wiki/main/). These routes give the owner a browse/edit/delete surface over it,
pinned to the ``wiki`` subtree with the same guard machinery
``routers/workspace_files.py`` uses for agent workspaces.

Lock-vs-plugin note: writes take the repo's two-layer lock (per-file
in-process ``threading.Lock`` + ``fcntl.lockf`` on a ``{file}.lock`` sidecar —
see ``core/services/efs_file_lock.py``), which serializes BACKEND writers.
The container's wiki_apply does NOT take this sidecar, so a same-second
owner-vs-agent collision is last-writer-wins on the inode swap — accepted
for v1.
"""

import asyncio
import fcntl
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.containers import get_workspace
from core.containers.workspace import WorkspaceError
from core.services.efs_file_lock import get_file_lock, open_lock_fd
from routers.workspace_files import (
    MAX_WRITE_SIZE,
    _collect_recursive,
    _ensure_within_subtree,
    _validate_relative_path,
)

logger = logging.getLogger(__name__)

router = APIRouter()

BRAIN_SUBTREE = "wiki"
_LOCK_SUFFIX = ".lock"


def _guard_brain_path(workspace, owner_id: str, path: str) -> str:
    """Validate a wiki-relative path and pin it to the ``wiki`` subtree.

    Returns the user-root-relative path (``wiki/{path}``). 400 on malformed
    paths, 403 on anything that resolves outside the subtree (symlink escapes
    included).
    """
    try:
        _validate_relative_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    full_path = f"{BRAIN_SUBTREE}/{path}"
    try:
        _ensure_within_subtree(workspace, owner_id, full_path, BRAIN_SUBTREE)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    return full_path


def _strip_wiki_prefix(entries: list[dict]) -> list[dict]:
    """Rewrite entry paths from user-root-relative to wiki-relative, dropping
    the per-file write-lock sidecars (plumbing, not owner content)."""
    prefix = f"{BRAIN_SUBTREE}/"
    out = []
    for entry in entries:
        if entry["name"].endswith(_LOCK_SUFFIX):
            continue
        p = entry["path"]
        out.append({**entry, "path": p[len(prefix) :] if p.startswith(prefix) else p})
    return out


@router.get("/brain/tree")
async def list_brain_tree(auth: AuthContext = Depends(get_current_user)):
    """Recursive listing of the owner's wiki vault, paths relative to wiki/.

    A missing vault (the plugin hasn't written yet) is an empty tree, never
    an error — _collect_recursive swallows the not-found.
    """
    owner_id = resolve_owner_id(auth)
    workspace = get_workspace()
    entries: list = []
    _collect_recursive(workspace, owner_id, BRAIN_SUBTREE, entries, BRAIN_SUBTREE)
    return {"files": _strip_wiki_prefix(entries)}


@router.get("/brain/file")
async def read_brain_file(
    path: str = Query(..., description="File path relative to the wiki vault"),
    auth: AuthContext = Depends(get_current_user),
):
    """Read a wiki page's content and metadata."""
    owner_id = resolve_owner_id(auth)
    workspace = get_workspace()
    full_path = _guard_brain_path(workspace, owner_id, path)
    try:
        info = workspace.read_file_info(owner_id, full_path)
    except WorkspaceError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=f"File not found: {path!r}")
        if "traversal" in str(exc).lower():
            raise HTTPException(status_code=403, detail="Access denied")
        logger.exception("Failed to read brain file for owner %s", owner_id)
        raise HTTPException(status_code=500, detail="Failed to read brain file")
    return {**info, "path": path}


class BrainWriteRequest(BaseModel):
    path: str
    content: str


def _locked_write(workspace, owner_id: str, full_path: str, content: str) -> dict:
    """Two-layer-locked EFS write (see module docstring); returns fresh stat."""
    resolved = workspace._resolve_user_file(owner_id, full_path)
    lock_path = f"{resolved}{_LOCK_SUFFIX}"
    with get_file_lock(lock_path):
        lock_fd = open_lock_fd(str(resolved.parent), lock_path)
        try:
            fcntl.lockf(lock_fd, fcntl.LOCK_EX)
            # write_file creates parents and chowns to uid 1000 so the
            # container can still read/write the page after the owner's edit.
            workspace.write_file(owner_id, full_path, content)
            stat = resolved.stat()
            return {"size": stat.st_size, "modified_at": stat.st_mtime}
        finally:
            fcntl.lockf(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()


@router.put("/brain/file")
async def write_brain_file(
    body: BrainWriteRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Write a wiki page (creating parents), size-capped and locked."""
    owner_id = resolve_owner_id(auth)
    workspace = get_workspace()
    if len(body.content.encode("utf-8")) > MAX_WRITE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"content exceeds {MAX_WRITE_SIZE // (1024 * 1024)}MB limit",
        )
    full_path = _guard_brain_path(workspace, owner_id, body.path)
    try:
        result = await asyncio.to_thread(_locked_write, workspace, owner_id, full_path, body.content)
    except WorkspaceError as exc:
        if "traversal" in str(exc).lower():
            raise HTTPException(status_code=403, detail="Access denied")
        logger.exception("Failed to write brain file for owner %s", owner_id)
        raise HTTPException(status_code=500, detail="Failed to write brain file")
    logger.info("Wrote brain file %s for owner %s", full_path, owner_id)
    return {"path": body.path, **result}


def _locked_delete(workspace, owner_id: str, full_path: str) -> None:
    """Locked unlink of a wiki page plus its lock sidecar."""
    resolved = workspace._resolve_user_file(owner_id, full_path)
    lock_path = f"{resolved}{_LOCK_SUFFIX}"
    with get_file_lock(lock_path):
        workspace.delete_file(owner_id, full_path)
        resolved.with_name(resolved.name + _LOCK_SUFFIX).unlink(missing_ok=True)


@router.delete("/brain/file", status_code=204)
async def delete_brain_file(
    path: str = Query(..., description="File path relative to the wiki vault"),
    auth: AuthContext = Depends(get_current_user),
):
    """Delete a wiki page (owner removes a wrong note). Refuses directories."""
    owner_id = resolve_owner_id(auth)
    workspace = get_workspace()
    full_path = _guard_brain_path(workspace, owner_id, path)
    try:
        resolved = workspace._resolve_user_file(owner_id, full_path)
        if resolved.is_dir():
            raise HTTPException(status_code=400, detail="path is a directory")
        await asyncio.to_thread(_locked_delete, workspace, owner_id, full_path)
    except WorkspaceError as exc:
        if "traversal" in str(exc).lower():
            raise HTTPException(status_code=403, detail="Access denied")
        logger.exception("Failed to delete brain file for owner %s", owner_id)
        raise HTTPException(status_code=500, detail="Failed to delete brain file")
    return Response(status_code=204)
