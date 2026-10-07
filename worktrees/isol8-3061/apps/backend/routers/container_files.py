"""File uploads into an agent's Hermes profile.

Single endpoint: POST /container/files writes uploaded files into the target agent's
profile under ``home/uploads/``. ``home/`` is the shell's ``$HOME`` for every turn
(``TERMINAL_HOME_MODE: profile``), and ``write_profile_subtree`` bumps the profile's
``.revision`` last, so the bridge re-pulls the new objects on the agent's next turn --
warm microVM included. The returned ``path`` is therefore ``uploads/<name>``, relative
to the agent's own home.

The mobile composer is the live caller: it uploads first, then prepends a notice
naming these paths to the outgoing message (``apps/mobile/src/lib/attachments.ts``).
"""

import logging
import re
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.dynamodb import run_in_thread
from core.repositories import container_repo
from core.services import hermes_profiles
from core.services.credentials.hermes_cli import HOME_DIR

logger = logging.getLogger(__name__)

router = APIRouter()


MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB per file
MAX_FILES_PER_REQUEST = 10
SAFE_FILENAME_RE = re.compile(r"^[\w\-. ]+$")
#: Relative to the profile root; ``home/`` is what the turn's shell sees as ``$HOME``.
UPLOAD_DIR = "uploads"


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
    summary="Upload files into an agent's workspace",
    description=(
        "Uploads one or more files into the agent's profile under `uploads/`, relative to "
        "the agent's home. Max 10MB per file, 10 files per request."
    ),
    operation_id="upload_files",
    responses={
        400: {"description": "File too large or too many files"},
        404: {"description": "No workspace for this user"},
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
    if not await container_repo.get_by_owner_id(owner_id):
        raise HTTPException(status_code=404, detail="No workspace found")

    payload: dict[str, bytes] = {}
    uploaded = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File '{f.filename}' exceeds {MAX_FILE_SIZE // (1024 * 1024)}MB limit.",
            )

        safe_name = _sanitize_filename(f.filename or "upload")
        # The agent's shell home is the profile's `home/`, so the path it is told to read
        # is relative to that -- not to the profile root the object key names.
        payload[f"{HOME_DIR}/{UPLOAD_DIR}/{safe_name}"] = data
        uploaded.append({"filename": safe_name, "path": f"{UPLOAD_DIR}/{safe_name}", "size": len(data)})

    # One revision bump for the whole request: the bridge re-pulls everything that changed.
    await run_in_thread(hermes_profiles.write_profile_subtree, owner_id, agent_id, payload, ())
    logger.info("Uploaded %d file(s) into %s/%s for owner %s", len(payload), agent_id, UPLOAD_DIR, owner_id)

    return {"uploaded": uploaded}
