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
from core.services import config_patcher, runtime_transport
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
        "connectionTimeoutMs": spec.connection_timeout_ms,
    }


async def build_connected_integration_servers(owner_id: str, *, pending: str | None = None) -> dict[str, dict]:
    """{server_key: live mcp.servers block} for every connected provider that owns
    a server entry. The single source of truth used by both the provision-time
    merge and the live reconcile. Two server-owning shapes are emitted:

      - mcp_server wiring: the full {command, args, env} block, built from the
        registry spec x the retained Secrets-Manager secret. Actor-refresh
        providers retain no SM secret (get -> None) and are skipped.
      - mcp_remote spec: a remote {transport, url} block. These providers ride the
        materializer rail and self-refresh in-container (no retained SM secret), so
        the block is emitted from the registry + connected status alone — without
        it a full-config overwrite (reprovision) drops the live server entry even
        though the OAuth token file survives on EFS.

    A THIRD, non-provider shape is folded in too: manually-defined servers an
    owner adds via the Integrations panel's raw MCP editor
    (routers/integrations.py). On ECS those go straight into openclaw.json's
    mcp.servers and this function never sees them. An AgentCore owner has no
    openclaw.json to write into, so the manual editor stores them on the
    reserved integrations_repo.MANUAL_MCP_KEY row instead, and this is where
    they surface — seeded FIRST so a same-key connected integration below
    always wins the collision, matching the ECS reconcile's own "integration
    ownership wins" rule (routers/integrations.py's own re-assert-after-write
    comments).

    ``pending`` (M6-R26 fix round 1, Critical 1): a provider mid-connect whose
    ``integrations`` status row has not been written yet. All three connect
    doors (connect_paste/aws_complete/oauth_callback in routers/connections.py)
    call ``apply_connection`` — and therefore, on an AgentCore owner,
    ``reconcile_integrations`` and this function — BEFORE
    ``integrations_repo.put_status(..., "connected")``. Without this, the
    FIRST connect's profile rewrite runs one beat too early: it never sees
    its own new row, so every profile gets rewritten WITHOUT the provider
    actually being connected. Treated as an extra connected row for THIS
    build only — never persisted, and a no-op if the row already exists
    (e.g. a reconnect, or the ECS/manual-MCP callers that never pass it).
    """
    servers: dict[str, dict] = dict(await integrations_repo.get_manual_mcp_servers(owner_id))
    rows = await integrations_repo.list_for_owner(owner_id)
    if pending and not any(row.get("provider") == pending for row in rows):
        rows = [*rows, {"provider": pending, "status": "connected"}]
    for row in rows:
        provider = row.get("provider")
        if not provider or row.get("status", "connected") != "connected":
            continue
        try:
            prov = registry.get_provider(provider)
        except KeyError:
            # Orphan status row for a provider no longer in the registry (e.g. a
            # since-removed integration). Skip it rather than failing the whole build.
            continue
        # Remote MCP server (mcp_oauth rail): re-publish the {transport, url} block
        # from the registry alone — actor-refresh, so there is no retained secret.
        remote = prov.mcp_remote
        if remote is not None:
            servers[remote.key] = remote.server_block()
            continue
        # Stdio MCP server whose credential is a FILE on EFS (qbo_dotenv rail).
        # Same reasoning as the remote branch: actor-refresh, no retained secret,
        # and the block carries only the store PATH — so it is rebuildable from
        # the registry, and the token store survives the config overwrite.
        stdio = prov.mcp_stdio
        if stdio is not None:
            from core.services.credentials.materializers.qbo_dotenv import container_store_path

            servers[stdio.key] = stdio.server_block(container_store_path())
            continue
        spec = prov.wiring.mcp_server
        if spec is None:
            continue
        cred = await integration_secret_store.get(owner_id, provider)
        if not cred:
            continue
        block = build_mcp_server_block(provider, cred)
        if block is not None:
            servers[spec.key] = block
    return servers


async def integration_owned_server_keys(owner_id: str) -> dict[str, str]:
    """``{server_key: provider}`` for every CONNECTED integration that owns an ``mcp.servers``
    entry -- the same three server-owning shapes ``build_connected_integration_servers``
    recognizes, but without touching Secrets Manager: a manual-server collision check (M6-R32)
    only needs to know WHICH keys are integration-owned, never the credential value, so this
    skips the ``integration_secret_store.get`` round trip that function pays per ``mcp_server``
    row."""
    owned: dict[str, str] = {}
    for row in await integrations_repo.list_for_owner(owner_id):
        provider = row.get("provider")
        if not provider or row.get("status", "connected") != "connected":
            continue
        try:
            prov = registry.get_provider(provider)
        except KeyError:
            continue
        if prov.mcp_remote is not None:
            owned[prov.mcp_remote.key] = provider
        elif prov.mcp_stdio is not None:
            owned[prov.mcp_stdio.key] = provider
        elif prov.wiring.mcp_server is not None:
            owned[prov.wiring.mcp_server.key] = provider
    return owned


async def reconcile_integrations(owner_id: str, *, agentcore: bool | None = None, pending: str | None = None) -> None:
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
    no config yet (ConfigPatchError swallowed — a provision will inject instead).

    M4-R11: an AgentCore owner has no openclaw.json and no live gateway socket to patch — the
    equivalent rewrite goes through hermes_profiles.refresh_integrations, which touches EVERY
    profile's config.yaml mcp_servers + .env + mcp-tokens/, then bumps each .revision so the
    bridge's own ensure_profile() re-pull picks it up before the agent's next turn.

    ``agentcore`` (M6-R26 fix round 1, Important 3+4): a caller that already
    resolved ``runtime_transport.is_agentcore_owner`` for this same request
    (apply_connection, _disconnect_teardown) passes its answer straight
    through instead of paying for a second DynamoDB read here. ``None``
    (every other caller — routers/integrations.py's manual-MCP writes,
    catalog_service's marketplace deploy) resolves it itself, unchanged from
    before this parameter existed.

    ``pending`` is forwarded to ``build_connected_integration_servers`` /
    ``hermes_profiles.refresh_integrations`` — see the former's docstring.
    """
    if agentcore is None:
        agentcore = await runtime_transport.is_agentcore_owner(owner_id)
    if agentcore:
        from core.services.hermes_profiles import refresh_integrations
        from core.services.messaging import load_agent_roster

        # I1 (fix round 1 continuation, M4-R46): refresh_integrations is genuinely async now —
        # no more run_in_thread hop. It was never blocking anything but its own small S3 PUTs,
        # and the old asyncio.run()-based sync wrapper it called into is gone.
        agents = await load_agent_roster(owner_id)
        await refresh_integrations(owner_id, [agent["id"] for agent in agents], pending=pending)
        return
    for key, block in (await build_connected_integration_servers(owner_id, pending=pending)).items():
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
