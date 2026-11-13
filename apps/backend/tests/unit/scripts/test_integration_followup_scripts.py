from unittest.mock import AsyncMock

import pytest

from scripts import announce_pending_reconnects


@pytest.mark.asyncio
async def test_announce_defaults_to_dry_run_and_apply_announces_only_reconnect_required(monkeypatch, capsys):
    rows = [
        {"owner_id": "org_a", "provider": "microsoft365", "status": "reconnect_required"},
        {"owner_id": "org_b", "provider": "github", "status": "connected"},
    ]
    monkeypatch.setattr("core.repositories.integrations_repo.scan_all", AsyncMock(return_value=rows))
    announce = AsyncMock()
    monkeypatch.setattr("core.services.credentials.access_token._announce_reconnect", announce)
    facts = AsyncMock()
    monkeypatch.setattr(announce_pending_reconnects, "write_owner_facts", facts)

    assert await announce_pending_reconnects.main([]) == 0
    announce.assert_not_awaited()
    facts.assert_not_awaited()
    assert "org_a microsoft365 would-announce" in capsys.readouterr().out

    assert await announce_pending_reconnects.main(["--apply"]) == 0
    announce.assert_awaited_once_with("org_a", "microsoft365")
    facts.assert_awaited_once_with("org_a")
