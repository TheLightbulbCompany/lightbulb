"""backfill_adapter_gateway_urls: dry run by default (no CLI flag == no
writes); --apply is required to mutate a stale openclaw_gateway adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts import backfill_adapter_gateway_urls as script

AGENTS = {
    "agents": [
        {
            "id": "ag-1",
            "adapterType": "openclaw_gateway",
            "adapterConfig": {
                "url": "wss://ws-dev.flickmeon.ai/?token=t&clientId=paperclip-operator",
                "agentId": "x",
            },
        },
        {
            "id": "ag-2",
            "adapterType": "openclaw_gateway",
            "adapterConfig": {"url": "wss://bv-dev.flickmeon.ai/api/v1/gateway/ws?token=t&clientId=paperclip-operator"},
        },
        {"id": "ag-3", "adapterType": "other", "adapterConfig": {}},
        {
            "id": "ag-4",
            "adapterType": "openclaw_gateway",
            "adapterConfig": {"url": "ws://localhost:8000", "agentId": "y"},
        },
    ]
}


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True])
async def test_rewrites_only_stale_openclaw_adapters(apply):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=AGENTS)
    admin.patch_agent = AsyncMock()
    http = MagicMock()
    http.aclose = AsyncMock()
    with (
        patch("scripts.backfill_adapter_gateway_urls._companies", new_callable=AsyncMock, return_value=["co-1"]),
        patch(
            "scripts.backfill_adapter_gateway_urls._open_admin_session",
            new_callable=AsyncMock,
            return_value=(http, admin, "cookie"),
        ),
        patch("scripts.backfill_adapter_gateway_urls.settings") as settings,
    ):
        settings.GATEWAY_RELAY_URL = "wss://bv-dev.flickmeon.ai/api/v1/gateway/ws"
        changed = await script.main(apply=apply)
    assert changed == 1
    if apply:
        admin.patch_agent.assert_awaited_once()
        body = admin.patch_agent.await_args.kwargs["body"]
        assert (
            body["adapterConfig"]["url"]
            == "wss://bv-dev.flickmeon.ai/api/v1/gateway/ws?token=t&clientId=paperclip-operator"
        )
        assert body["adapterConfig"]["agentId"] == "x"
        assert body["replaceAdapterConfig"] is True
        assert admin.patch_agent.await_args.kwargs["session_cookie"] == "cookie"
        assert admin.patch_agent.await_args.kwargs["agent_id"] == "ag-1"
    else:
        admin.patch_agent.assert_not_awaited()
    http.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_agents_called_with_keyword_args_and_bare_list_shape_accepted():
    """`list_agents` is keyword-only upstream, and upstream itself can return
    a bare list (not the BFF-enveloped dict) -- both must work."""
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=AGENTS["agents"])  # bare list, no envelope
    admin.patch_agent = AsyncMock()
    http = MagicMock()
    http.aclose = AsyncMock()
    with (
        patch("scripts.backfill_adapter_gateway_urls._companies", new_callable=AsyncMock, return_value=["co-1"]),
        patch(
            "scripts.backfill_adapter_gateway_urls._open_admin_session",
            new_callable=AsyncMock,
            return_value=(http, admin, "cookie"),
        ),
        patch("scripts.backfill_adapter_gateway_urls.settings") as settings,
    ):
        settings.GATEWAY_RELAY_URL = "wss://bv-dev.flickmeon.ai/api/v1/gateway/ws"
        changed = await script.main(apply=False)
    assert changed == 1
    admin.list_agents.assert_awaited_once_with(session_cookie="cookie", company_id="co-1")


@pytest.mark.asyncio
async def test_localhost_adapter_is_never_patched():
    """ws://localhost:<port> is the legitimate local-dev shape
    (paperclip_provisioning._ws_gateway_url("")), still accepted by the
    paperclip_adapter_config.py allowlist -- the backfill must leave it
    alone rather than rewriting it onto the relay."""
    agents = {
        "agents": [
            {
                "id": "ag-local",
                "adapterType": "openclaw_gateway",
                "adapterConfig": {"url": "ws://localhost:8000", "agentId": "z"},
            },
        ]
    }
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=agents)
    admin.patch_agent = AsyncMock()
    http = MagicMock()
    http.aclose = AsyncMock()
    with (
        patch("scripts.backfill_adapter_gateway_urls._companies", new_callable=AsyncMock, return_value=["co-1"]),
        patch(
            "scripts.backfill_adapter_gateway_urls._open_admin_session",
            new_callable=AsyncMock,
            return_value=(http, admin, "cookie"),
        ),
        patch("scripts.backfill_adapter_gateway_urls.settings") as settings,
    ):
        settings.GATEWAY_RELAY_URL = "wss://bv-dev.flickmeon.ai/api/v1/gateway/ws"
        changed = await script.main(apply=True)
    assert changed == 0
    admin.patch_agent.assert_not_awaited()
