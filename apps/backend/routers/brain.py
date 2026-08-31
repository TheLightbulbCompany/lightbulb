"""Owner routes over the memory-wiki vault at {user_root}/wiki/ on EFS.

The vault is written by the container-side memory-wiki plugin (usually under
wiki/main/). These routes give the owner a browse/edit/delete surface over it,
pinned to the ``wiki`` subtree with the same guard machinery
``routers/workspace_files.py`` uses for agent workspaces.

Lock-vs-plugin note: writes take the repo's two-layer lock (per-file
in-process ``threading.Lock`` + ``fcntl.lockf`` on a dot-prefixed
``.{name}.lb-lock`` sidecar — see ``core/services/efs_file_lock.py``), which
serializes BACKEND writers, and land via tempfile + ``os.replace`` so readers
(the container's memory-wiki plugin included) never see a torn page. The
plugin's wiki_apply does NOT take our sidecar, so a same-second owner-vs-agent
collision is last-writer-wins on the inode swap — accepted for v1. The wiki
subtree pin is checked pre-write; the only actor that could swap a symlink in
after the guard is the container itself, which already owns the entire user
root — no trust boundary is crossed.
"""

import asyncio
import fcntl
import logging
import os
import re
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.containers import get_workspace
from core.containers.workspace import WorkspaceError
from core.services.efs_file_lock import get_file_lock, open_lock_fd
from routers.container_files import MAX_FILE_SIZE, MAX_FILES_PER_REQUEST, _sanitize_filename
from routers.workspace_files import (
    MAX_WRITE_SIZE,
    _collect_recursive,
    _ensure_within_subtree,
    _validate_relative_path,
)

logger = logging.getLogger(__name__)

router = APIRouter()

BRAIN_SUBTREE = "wiki"
# Dot-prefixed + distinctive so it can never collide with agent-written vault
# content (a plain `{file}.lock` did — an agent's report.md.lock is real data).
_LOCK_SUFFIX = ".lb-lock"


def _lock_sidecar_path(resolved: Path) -> Path:
    """The write-lock sidecar beside a vault file: ``.{name}.lb-lock``."""
    return resolved.parent / f".{resolved.name}{_LOCK_SUFFIX}"


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
        # Hide ONLY our own sidecars: files (never dirs, whose children would
        # orphan) named exactly `.{something}.lb-lock`.
        if entry["type"] == "file" and entry["name"].startswith(".") and entry["name"].endswith(_LOCK_SUFFIX):
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


def _write_bytes_atomic(workspace, owner_id: str, resolved: Path, data: bytes) -> dict:
    """Tempfile + os.replace + chown at an already-resolved path; returns fresh stat.

    No locking of its own — the caller is responsible for serializing access
    to ``resolved`` (the per-file page-edit lock in ``_locked_write``, or the
    inbox-wide upload lock in ``_locked_inbox_upload``). The container's
    memory-wiki plugin reads these pages, so the swap must be atomic — never
    an in-place write. The tmp name is dot-prefixed so it can never collide
    with genuine agent-written vault content (same reasoning as the
    ``.lb-lock`` sidecar); a crash-orphaned one stays visible in the tree by
    design — the owner can see and delete it.
    """
    tmp = resolved.with_name(f".{resolved.name}.tmp")
    try:
        tmp.write_bytes(data)
        # chown to uid 1000 (parents included) so the container can still
        # read/write the page after the owner's edit.
        workspace._chown_for_access_point(tmp, owner_id)
        os.replace(tmp, resolved)
    except OSError as exc:
        raise WorkspaceError(
            f"Failed to write {resolved!r} for {owner_id}: {exc}",
            user_id=owner_id,
        ) from exc
    finally:
        tmp.unlink(missing_ok=True)
    stat = resolved.stat()
    return {"size": stat.st_size, "modified_at": stat.st_mtime}


def _locked_write(workspace, owner_id: str, full_path: str, content: str | bytes) -> dict:
    """Two-layer-locked atomic EFS write (see module docstring); returns fresh stat.

    Accepts ``str`` (page edits) or ``bytes`` (uploads) — same lock/tempfile/
    chown discipline either way.
    """
    resolved = workspace._resolve_user_file(owner_id, full_path)
    lock_path = str(_lock_sidecar_path(resolved))
    with get_file_lock(lock_path):
        # open_lock_fd creates the parent dirs alongside the sidecar.
        lock_fd = open_lock_fd(str(resolved.parent), lock_path)
        try:
            fcntl.lockf(lock_fd, fcntl.LOCK_EX)
            data = content.encode("utf-8") if isinstance(content, str) else content
            return _write_bytes_atomic(workspace, owner_id, resolved, data)
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
    except FileExistsError as exc:
        # F7: a parent-chain component is a regular file (e.g. PUT
        # "main/index.md/child.md" when "main/index.md" already exists) —
        # open_lock_fd's os.makedirs surfaces this as a raw OSError. A bad
        # request, not a 500.
        raise HTTPException(status_code=400, detail=f"Invalid path: {exc}")
    except WorkspaceError as exc:
        if "traversal" in str(exc).lower():
            raise HTTPException(status_code=403, detail="Access denied")
        if isinstance(exc.__cause__, IsADirectoryError):
            # F7: the write target names an existing directory — os.replace
            # refuses (IsADirectoryError, wrapped by _write_bytes_atomic). A
            # bad request, not a 500.
            raise HTTPException(status_code=400, detail=f"Path is a directory: {body.path!r}")
        logger.exception("Failed to write brain file for owner %s", owner_id)
        raise HTTPException(status_code=500, detail="Failed to write brain file")
    logger.info("Wrote brain file %s for owner %s", full_path, owner_id)
    return {"path": body.path, **result}


def _dedupe_filename(dir_path: Path, name: str) -> str:
    """If ``name`` already exists in ``dir_path``, append ``-1``, ``-2``, ... before the extension."""
    if not (dir_path / name).exists():
        return name
    stem, dot, ext = name.rpartition(".")
    base, suffix = (stem, f".{ext}") if dot else (name, "")
    counter = 1
    candidate = f"{base}-{counter}{suffix}"
    while (dir_path / candidate).exists():
        counter += 1
        candidate = f"{base}-{counter}{suffix}"
    return candidate


MAX_UPLOAD_FILENAME_BYTES = 200
_FILENAME_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
# The inbox-wide upload lock sidecar — deliberately ONE lock for the whole
# inbox, not per-filename (see _locked_inbox_upload).
_INBOX_LOCK_NAME = ".inbox-upload.lb-lock"


def _validate_upload_filename(name: str) -> None:
    """Reject a sanitized filename BEFORE any filesystem work touches it.

    ``_sanitize_filename`` still lets three shapes through that break the
    drop-zone (T3 review round 1):
    - F1: an empty or all-dot name (``"."``/``".."``) — ``"."`` joined onto
      ``inbox/`` collapses to the inbox directory itself under pathlib's own
      join rules, so the write lands ON the directory and bricks every later
      upload once ``os.replace`` turns it into a regular file.
    - F2: a name long enough to blow ``NAME_MAX`` inside the lock sidecar
      path, which raises a bare ``OSError``/``ENAMETOOLONG`` — an unhandled
      500 instead of a clean 400.
    - F4: a trailing control character (``\\n`` included) — ``$`` in
      ``SAFE_FILENAME_RE`` matches just before a trailing newline, so it
      survives sanitization and lands in the filename and the access log.
    """
    if not name or set(name) <= {"."}:
        raise HTTPException(status_code=400, detail=f"Invalid filename: {name!r}")
    if _FILENAME_CONTROL_CHAR_RE.search(name):
        raise HTTPException(status_code=400, detail="Filename contains control characters")
    if len(name.encode("utf-8")) > MAX_UPLOAD_FILENAME_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Filename exceeds {MAX_UPLOAD_FILENAME_BYTES} bytes",
        )
    if name.endswith(_LOCK_SUFFIX):
        # F8: an upload named "*.lb-lock" would land in inbox/ and 200, but
        # the tree filter (_strip_wiki_prefix) hides any `.{name}.lb-lock`
        # file — the owner would never see it landed.
        raise HTTPException(status_code=400, detail=f"Filename may not end with {_LOCK_SUFFIX!r}")


def _locked_inbox_upload(workspace, owner_id: str, safe_name: str, data: bytes) -> dict:
    """Dedupe-probe + atomic write for one uploaded file under a SINGLE
    inbox-wide lock (T3 review M3 — controller ruling).

    The previous shape ran the dedupe stat OUTSIDE any lock, so two in-flight
    uploads of "notes.md" could both observe no collision and both resolve to
    "notes.md" — the loser's bytes silently vanish on the second
    ``os.replace``. Serializing probe+write under one lock per owner (not the
    per-file page-edit lock, which only fences a name already decided) closes
    that window: whichever upload gets the lock first commits its name before
    the next one even runs its probe.

    ponytail: one lock guards the WHOLE inbox, so two uploads with unrelated
    filenames also serialize. Uploads are rare — move to per-name locks if
    concurrent-upload throughput ever becomes a real bottleneck.
    """
    # F3: pin the tenancy boundary BEFORE any filesystem mutation. A
    # compromised container could plant `wiki` as a symlink escaping the user
    # root — _ensure_within_subtree alone doesn't catch that (it resolves the
    # subtree root through the same symlink), so without this the mkdir calls
    # below would create a lock file / inbox dir inside another owner's tree
    # before the eventual content-write's _resolve_user_file call refuses it.
    workspace._resolve_user_file(owner_id, BRAIN_SUBTREE)
    wiki_dir = workspace.user_path(owner_id) / BRAIN_SUBTREE
    inbox_dir = wiki_dir / "inbox"
    lock_path = str(wiki_dir / _INBOX_LOCK_NAME)
    with get_file_lock(lock_path):
        # open_lock_fd creates wiki_dir (holding the sidecar) if missing.
        lock_fd = open_lock_fd(str(wiki_dir), lock_path)
        try:
            fcntl.lockf(lock_fd, fcntl.LOCK_EX)
            inbox_dir.mkdir(parents=True, exist_ok=True)
            name = _dedupe_filename(inbox_dir, safe_name)
            full_path = _guard_brain_path(workspace, owner_id, f"inbox/{name}")
            resolved = workspace._resolve_user_file(owner_id, full_path)
            resolved_inbox = inbox_dir.resolve(strict=False)
            # Belt-and-suspenders on top of _validate_upload_filename: the
            # write target must be a plain file strictly inside inbox/, never
            # the directory itself.
            if resolved.parent != resolved_inbox or resolved == resolved_inbox:
                raise HTTPException(status_code=400, detail=f"Invalid filename: {safe_name!r}")
            stat_result = _write_bytes_atomic(workspace, owner_id, resolved, data)
            return {"name": name, **stat_result}
        finally:
            fcntl.lockf(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()


@router.post("/brain/upload")
async def upload_brain_files(
    files: List[UploadFile] = File(..., description="Files to drop into the vault inbox"),
    auth: AuthContext = Depends(get_current_user),
):
    """Owner drop-zone: land files under wiki/inbox/ so agents notice new material.

    Same caps/shape as POST /container/files (routers/container_files.py):
    10 files per request, 10MB each. A same-named collision gets a `-1`
    (`-2`, ...) suffix rather than overwriting — dropping the same filename
    twice never loses the first copy (serialized under one inbox-wide lock,
    see _locked_inbox_upload).
    """
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Maximum {MAX_FILES_PER_REQUEST} per request.",
        )

    owner_id = resolve_owner_id(auth)
    workspace = get_workspace()

    uploaded = []
    for f in files:
        # Bounded read: a crafted part with no declared size never inflates
        # past the cap we're about to enforce anyway (T3 review F5).
        data = await f.read(MAX_FILE_SIZE + 1)
        if len(data) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File '{f.filename}' exceeds {MAX_FILE_SIZE // (1024 * 1024)}MB limit.",
            )
        safe_name = _sanitize_filename(f.filename or "upload")
        _validate_upload_filename(safe_name)
        try:
            result = await asyncio.to_thread(_locked_inbox_upload, workspace, owner_id, safe_name, data)
        except WorkspaceError:
            logger.exception("Failed to upload brain file for owner %s", owner_id)
            raise HTTPException(status_code=500, detail="Failed to upload file")
        uploaded.append({"filename": result["name"], "path": f"inbox/{result['name']}", "size": result["size"]})
        logger.info("Uploaded inbox/%s (%d bytes) for owner %s", result["name"], len(data), owner_id)

    return {"uploaded": uploaded}


def _locked_delete(workspace, owner_id: str, full_path: str) -> None:
    """Locked unlink of a wiki page plus its own lock sidecar (never vault content).

    Takes the same two-layer lock as ``_locked_write`` (F4): the in-process
    lock alone only fences threads in this backend process, not the second
    instance transiently running during a rolling deploy — the fcntl sidecar
    is what the module docstring means by "serializes BACKEND writers", and a
    delete that skips it can unlink a sidecar the other instance still holds.
    """
    resolved = workspace._resolve_user_file(owner_id, full_path)
    sidecar = _lock_sidecar_path(resolved)
    lock_path = str(sidecar)
    with get_file_lock(lock_path):
        # open_lock_fd creates the parent dirs alongside the sidecar — a
        # no-op in practice here since a file's parent must already exist for
        # the file itself to exist.
        lock_fd = open_lock_fd(str(resolved.parent), lock_path)
        try:
            fcntl.lockf(lock_fd, fcntl.LOCK_EX)
            workspace.delete_file(owner_id, full_path)
            sidecar.unlink(missing_ok=True)
        finally:
            fcntl.lockf(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()


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
