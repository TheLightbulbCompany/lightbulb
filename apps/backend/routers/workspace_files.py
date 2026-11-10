"""REST endpoints for browsing agent workspace files.

An agent's files are its Hermes profile in S3 (``hermes_profiles.profile_prefix``).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.dynamodb import run_in_thread
from core.services import hermes_profiles

logger = logging.getLogger(__name__)

router = APIRouter()


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
    "AGENTS.md",
}

MAX_WRITE_SIZE = 10 * 1024 * 1024  # 10 MB


# ---------------------------------------------------------------------------
# AgentCore rows: the same five routes, over the agent's S3 Hermes profile.
# ---------------------------------------------------------------------------

#: The config-tab filenames that HAVE a Hermes home, and where it is inside the profile.
#: ``SOUL.md`` is the agent's identity document (Hermes reads ``$HERMES_HOME/SOUL.md``);
#: ``USER.md`` is Hermes's own user-profile memory store (``tools/memory_tool.py``), plain text
#: read and written verbatim; ``AGENTS.md`` sits at the profile root as it does on EFS. The
#: config tab is also the ONLY door to these three -- the Files panel hides every top-level
#: CONFIG_ALLOWLIST name from its workspace tree on the grounds that the config tab has them.
#: ``MEMORY.md`` is Hermes's own agent-notes store (``tools/memory_tool.py`` ``_path_for``:
#: ``memories/MEMORY.md``), written by the agent and shown in its prompt in every room, so people
#: can read and clear it here (memory-scopes D7). TOOLS.md has no Hermes reader at all, so it is
#: refused.
_AGENTCORE_CONFIG_PATHS = {
    "SOUL.md": "SOUL.md",
    "USER.md": "memories/USER.md",
    "MEMORY.md": "memories/MEMORY.md",
    "AGENTS.md": "AGENTS.md",
}

#: IDENTITY.md has no Hermes equivalent at all -- an agent has ONE identity document and it is
#: SOUL.md. GET serves this read-only note so the tab explains itself; PUT is refused.
_IDENTITY_STUB = (
    "This workspace runs on Hermes, where an agent has one identity document: SOUL.md.\n"
    "\n"
    "IDENTITY.md is not read here and cannot be saved. Edit SOUL.md instead.\n"
)


def _unsupported(message: str) -> HTTPException:
    """422 with a code the client can branch on and copy it can show as-is (the shared API client
    promotes ``detail.message`` into the toast)."""
    return HTTPException(status_code=422, detail={"code": "unsupported_on_agentcore", "message": message})


def _require_agent_id(agent_id: str) -> None:
    if "/" in agent_id or "\\" in agent_id or ".." in agent_id:
        raise HTTPException(status_code=400, detail="Invalid agent_id")


async def _agentcore_tree(owner_id: str, agent_id: str, path: str) -> list[dict]:
    try:
        return await run_in_thread(hermes_profiles.list_profile_dir, owner_id, agent_id, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def _agentcore_read(owner_id: str, agent_id: str, path: str) -> dict:
    try:
        return await run_in_thread(hermes_profiles.read_profile_file, owner_id, agent_id, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {path!r}")


async def _agentcore_config_files(owner_id: str, agent_id: str) -> list[dict]:
    """``config-files`` over the profile: the mapped names that actually exist, each reported
    under the filename the tab asks for rather than its in-profile path."""
    entries: dict[str, dict] = {}
    for sub in ("", "memories"):
        for entry in await run_in_thread(hermes_profiles.list_profile_dir, owner_id, agent_id, sub):
            entries[entry["path"]] = entry
    out = []
    for name, key in sorted(_AGENTCORE_CONFIG_PATHS.items()):
        entry = entries.get(key)
        if entry and entry["type"] == "file":
            out.append({**entry, "name": name, "path": name})
    return out


async def _agentcore_read_config(owner_id: str, agent_id: str, path: str) -> dict:
    if path == "IDENTITY.md":
        return {
            "name": path,
            "path": path,
            "size": len(_IDENTITY_STUB),
            "modified_at": 0,
            "mime_type": "text/markdown",
            "binary": False,
            "content": _IDENTITY_STUB,
        }
    key = _AGENTCORE_CONFIG_PATHS.get(path)
    if key is None:
        raise HTTPException(status_code=404, detail=f"File not found: {path!r}")
    info = await _agentcore_read(owner_id, agent_id, key)
    return {**info, "name": path, "path": path}


async def _agentcore_write(owner_id: str, agent_id: str, path: str, content: str, tab: str) -> str:
    if len(content.encode("utf-8")) > MAX_WRITE_SIZE:
        raise HTTPException(status_code=400, detail=f"content exceeds {MAX_WRITE_SIZE // (1024 * 1024)}MB limit")
    target = path
    if tab == "config":
        if path not in CONFIG_ALLOWLIST:
            raise HTTPException(status_code=400, detail=f"File not in allowlist: {path}")
        if path == "SOUL.md":
            # Saving here makes the file the OWNER's: strip the generated marker so the rename
            # branch (hermes_provisioning.refresh_agent_soul) never overwrites their text again.
            soul = hermes_profiles.strip_generated_marker(content)
            await run_in_thread(hermes_profiles.upload_soul_md, owner_id, agent_id, soul)
            return path
        mapped = _AGENTCORE_CONFIG_PATHS.get(path)
        if mapped is None:
            raise _unsupported(
                "Identity lives in SOUL.md on this workspace — IDENTITY.md is not read here."
                if path == "IDENTITY.md"
                else f"{path} has no equivalent on this workspace."
            )
        target = mapped
    try:
        return await hermes_profiles.write_profile_file(owner_id, agent_id, target, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        # A hidden path (.env, .revision, mcp-tokens/, the facts file) answers exactly as the read
        # route does -- the write route must not confirm what the read route hides.
        raise HTTPException(status_code=404, detail=f"File not found: {target!r}")
    except PermissionError:
        raise _unsupported(f"{target} is managed by Lightbulb and cannot be edited here.")


@router.get("/workspace/{agent_id}/tree")
async def list_workspace_tree(
    agent_id: str,
    path: str = Query("", description="Subdirectory path relative to agent workspace"),
    auth: AuthContext = Depends(get_current_user),
):
    """List ONE directory of an agent's workspace; the client expands level by level.

    There is deliberately no recursive mode: a full walk is one S3 LIST per level.
    """
    owner_id = resolve_owner_id(auth)
    _require_agent_id(agent_id)
    return {"files": await _agentcore_tree(owner_id, agent_id, path)}


@router.get("/workspace/{agent_id}/file")
async def read_workspace_file(
    agent_id: str,
    path: str = Query(..., description="File path relative to agent workspace"),
    auth: AuthContext = Depends(get_current_user),
):
    """Read a file's content and metadata from an agent's workspace."""
    owner_id = resolve_owner_id(auth)
    _require_agent_id(agent_id)
    return await _agentcore_read(owner_id, agent_id, path)


@router.get("/workspace/{agent_id}/config-files")
async def list_config_files(
    agent_id: str,
    auth: AuthContext = Depends(get_current_user),
):
    """List allowlisted agent config files (SOUL.md, MEMORY.md, etc.)."""
    owner_id = resolve_owner_id(auth)
    _require_agent_id(agent_id)
    return {"files": await _agentcore_config_files(owner_id, agent_id)}


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
    _require_agent_id(agent_id)
    return await _agentcore_read_config(owner_id, agent_id, path)


class WriteFileRequest(BaseModel):
    path: str
    content: str
    tab: str  # "workspace" or "config"


@router.put("/workspace/{agent_id}/file")
async def write_workspace_file(
    agent_id: str,
    body: WriteFileRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """Write a file to the agent's workspace or config directory."""
    owner_id = resolve_owner_id(auth)
    _require_agent_id(agent_id)
    if body.tab not in ("workspace", "config"):
        raise HTTPException(status_code=400, detail="tab must be 'workspace' or 'config'")

    written = await _agentcore_write(owner_id, agent_id, body.path, body.content, body.tab)
    logger.info("Wrote %s into the Hermes profile for owner %s (tab=%s)", written, owner_id, body.tab)
    return {"status": "ok", "path": written}
