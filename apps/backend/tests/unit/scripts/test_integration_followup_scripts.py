from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import announce_pending_reconnects, reapply_instagram_skill


@pytest.mark.asyncio
async def test_announce_defaults_to_dry_run_and_apply_announces_only_reconnect_required(monkeypatch, capsys):
    rows = [
        {"owner_id": "org_a", "provider": "microsoft365", "status": "reconnect_required"},
        {"owner_id": "org_b", "provider": "github", "status": "connected"},
    ]
    monkeypatch.setattr("core.repositories.integrations_repo.scan_all", AsyncMock(return_value=rows))
    announce = AsyncMock()
    monkeypatch.setattr("core.services.credentials.access_token._announce_reconnect", announce)

    assert await announce_pending_reconnects.main([]) == 0
    announce.assert_not_awaited()
    assert "org_a microsoft365 would-announce" in capsys.readouterr().out

    assert await announce_pending_reconnects.main(["--apply"]) == 0
    announce.assert_awaited_once_with("org_a", "microsoft365")


@pytest.mark.asyncio
async def test_instagram_skill_rewrites_only_profiles_holding_credentials(monkeypatch, capsys):
    monkeypatch.setattr(
        reapply_instagram_skill, "load_agent_roster", AsyncMock(return_value=[{"id": "a1"}, {"id": "a2"}])
    )
    monkeypatch.setattr(reapply_instagram_skill.settings, "OWNER_STATE_BUCKET", "bucket", raising=False)
    monkeypatch.setattr(
        "core.services.hermes_profiles._s3_get",
        lambda bucket, key: "{}" if "/a1/" in key else None,
    )
    write = MagicMock()
    monkeypatch.setattr("core.services.hermes_profiles.write_profile_subtree", write)

    assert await reapply_instagram_skill.main(["--owner-id", "org_a"]) == 0

    assert write.call_count == 1
    owner, agent, files, removed = write.call_args.args
    assert (owner, agent) == ("org_a", "a1")
    assert set(files) == {"skills/instagram/SKILL.md", "skills/instagram/token.sh"}  # never credentials.json
    out = capsys.readouterr().out
    assert "a1 updated" in out and "a2 skipped" in out
