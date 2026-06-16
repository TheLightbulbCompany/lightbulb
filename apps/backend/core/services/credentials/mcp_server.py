"""Build + reconcile the per-owner MCP servers an integration owns.

A curated provider with `mcp_server` wiring owns `mcp.servers.<key>` in the
owner's openclaw.json. Connect installs the full {command,args,env} block;
disconnect removes it; reconcile re-asserts it from the durable sources
(registry spec x Secrets-Manager secret). Actor-refresh providers (gog) retain
no SM secret and persist via their EFS keyring — reconcile skips them.
"""

from __future__ import annotations

from typing import Any

from core.repositories import integrations_repo
from core.services import config_patcher
from core.services.credentials import integration_secret_store, registry


def build_mcp_server_block(provider_key: str, credential: dict[str, Any]) -> dict | None:
    """The {command, args, env} block for a provider's MCP server, or None if the
    provider has no mcp_server wiring. Each env var's value is resolved from the
    credential via the registry's source string (e.g. "input.token"), reusing the
    same _resolve_source the openclaw_paths rail uses — so a paste-key, OAuth, or
    multi-env provider is pure registry data."""
    spec = registry.get_provider(provider_key).wiring.mcp_server
    if spec is None:
        return None
    # Lazy import: apply_connection lazily imports THIS module's builder, so keep
    # the dependency one-directional at module-load time.
    from core.services.credentials.apply_connection import _resolve_source

    return {
        "command": spec.command,
        "args": list(spec.args),
        "env": {var: _resolve_source(src, credential, credential) for var, src in spec.env.items()},
    }


async def build_connected_integration_servers(owner_id: str) -> dict[str, dict]:
    """{provider_key: full_block} for every connected mcp_server provider whose
    credential is retained in Secrets Manager. The single source of truth used by
    both the provision-time merge and the live reconcile. Skips actor-refresh
    providers (SM get -> None) and non-mcp_server rails."""
    servers: dict[str, dict] = {}
    for row in await integrations_repo.list_for_owner(owner_id):
        provider = row.get("provider")
        if not provider or row.get("status", "connected") != "connected":
            continue
        try:
            spec = registry.get_provider(provider).wiring.mcp_server
        except KeyError:
            # Orphan status row for a provider no longer in the registry (e.g. a
            # since-removed integration). Skip it rather than failing the whole build.
            continue
        if spec is None:
            continue
        cred = await integration_secret_store.get(owner_id, provider)
        if not cred:
            continue
        block = build_mcp_server_block(provider, cred)
        if block is not None:
            servers[spec.key] = block
    return servers


async def reconcile_integrations(owner_id: str) -> None:
    """Re-assert every connected integration's config into the LIVE openclaw.json
    from the durable sources (registry spec x Secrets-Manager secret). Both
    openclaw.json-resident rails are re-applied, because a full config overwrite
    (reprovision) wipes both:

      - mcp_server: REPLACE mcp.servers.<key> (preserves the paperclip block +
        user-owned keys).
      - openclaw_paths: re-write each owned config path (e.g. a bundled web-search
        provider's tools.web.search.* selection + key).

    Materializer providers persist via their own EFS files (not openclaw.json), so
    they survive the overwrite and are not re-applied here. Per-path / per-server
    REPLACE is idempotent (set no-ops when unchanged). No-op if the container has
    no config yet (ConfigPatchError swallowed — a provision will inject instead)."""
    for key, block in (await build_connected_integration_servers(owner_id)).items():
        try:
            await config_patcher.set_openclaw_config_path(owner_id, ["mcp", "servers", key], block)
        except config_patcher.ConfigPatchError:
            return
    # openclaw_paths providers live in openclaw.json (unlike materializer files),
    # so a config overwrite drops them — re-write each owned path from the retained
    # secret. Lazy import keeps the module-load dependency one-directional.
    from core.services.credentials.apply_connection import _resolve_source

    for row in await integrations_repo.list_for_owner(owner_id):
        provider = row.get("provider")
        if not provider or row.get("status", "connected") != "connected":
            continue
        try:
            paths = registry.get_provider(provider).wiring.openclaw_paths
        except KeyError:
            continue
        if not paths:
            continue
        cred = await integration_secret_store.get(owner_id, provider)
        if not cred:
            continue
        for op in paths:
            try:
                await config_patcher.set_openclaw_config_path(
                    owner_id, op.path.split("."), _resolve_source(op.source, cred, cred)
                )
            except config_patcher.ConfigPatchError:
                return
