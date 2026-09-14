"""backfill_adapter_gateway_urls: dry run by default (no CLI flag == no
writes); --apply is required to mutate a stale openclaw_gateway adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.paperclip_admin_client import PaperclipApiError
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
        result = await script.main(apply=apply)
    assert result.changed == 1
    assert result.skipped == 0
    assert result.failed == 0
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
        result = await script.main(apply=False)
    assert result.changed == 1
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
        result = await script.main(apply=True)
    assert result.changed == 0
    admin.patch_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_409_on_patch_is_skipped_not_aborted(capsys):
    """Prod incident: --apply hit a 409 (agent pending board approval) on
    agent 60/87 and the loop had no error handling, so it raised and the
    remaining 28 agents were never attempted. A 409 must be logged +
    counted as skipped and the loop must continue to the next agent."""
    agents = {
        "agents": [
            {
                "id": "ag-pending",
                "adapterType": "openclaw_gateway",
                "adapterConfig": {"url": "wss://ws-dev.flickmeon.ai/?token=t&clientId=x"},
            },
            {
                "id": "ag-next",
                "adapterType": "openclaw_gateway",
                "adapterConfig": {"url": "wss://ws-dev.flickmeon.ai/?token=t&clientId=y"},
            },
        ]
    }
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=agents)
    admin.patch_agent = AsyncMock(
        side_effect=[
            PaperclipApiError(
                "PATCH /api/agents/ag-pending -> 409",
                409,
                {"error": "Pending approval agent configuration cannot be changed before board approval"},
            ),
            None,
        ]
    )
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
        result = await script.main(apply=True)
    assert admin.patch_agent.await_count == 2  # ag-next was still attempted
    assert result.changed == 1
    assert result.skipped == 1
    assert result.failed == 0
    assert script.exit_code(result) == 0
    http.aclose.assert_awaited_once()
    out = capsys.readouterr().out
    assert "SKIP company=co-1 agent=ag-pending 409" in out
    assert "patched: 1 skipped: 1 failed: 0" in out


@pytest.mark.asyncio
async def test_non_409_error_is_counted_and_flips_exit_code(capsys):
    """A non-409 error (e.g. a 500) must not abort the run either, but
    unlike a 409 it should make the process exit non-zero."""
    agents = {
        "agents": [
            {
                "id": "ag-broken",
                "adapterType": "openclaw_gateway",
                "adapterConfig": {"url": "wss://ws-dev.flickmeon.ai/?token=t&clientId=x"},
            },
        ]
    }
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=agents)
    admin.patch_agent = AsyncMock(
        side_effect=PaperclipApiError("PATCH /api/agents/ag-broken -> 500", 500, {"error": "boom"})
    )
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
        result = await script.main(apply=True)
    assert result.changed == 0
    assert result.failed == 1
    assert script.exit_code(result) == 1
    http.aclose.assert_awaited_once()
    out = capsys.readouterr().out
    assert "FAIL company=co-1 agent=ag-broken 500" in out
