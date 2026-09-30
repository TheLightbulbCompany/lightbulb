"""MCP server integration management endpoints.

CRUD over the owner's MANUAL MCP servers, stored on the reserved
``integrations_repo.MANUAL_MCP_KEY`` row. Every write fans the
(manual ∪ connected-integration) merge out to every Hermes profile, and the
response body is that merge — see ``_agentcore_refresh``.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.repositories import integrations_repo
from core.services.credentials.mcp_server import (
    build_connected_integration_servers,
    integration_owned_server_keys,
    reconcile_integrations,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class ServerEntry(BaseModel):
    """One mcp.servers entry — stdio (command) or remote (transport+url).

    Mirrors the registry's McpServer invariant: exactly one of the two shapes.
    Timeouts are optional here; absent values get container-appropriate defaults
    injected at write time (see _normalize_entry). Both timeout fields are
    deliberately shape-agnostic: OpenClaw applies a connect timeout and a
    per-request timeout to either transport.

    extra="allow": OpenClaw's per-server schema is a catchall with many
    legitimate keys this model doesn't enumerate (toolFilter,
    supportsParallelToolCalls, oauth, ...). Forbidding extras would 422
    power users; ignoring them would silently drop what they configured.
    Unknown keys pass through to the stored block instead — OpenClaw
    tolerates keys it doesn't recognize."""

    model_config = {"extra": "allow"}

    # min_length=1: an empty-string command/transport/url is never a valid value,
    # and allowing one would let a mixed shape slip past the presence checks below
    # (e.g. command:"" alongside transport+url) and into openclaw.json.
    command: str | None = Field(None, min_length=1, description="Command to run a stdio MCP server")
    args: list[str] = Field(default_factory=list, description="Command arguments")
    env: Dict[str, str] = Field(default_factory=dict, description="Environment variables")
    transport: str | None = Field(None, min_length=1, description="Remote transport, e.g. streamable-http")
    url: str | None = Field(None, min_length=1, description="Remote server URL")
    headers: Dict[str, str] | None = Field(None, description="HTTP headers for a remote server (e.g. API-key auth)")
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


class ServersListResponse(ServersResponse):
    managed_keys: list[str]


class PutServersRequest(BaseModel):
    servers: Dict[str, Any] = Field(..., description="Complete servers dict to replace")


class PatchServerRequest(ServerEntry):
    pass


def _check_no_sanitized_name_collisions(servers: dict[str, dict]) -> None:
    """M6-R26 fix round 1, minor: two server names that sanitize to the same
    ``MCP_<SERVER>_<VAR>`` prefix (e.g. ``acme-crm`` vs ``acme_crm``) would
    otherwise only be caught much later, inside
    ``hermes_profiles.translate_mcp_servers`` -- which raises a ``ValueError``
    deep inside an AgentCore write's best-effort profile refresh (swallowed
    there so a hiccup can't fail a committed write), silently landing a
    profile whose Hermes env vars never actually updated behind a 200 OK.
    Reject the collision here instead, at submission time, using the exact
    same sanitizer so the two can never disagree. Scoped to the payload being
    submitted -- checking against already-connected integrations is Q9c's
    manual-vs-integration shadowing work."""
    from core.services.hermes_profiles import _mcp_env_key

    seen: dict[str, str] = {}
    for name, entry in servers.items():
        for field in ("env", "headers"):
            values = entry.get(field)
            if not isinstance(values, dict):
                continue
            for var_name in values:
                key = _mcp_env_key(name, var_name)
                origin = seen.setdefault(key, name)
                if origin != name:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Server names {origin!r} and {name!r} collide once sanitized to the "
                            f"same env var ({key!r}) -- rename one of them."
                        ),
                    )


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
    _check_no_sanitized_name_collisions(normalized)
    return normalized


async def _reject_integration_owned_key_collisions(owner_id: str, names: set[str]) -> None:
    """Ruling M6-R32: a manual server whose key equals a CONNECTED integration's owned key is
    rejected here, at write time, with a 409 naming the integration -- instead of being
    silently overwritten by the very next reconcile/refresh (ECS's own re-assert-after-write,
    or AgentCore's build_connected_integration_servers seeding manual first) and vanishing from
    the response with no signal to the caller. Checked before either runtime branch, since both
    exhibit the same silent-overwrite shape today."""
    owned = await integration_owned_server_keys(owner_id)
    collisions = sorted(names & owned.keys())
    if collisions:
        name = collisions[0]
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{name}' is owned by the connected {owned[name]!r} integration -- "
                f"rename this server or disconnect {owned[name]!r} first."
            ),
        )


async def _agentcore_refresh(owner_id: str) -> dict:
    """AgentCore twin of the ECS write-then-reconcile-then-reread sequence: fan
    the (manual ∪ connected-integration) merge out to every Hermes profile,
    then return that merge for the response body -- the manual write itself
    already landed on the reserved integrations_repo.MANUAL_MCP_KEY row before
    this is called. Best-effort: the manual write already committed, so a
    profile-refresh hiccup must not 500 it."""
    try:
        await reconcile_integrations(owner_id)
    except Exception:
        logger.exception("hermes profile refresh after manual MCP write failed for %s", owner_id)
    return await build_connected_integration_servers(owner_id)


@router.get(
    "/integrations/mcp/servers",
    summary="List configured MCP servers",
    description=(
        "Returns the owner's manual MCP server entries merged with their "
        "connected integrations'. Org callers must be org_admin — the entries "
        "carry per-server `env` secrets that non-admin members shouldn't "
        "see (mirrors the existing org-admin gate on the write endpoints "
        "and the GET /config gate)."
    ),
    response_model=ServersListResponse,
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
    # Keep the merged server map for existing readers; managed_keys lets the
    # editor distinguish provider-owned entries from owner-editable ones.
    return {
        "servers": await build_connected_integration_servers(owner_id),
        "managed_keys": sorted(await integration_owned_server_keys(owner_id)),
    }


@router.put(
    "/integrations/mcp/servers",
    summary="Replace all MCP server configurations",
    description="Replaces the entire manual mcp.servers dict for the owner.",
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
    await _reject_integration_owned_key_collisions(owner_id, set(servers))
    # The manual bucket IS the whole replace target, mirroring PUT's "keys not
    # present are deleted" semantics for the manual-only dict.
    await integrations_repo.set_manual_mcp_servers(owner_id, servers)
    return {"servers": await _agentcore_refresh(owner_id)}


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
    await _reject_integration_owned_key_collisions(owner_id, {name})
    manual = await integrations_repo.get_manual_mcp_servers(owner_id)
    manual[name] = entry
    await integrations_repo.set_manual_mcp_servers(owner_id, manual)
    return {"servers": await _agentcore_refresh(owner_id)}


@router.delete(
    "/integrations/mcp/servers/{name}",
    summary="Remove an MCP server",
    description="Removes a manual MCP server entry by name.",
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
    manual = await integrations_repo.get_manual_mcp_servers(owner_id)
    manual.pop(name, None)
    await integrations_repo.set_manual_mcp_servers(owner_id, manual)
    return {"servers": await _agentcore_refresh(owner_id)}
