"""scripts/reproject_hermes_connections.py — the ops door onto credentials.reproject."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scripts import reproject_hermes_connections as mod


async def test_prints_every_outcome_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        mod, "reproject_connections", AsyncMock(return_value={"github": "projected", "google": "needs_reconnect"})
    )

    assert await mod.main(["--owner-id", "org_1"]) == 0

    out = capsys.readouterr().out
    assert "org_1 github=projected" in out
    assert "org_1 google=needs_reconnect" in out


async def test_exits_non_zero_when_a_provider_errored(monkeypatch, capsys):
    monkeypatch.setattr(
        mod, "reproject_connections", AsyncMock(return_value={"github": "projected", "aws": "error:ClientError"})
    )

    assert await mod.main(["--owner-id", "org_1"]) == 1


async def test_dry_run_is_passed_through_and_flagged(monkeypatch, capsys):
    reproject = AsyncMock(return_value={"github": "projected"})
    monkeypatch.setattr(mod, "reproject_connections", reproject)

    assert await mod.main(["--owner-id", "org_1", "--dry-run"]) == 0

    reproject.assert_awaited_once_with("org_1", dry_run=True)
    assert "DRY RUN" in capsys.readouterr().out


async def test_owner_id_is_required(monkeypatch):
    with pytest.raises(SystemExit):
        await mod.main([])
