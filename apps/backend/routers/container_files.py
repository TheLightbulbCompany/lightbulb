"""File uploads into the user's OpenClaw agent workspace.

Single endpoint: POST /files writes uploaded files to the user's EFS workspace
under ``workspace-{agent_id}/uploads/`` so the agent can read them. This is a
plain EFS write — no gateway WebSocket is involved.
"""

import logging
import re
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.containers import get_ecs_manager, get_workspace

logger = logging.getLogger(__name__)

router = APIRouter()


MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB per file
MAX_FILES_PER_REQUEST = 10
SAFE_FILENAME_RE = re.compile(r"^[\w\-. ]+$")


def _sanitize_filename(name: str) -> str:
    """Return a safe filename, stripping path components and invalid chars."""
    # Take only the basename (no directory traversal)
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if not base or not SAFE_FILENAME_RE.match(base):
        # Replace unsafe chars
        base = re.sub(r"[^\w\-. ]", "_", base) or "upload"
    return base


@router.post(
    "/files",
    summary="Upload files to the user's agent workspace",
    description=(
        "Uploads one or more files to the user's workspace on EFS. "
        "Files are placed in the agent's `workspace-{agent_id}/uploads/` directory "
        "and are accessible to the user's OpenClaw agent. "
        "Max 10MB per file, 10 files per request."
    ),
    operation_id="upload_files",
    responses={
        400: {"description": "File too large or too many files"},
        404: {"description": "No container for this user"},
    },
)
async def upload_files(
    files: List[UploadFile] = File(..., description="Files to upload"),
    agent_id: str = Query(..., description="Target agent ID"),
    auth: AuthContext = Depends(get_current_user),
):
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Maximum {MAX_FILES_PER_REQUEST} per request.",
        )

    if "/" in agent_id or "\\" in agent_id or ".." in agent_id:
        raise HTTPException(status_code=400, detail="Invalid agent_id")

    owner_id = resolve_owner_id(auth)

    # Verify user has a container
    ecs_manager = get_ecs_manager()
    container = await ecs_manager.get_service_status(owner_id)
    if not container:
        raise HTTPException(status_code=404, detail="No container found")

    workspace = get_workspace()
    workspace.ensure_user_dir(owner_id)

    uploaded = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File '{f.filename}' exceeds {MAX_FILE_SIZE // (1024 * 1024)}MB limit.",
            )

        safe_name = _sanitize_filename(f.filename or "upload")
        dest_path = f"workspace-{agent_id}/uploads/{safe_name}"
        workspace.write_bytes(owner_id, dest_path, data)
        # The agent's working dir is $HOME (/home/node) but EFS is mounted
        # at $HOME/.openclaw, so the agent sees uploads relative to that mount.
        agent_path = f".openclaw/{dest_path}"
        uploaded.append({"filename": safe_name, "path": agent_path, "size": len(data)})
        logger.info("Uploaded %s (%d bytes) for user %s", dest_path, len(data), owner_id)

    return {"uploaded": uploaded}
