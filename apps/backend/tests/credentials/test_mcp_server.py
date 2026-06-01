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


def test_build_block_slack_resolves_literals_and_token():
    # Slack's three write-tool flags ship as `literal:true` so they resolve to
    # the static string "true" regardless of the credential, while the token
    # resolves from the pasted credential — the curated full-access default.
    block = build_mcp_server_block("slack", {"token": "xoxp-abc"})
    assert block["env"] == {
        "SLACK_MCP_XOXP_TOKEN": "xoxp-abc",
        "SLACK_MCP_ADD_MESSAGE_TOOL": "true",
        "SLACK_MCP_REACTION_TOOL": "true",
        "SLACK_MCP_MARK_TOOL": "true",
    }


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
async def test_build_connected_map_skips_orphan_unknown_provider(monkeypatch):
    # A real owner can have a status row for a provider that was since removed
    # from the registry (e.g. linear). The registry lookup KeyErrors for it;
    # the build must skip the orphan row, not crash and drop every server.
    from core.services.credentials import mcp_server as m

    monkeypatch.setattr(
        m.integrations_repo,
        "list_for_owner",
        AsyncMock(
            return_value=[
                {"provider": "linear", "status": "connected"},
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
