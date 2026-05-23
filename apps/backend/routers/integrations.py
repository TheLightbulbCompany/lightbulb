"""MCP server integration management endpoints.

CRUD for OpenClaw-native MCP server definitions stored under ``mcp.servers``
in the owner's ``openclaw.json``. Equivalent to ``openclaw mcp list/show/set/unset``
inside the container — but operated from the Isol8 control panel via the
file-locked patcher in ``core.services.config_patcher``.

Reads use ``read_openclaw_config_from_efs``. Writes use the same atomic
file-lock + chown machinery as channels, updates, catalog, admin — every
patch triggers OpenClaw's chokidar-based hot reload, so changes take effect
without a container restart.
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.containers.config import read_openclaw_config_from_efs
from core.services.config_patcher import (
    ConfigPatchError,
    delete_openclaw_config_path,
    set_openclaw_config_path,
)

router = APIRouter()


class ServerEntry(BaseModel):
    command: str = Field(..., description="Command to run the MCP server")
    args: list[str] = Field(default_factory=list, description="Command arguments")
    env: Dict[str, str] = Field(default_factory=dict, description="Environment variables")


class ServersResponse(BaseModel):
    servers: Dict[str, Any]


class PutServersRequest(BaseModel):
    servers: Dict[str, Any] = Field(..., description="Complete servers dict to replace")


class PatchServerRequest(BaseModel):
    command: str = Field(..., description="Command to run the MCP server")
    args: list[str] = Field(default_factory=list, description="Command arguments")
    env: Dict[str, str] = Field(default_factory=dict, description="Environment variables")


def _validate_servers(servers: dict) -> None:
    """Validate each server entry against the ``ServerEntry`` schema.

    Strict per-entry validation matters now that writes go straight into
    ``openclaw.json``: malformed shapes (e.g. ``args: "--stdio"``,
    ``env: {"K": 123}``) get past Pydantic's ``Dict[str, Any]`` body
    validation but then fail OpenClaw's chokidar hot-reload, leaving the
    API at 200 with the agent state silently broken. Reject at the API.
    """
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=422,
                detail=f"Server '{name}' must be an object",
            )
        try:
            ServerEntry.model_validate(entry)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Server '{name}' is invalid: {exc.errors()}",
            ) from exc


async def _read_mcp_servers(owner_id: str) -> dict:
    """Return the current ``mcp.servers`` block, or empty dict if absent."""
    config = await read_openclaw_config_from_efs(owner_id)
    if not isinstance(config, dict):
        return {}
    mcp = config.get("mcp")
    if not isinstance(mcp, dict):
        return {}
    servers = mcp.get("servers")
    return servers if isinstance(servers, dict) else {}


@router.get(
    "/integrations/mcp/servers",
    summary="List configured MCP servers",
    description=(
        "Returns all MCP server entries from the owner's openclaw.json "
        "mcp.servers block. Org callers must be org_admin — the entries "
        "carry per-server `env` secrets that non-admin members shouldn't "
        "see (mirrors the existing org-admin gate on the write endpoints "
        "and the GET /config gate)."
    ),
    response_model=ServersResponse,
    operation_id="list_mcp_servers",
    tags=["integrations"],
)
async def list_mcp_servers(
    auth: AuthContext = Depends(get_current_user),
):
    # Mirror the WRITE-endpoint gate. Without this, any org member could
    # read shared MCP server configs (including `env` secrets) even though
    # they can't create/update/delete them.
    if auth.is_org_context:
        require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    servers = await _read_mcp_servers(owner_id)
    return {"servers": servers}


@router.put(
    "/integrations/mcp/servers",
    summary="Replace all MCP server configurations",
    description="Replaces the entire mcp.servers dict in the owner's openclaw.json.",
    response_model=ServersResponse,
    operation_id="replace_mcp_servers",
    tags=["integrations"],
)
async def replace_mcp_servers(
    body: PutServersRequest,
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    if auth.is_org_context:
        require_org_admin(auth)
    _validate_servers(body.servers)
    try:
        await set_openclaw_config_path(owner_id, ["mcp", "servers"], body.servers)
    except ConfigPatchError as exc:
        raise HTTPException(status_code=503, detail=f"container_not_ready: {exc}") from exc
    return {"servers": body.servers}


@router.patch(
    "/integrations/mcp/servers/{name}",
    summary="Add or update a single MCP server",
    description="Adds or replaces a single MCP server entry by name (full overwrite, not deep-merged).",
    response_model=ServersResponse,
    operation_id="upsert_mcp_server",
    tags=["integrations"],
)
async def upsert_mcp_server(
    name: str,
    body: PatchServerRequest,
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    if auth.is_org_context:
        require_org_admin(auth)
    entry = body.model_dump()
    try:
        await set_openclaw_config_path(owner_id, ["mcp", "servers", name], entry)
    except ConfigPatchError as exc:
        raise HTTPException(status_code=503, detail=f"container_not_ready: {exc}") from exc
    servers = await _read_mcp_servers(owner_id)
    return {"servers": servers}


@router.delete(
    "/integrations/mcp/servers/{name}",
    summary="Remove an MCP server",
    description="Removes an MCP server entry by name from the owner's openclaw.json mcp.servers.",
    response_model=ServersResponse,
    operation_id="delete_mcp_server",
    tags=["integrations"],
)
async def delete_mcp_server(
    name: str,
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    if auth.is_org_context:
        require_org_admin(auth)
    try:
        await delete_openclaw_config_path(owner_id, ["mcp", "servers", name])
    except ConfigPatchError as exc:
        raise HTTPException(status_code=503, detail=f"container_not_ready: {exc}") from exc
    servers = await _read_mcp_servers(owner_id)
    return {"servers": servers}
