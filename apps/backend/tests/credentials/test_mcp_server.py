import pytest
from unittest.mock import AsyncMock

from core.services.credentials.mcp_server import build_mcp_server_block


def test_build_block_from_paste_credential():
    assert build_mcp_server_block("stripe", {"token": "sk_live_X"}) == {
        "command": "npx",
        "args": ["-y", "@stripe/mcp", "--tools=all"],
        "env": {"STRIPE_SECRET_KEY": "sk_live_X"},
    }


def test_resolve_source_literal_returns_static_string():
    # `literal:` lets a provider set a static env value (e.g. a feature flag)
    # with no dependence on the credential or raw input.
    from core.services.credentials.apply_connection import _resolve_source

    assert _resolve_source("literal:true", {}, {}) == "true"
    # Everything after the prefix is the value verbatim, including a colon.
    assert _resolve_source("literal:a:b", {}, {}) == "a:b"


def test_build_block_mixes_literal_and_credential_env(monkeypatch):
    # A provider can set a static flag alongside a credential-derived value; both
    # resolve through the same _resolve_source the env source-map already uses.
    import core.services.credentials.mcp_server as m
    from core.services.credentials.registry import McpServer, Provider, Wiring

    provider = Provider(
        key="slack",
        display_name="Slack",
        acquisition="paste_token",
        actor="mcp",
        refresh_owner="none",
        wiring=Wiring(
            mcp_server=McpServer(
                key="slack",
                command="npx",
                env={"FLAG": "literal:true", "TOKEN": "input.token"},
            )
        ),
    )
    monkeypatch.setattr(m.registry, "get_provider", lambda key: provider)

    block = build_mcp_server_block("slack", {"token": "xoxb-123"})
    assert block["env"] == {"FLAG": "true", "TOKEN": "xoxb-123"}


def test_build_block_none_for_non_mcp_provider():
    assert build_mcp_server_block("google", {"access_token": "a"}) is None  # materializer, not mcp_server


@pytest.mark.asyncio
async def test_build_connected_map_skips_actor_and_unretained(monkeypatch):
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(
            return_value=[
                {"provider": "stripe", "status": "connected"},
                {"provider": "google", "status": "connected"},
            ]
        ),
    )
    monkeypatch.setattr(
        m.integration_secret_store,
        "get",
        AsyncMock(side_effect=lambda o, p: {"token": "sk_X"} if p == "stripe" else None),
    )
    servers = await m.build_connected_integration_servers("org_1")
    assert servers == {
        "stripe": {
            "command": "npx",
            "args": ["-y", "@stripe/mcp", "--tools=all"],
            "env": {"STRIPE_SECRET_KEY": "sk_X"},
        }
    }


@pytest.mark.asyncio
async def test_build_connected_map_emits_remote_block_without_retained_secret(monkeypatch):
    # An mcp_remote provider (attio) rides the materializer rail and self-refreshes
    # in-container, so it has NO retained Secrets-Manager secret. Its live
    # {transport, url} block must still be emitted from the registry + connected
    # status alone — otherwise a reprovision drops the server entry. The mcp_server
    # provider (stripe) is still included; the actor-refresh one (google) excluded.
    # Uses the real registry attio entry, so it pins the YAML mcp_remote wiring.
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(
            return_value=[
                {"provider": "attio", "status": "connected"},
                {"provider": "stripe", "status": "connected"},
                {"provider": "google", "status": "connected"},
            ]
        ),
    )
    # attio + google have no retained secret; stripe does. get() must NOT gate the
    # attio remote block (it is never consulted for it).
    secret_get = AsyncMock(side_effect=lambda o, p: {"token": "sk_X"} if p == "stripe" else None)
    monkeypatch.setattr(m.integration_secret_store, "get", secret_get)

    servers = await m.build_connected_integration_servers("org_1")
    # The rehydrated remote block carries auth: "oauth" so a reprovision re-arms the
    # SDK OAuth provider — identical to what the live materializer write publishes.
    assert servers == {
        "attio": {
            "transport": "streamable-http",
            "url": "https://mcp.attio.com/mcp",
            "auth": "oauth",
            "requestTimeoutMs": 60000,
        },
        "stripe": {
            "command": "npx",
            "args": ["-y", "@stripe/mcp", "--tools=all"],
            "env": {"STRIPE_SECRET_KEY": "sk_X"},
        },
    }


@pytest.mark.asyncio
async def test_build_connected_map_skips_disconnected_remote_provider(monkeypatch):
    # A disconnected mcp_remote row must not be re-published (no stale server block).
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(return_value=[{"provider": "attio", "status": "disconnected"}]),
    )
    monkeypatch.setattr(m.integration_secret_store, "get", AsyncMock(return_value=None))
    assert await m.build_connected_integration_servers("org_1") == {}


@pytest.mark.asyncio
async def test_build_connected_map_skips_orphan_unknown_provider(monkeypatch):
    # A real owner can have a status row for a provider that was since removed
    # from the registry. The registry lookup KeyErrors for it; the build must
    # skip the orphan row, not crash and drop every server.
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(
            return_value=[
                {"provider": "since-removed-provider", "status": "connected"},
                {"provider": "stripe", "status": "connected"},
            ]
        ),
    )
    monkeypatch.setattr(
        m.integration_secret_store,
        "get",
        AsyncMock(side_effect=lambda o, p: {"token": "sk_X"} if p == "stripe" else None),
    )
    servers = await m.build_connected_integration_servers("org_1")
    assert servers == {
        "stripe": {
            "command": "npx",
            "args": ["-y", "@stripe/mcp", "--tools=all"],
            "env": {"STRIPE_SECRET_KEY": "sk_X"},
        }
    }


@pytest.mark.asyncio
async def test_reconcile_writes_each_block_per_server(monkeypatch):
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m,
        "build_connected_integration_servers",
        AsyncMock(return_value={"stripe": {"command": "npx", "args": [], "env": {"STRIPE_SECRET_KEY": "sk_X"}}}),
    )
    # No openclaw_paths providers connected — the reconcile's second pass no-ops.
    monkeypatch.setattr(m.integrations_repo, "list_for_owner", AsyncMock(return_value=[]))
    writes = []

    async def _set(owner, path, value):
        writes.append((path, value))

    monkeypatch.setattr(m.config_patcher, "set_openclaw_config_path", _set)
    await m.reconcile_integrations("org_1")
    # per-server REPLACE of the stripe subtree only — preserves paperclip + user keys
    assert writes == [
        (["mcp", "servers", "stripe"], {"command": "npx", "args": [], "env": {"STRIPE_SECRET_KEY": "sk_X"}})
    ]


@pytest.mark.asyncio
async def test_reconcile_noops_when_config_missing(monkeypatch):
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m,
        "build_connected_integration_servers",
        AsyncMock(return_value={"stripe": {"command": "npx", "args": [], "env": {}}}),
    )

    async def _raise(owner, path, value):
        raise m.config_patcher.ConfigPatchError("no file")

    monkeypatch.setattr(m.config_patcher, "set_openclaw_config_path", _raise)
    await m.reconcile_integrations("org_1")  # must not raise


@pytest.mark.asyncio
async def test_reconcile_reapplies_openclaw_paths_provider(monkeypatch):
    """A connected openclaw_paths provider (parallel) is re-asserted on reconcile:
    each owned config path is re-written from the retained secret, so the bundled
    web-search selection + key survive a config overwrite. Uses the real registry
    entry, so it also pins the parallel YAML wiring."""
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(m, "build_connected_integration_servers", AsyncMock(return_value={}))
    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(return_value=[{"provider": "parallel", "status": "connected"}]),
    )
    monkeypatch.setattr(
        m.integration_secret_store,
        "get",
        AsyncMock(return_value={"token": "par-secret123"}),
    )
    writes = []

    async def _set(owner, path, value):
        writes.append((path, value))

    monkeypatch.setattr(m.config_patcher, "set_openclaw_config_path", _set)
    await m.reconcile_integrations("org_1")
    assert writes == [
        (["tools", "web", "search", "provider"], "parallel-free"),
        (["tools", "web", "search", "parallel", "apiKey"], "par-secret123"),
    ]


@pytest.mark.asyncio
async def test_reconcile_skips_disconnected_openclaw_paths_provider(monkeypatch):
    """A disconnected openclaw_paths row is not re-applied (no stale re-write)."""
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(m, "build_connected_integration_servers", AsyncMock(return_value={}))
    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(return_value=[{"provider": "parallel", "status": "disconnected"}]),
    )
    secret_get = AsyncMock(return_value={"token": "par-x"})
    monkeypatch.setattr(m.integration_secret_store, "get", secret_get)
    writes = []

    async def _set(owner, path, value):
        writes.append((path, value))

    monkeypatch.setattr(m.config_patcher, "set_openclaw_config_path", _set)
    await m.reconcile_integrations("org_1")
    assert writes == []
    secret_get.assert_not_awaited()
