"""Build + reconcile the per-owner MCP servers an integration owns.

A curated provider with `mcp_server` wiring owns one `mcp_servers.<key>` entry in
every Hermes profile's config.yaml. Connect installs the full {command,args,env}
block; disconnect removes it; reconcile re-asserts it from the durable sources
(registry spec x Secrets-Manager secret). Actor-refresh providers (gog) retain no
SM secret and persist via their own profile files — reconcile skips them.
"""

from __future__ import annotations

from typing import Any

from core.repositories import integrations_repo
from core.services.credentials import integration_secret_store, registry


def build_mcp_server_block(provider_key: str, credential: dict[str, Any]) -> dict | None:
    """The {command, args, env} block for a provider's MCP server, or None if the
    provider has no mcp_server wiring. Each env var's value is resolved from the
    credential via the registry's source string (e.g. "input.token") — so a
    paste-key, OAuth, or multi-env provider is pure registry data."""
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
        it a reprovision drops the live server entry even though the OAuth token
        file survives in the profile.

    A THIRD, non-provider shape is folded in too: manually-defined servers an
    owner adds via the Integrations panel's raw MCP editor
    (routers/integrations.py). There is no owner-editable config file to write
    into, so the manual editor stores them on the reserved
    integrations_repo.MANUAL_MCP_KEY row instead, and this is where they surface —
    seeded FIRST so a same-key connected integration below always wins the
    collision ("integration ownership wins").

    ``pending`` (M6-R26 fix round 1, Critical 1): a provider mid-connect whose
    ``integrations`` status row has not been written yet. All three connect
    doors (connect_paste/aws_complete/oauth_callback in routers/connections.py)
    call ``apply_connection`` — and therefore ``reconcile_integrations`` and
    this function — BEFORE
    ``integrations_repo.put_status(..., "connected")``. Without this, the
    FIRST connect's profile rewrite runs one beat too early: it never sees
    its own new row, so every profile gets rewritten WITHOUT the provider
    actually being connected. Treated as an extra connected row for THIS
    build only — never persisted, and a no-op if the row already exists
    (e.g. a reconnect, or the manual-MCP caller that never passes it).
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
        # Remote MCP server: brokered grants use a bearer reference; native actor
        # grants use Hermes's OAuth token file.
        remote = prov.mcp_remote
        if remote is not None:
            servers[remote.key] = remote.server_block(brokered=registry.refresh_owner_for(prov) == "backend")
            continue
        # Stdio MCP server whose credential is a FILE in the profile (qbo_dotenv rail).
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


async def reconcile_integrations(owner_id: str, *, pending: str | None = None) -> None:
    """Re-assert every connected integration's MCP wiring into EVERY Hermes profile.

    ``hermes_profiles.refresh_integrations`` rewrites each profile's config.yaml
    ``mcp_servers`` + ``.env`` + ``mcp-tokens/`` from the durable sources (registry
    spec x Secrets-Manager secret), then bumps each ``.revision`` so the bridge's
    own ``ensure_profile()`` re-pull picks it up before the agent's next turn. It is
    idempotent — a profile whose projection is already correct is not rewritten.

    Materializer providers persist through their own profile files, not the MCP
    block, so they are not re-applied here.

    ``pending`` is forwarded to ``hermes_profiles.refresh_integrations`` — see
    ``build_connected_integration_servers``' docstring.
    """
    from core.services.hermes_profiles import refresh_integrations
    from core.services.messaging import load_agent_roster

    # I1 (fix round 1 continuation, M4-R46): refresh_integrations is genuinely async — no
    # run_in_thread hop. It was never blocking anything but its own small S3 PUTs.
    agents = await load_agent_roster(owner_id)
    await refresh_integrations(owner_id, [agent["id"] for agent in agents], pending=pending)
