"""The reconcile-mcp-integrations ops script: find agents whose stored stdio MCP server lacks its
profile HOME, reconcile only those owners, never write on --dry-run, keep going past a failure."""

from unittest.mock import AsyncMock

import pytest

import scripts.reconcile_mcp_integrations as mod

QB = {"command": "/usr/local/bin/lightbulb-quickbooks-mcp", "env": {"QUICKBOOKS_TOKEN_STORE_PATH": "${X}"}}


def _wire(monkeypatch, configs, reconcile):
    monkeypatch.setattr(mod, "read_profile_config", lambda o, a: {"mcp_servers": configs[(o, a)]})
    monkeypatch.setattr(
        mod, "load_agent_roster", AsyncMock(side_effect=lambda o: [{"id": a} for (oo, a) in configs if oo == o])
    )
    monkeypatch.setattr(mod, "reconcile_integrations", reconcile)


@pytest.mark.asyncio
async def test_reconciles_only_owners_with_a_stale_stdio_server(monkeypatch, capsys):
    reconcile = AsyncMock()
    fixed = {**QB, "env": {**QB["env"], "HOME": "/opt/data/profiles/a2/home"}}
    _wire(monkeypatch, {("org_stale", "a1"): {"quickbooks": QB}, ("org_ok", "a2"): {"quickbooks": fixed}}, reconcile)
    monkeypatch.setattr(mod, "_targets", AsyncMock(return_value=["org_stale", "org_ok"]))

    assert await mod.main(["--all"]) == 0

    reconcile.assert_awaited_once_with("org_stale")
    assert "org_stale a1 reconcile quickbooks" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_dry_run_never_writes(monkeypatch, capsys):
    reconcile = AsyncMock()
    _wire(monkeypatch, {("org_stale", "a1"): {"quickbooks": QB}}, reconcile)

    assert await mod.main(["--owner-id", "org_stale", "--dry-run"]) == 0

    reconcile.assert_not_awaited()
    assert "would-reconcile quickbooks" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_failing_owner_does_not_stop_the_run(monkeypatch):
    reconcile = AsyncMock(side_effect=[RuntimeError("s3"), None])
    _wire(monkeypatch, {("org_x", "a1"): {"quickbooks": QB}, ("org_y", "a2"): {"quickbooks": QB}}, reconcile)
    monkeypatch.setattr(mod, "_targets", AsyncMock(return_value=["org_x", "org_y"]))

    assert await mod.main(["--all"]) == 1
    assert reconcile.await_count == 2
