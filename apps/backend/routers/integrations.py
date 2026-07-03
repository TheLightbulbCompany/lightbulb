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

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.containers.config import read_openclaw_config_from_efs
from core.services.config_patcher import (
    ConfigPatchError,
    delete_openclaw_config_path,
    set_openclaw_config_path,
)
from core.services.credentials.mcp_server import reconcile_integrations

logger = logging.getLogger(__name__)

router = APIRouter()


class ServerEntry(BaseModel):
    """One mcp.servers entry — stdio (command) or remote (transport+url).

    Mirrors the registry's McpServer invariant: exactly one of the two shapes.
    Timeouts are optional here; absent values get container-appropriate defaults
    injected at write time (see _normalize_entry)."""

    # min_length=1: an empty-string command/transport/url is never a valid value,
    # and allowing one would let a mixed shape slip past the presence checks below
    # (e.g. command:"" alongside transport+url) and into openclaw.json.
    command: str | None = Field(None, min_length=1, description="Command to run a stdio MCP server")
    args: list[str] = Field(default_factory=list, description="Command arguments")
    env: Dict[str, str] = Field(default_factory=dict, description="Environment variables")
    transport: str | None = Field(None, min_length=1, description="Remote transport, e.g. streamable-http")
    url: str | None = Field(None, min_length=1, description="Remote server URL")
    headers: Dict[str, str | int | bool] | None = Field(
        None, description="HTTP headers for a remote server (e.g. API-key auth)"
    )
    connectionTimeoutMs: int | None = Field(None, gt=0, description="MCP connect timeout override")
    requestTimeoutMs: int | None = Field(None, gt=0, description="Per-request timeout override")

    @model_validator(mode="after")
    def _stdio_or_remote(self) -> "ServerEntry":
        is_stdio = self.command is not None
        is_remote = self.transport is not None and self.url is not None
        if is_stdio == is_remote:
            raise ValueError("server must be either stdio (command) or remote (transport+url), not neither/both")
        # A stdio entry carrying a stray remote field (or vice versa) would write
        # a mixed shape into openclaw.json that fails the hot reload — reject the
        # partial overlap too, not just the complete one.
        if is_stdio and (self.transport is not None or self.url is not None or self.headers is not None):
            raise ValueError("a stdio (command) server must not set transport, url, or headers")
        if is_remote and (self.args or self.env):
            raise ValueError("a remote (transport+url) server must not set args or env")
        return self


# A stdio server spawned via npx takes 13s+ to start even with a warm cache on a
# 0.5-vCPU container, and the npx cache lives on ephemeral /tmp — so every task
# restart is a cold start that reliably exceeds OpenClaw's default 30s connect
# timeout. Sessions are reused once connected, so the budget is only spent on
# genuine cold starts.
_STDIO_CONNECT_TIMEOUT_MS = 120_000
# OpenClaw caps catalog tools/list at a hardcoded 1.5s unless the server block
# carries an explicit request timeout; a remote server's list can exceed that
# under CPU contention, silently excluding it from the catalog. 60s matches the
# MCP SDK's per-request default, so runtime tool-call behavior is unchanged.
_REMOTE_REQUEST_TIMEOUT_MS = 60_000


def _normalize_entry(model: "ServerEntry") -> dict:
    """The dict actually written to openclaw.json for a validated entry.

    ``exclude_none`` keeps the unset shape's keys (transport/url on a stdio
    entry, command on a remote one) out of the stored block — a literal null
    there fails OpenClaw's config schema on hot reload, which bricks the
    container. Remote entries also drop the empty args/env artifacts of the
    stdio-oriented defaults, matching the registry's canonical remote shape.

    Timeout defaults are injected only when the user didn't set one — an
    explicit user-provided value always wins."""
    entry = model.model_dump(exclude_none=True)
    if entry.get("url"):
        if not entry.get("args"):
            entry.pop("args", None)
        if not entry.get("env"):
            entry.pop("env", None)
        if not entry.get("requestTimeoutMs"):
            entry["requestTimeoutMs"] = _REMOTE_REQUEST_TIMEOUT_MS
    if entry.get("command") and not entry.get("connectionTimeoutMs"):
        entry["connectionTimeoutMs"] = _STDIO_CONNECT_TIMEOUT_MS
    return entry


class ServersResponse(BaseModel):
    servers: Dict[str, Any]


class PutServersRequest(BaseModel):
    servers: Dict[str, Any] = Field(..., description="Complete servers dict to replace")


class PatchServerRequest(ServerEntry):
    pass


def _validate_servers(servers: dict) -> dict:
    """Validate each server entry and return the normalized dict to store.

    Strict per-entry validation matters now that writes go straight into
    ``openclaw.json``: malformed shapes (e.g. ``args: "--stdio"``,
    ``env: {"K": 123}``) get past Pydantic's ``Dict[str, Any]`` body
    validation but then fail OpenClaw's chokidar hot-reload, leaving the
    API at 200 with the agent state silently broken. Reject at the API.

    The RETURNED entries — not the raw client input — must be what gets
    written: normalization strips explicit nulls on the unset shape's keys
    (which would equally brick the hot reload) and injects timeout defaults.
    """
    normalized: dict[str, dict] = {}
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=422,
                detail=f"Server '{name}' must be an object",
            )
        try:
            model = ServerEntry.model_validate(entry)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Server '{name}' is invalid: {exc.errors()}",
            ) from exc
        normalized[name] = _normalize_entry(model)
    return normalized


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
    servers = _validate_servers(body.servers)
    try:
        await set_openclaw_config_path(owner_id, ["mcp", "servers"], servers)
    except ConfigPatchError as exc:
        raise HTTPException(status_code=503, detail=f"container_not_ready: {exc}") from exc
    # The whole-dict replace drops any integration-owned key the user's body
    # omits (e.g. a connected integration's `stripe` server). Re-assert those
    # keys so a manual edit can't clobber a live integration. Best-effort: the
    # user's write already committed, so a Secrets-Manager/reconcile hiccup must
    # not 500 a successful write — the next reconcile/reprovision self-heals.
    try:
        await reconcile_integrations(owner_id)
    except Exception:
        logger.exception("integration reconcile after manual MCP write failed for %s", owner_id)
    # Re-read AFTER reconcile so the response reflects the final on-disk state.
    # Returning body.servers would omit integration-owned keys reconcile just
    # re-asserted (e.g. `stripe`) and misrepresent what's in openclaw.json.
    servers = await _read_mcp_servers(owner_id)
    return {"servers": servers}


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
    entry = _normalize_entry(body)
    try:
        await set_openclaw_config_path(owner_id, ["mcp", "servers", name], entry)
    except ConfigPatchError as exc:
        raise HTTPException(status_code=503, detail=f"container_not_ready: {exc}") from exc
    # An upsert whose name collides with an integration-owned key (e.g. `stripe`)
    # would overwrite it with the user's value. Re-assert integration-owned keys
    # so integration ownership always wins. Best-effort for the same reason as
    # the PUT path — read AFTER reconcile so the response reflects the final state.
    try:
        await reconcile_integrations(owner_id)
    except Exception:
        logger.exception("integration reconcile after manual MCP write failed for %s", owner_id)
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
    # Deleting an integration-owned key (e.g. `stripe`) via the manual panel must
    # not silently break a still-connected integration: re-assert integration-owned
    # keys so ownership wins. A user-owned (non-provider) key stays deleted, since
    # reconcile only touches connected integration providers. Best-effort; read after.
    try:
        await reconcile_integrations(owner_id)
    except Exception:
        logger.exception("integration reconcile after manual MCP delete failed for %s", owner_id)
    servers = await _read_mcp_servers(owner_id)
    return {"servers": servers}
