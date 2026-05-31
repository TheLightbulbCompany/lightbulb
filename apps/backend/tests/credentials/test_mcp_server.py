import pytest
from unittest.mock import AsyncMock

from core.services.credentials.mcp_server import build_mcp_server_block


def test_build_block_from_paste_credential():
    assert build_mcp_server_block("stripe", {"token": "sk_live_X"}) == {
        "command": "npx",
        "args": ["-y", "@stripe/mcp", "--tools=all"],
        "env": {"STRIPE_SECRET_KEY": "sk_live_X"},
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
