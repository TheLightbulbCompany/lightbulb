"""The backfill merges each roster agent's own Paperclip identity into its stored .env, skips
(and fails the run on) an agent whose identity is incomplete, never writes on --dry-run, and
keeps going past a failing owner."""

from unittest.mock import AsyncMock

import pytest

import scripts.backfill_paperclip_identity_env as mod
from core.services import hermes_profiles


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(hermes_profiles.settings, "PAPERCLIP_INTERNAL_URL", "http://pc:3100")
    writes = []
    monkeypatch.setattr(mod, "patch_profile_env_and_config", lambda o, a, e, c: writes.append((o, a, e, c)))
    return writes


@pytest.mark.asyncio
async def test_patches_each_agent_with_its_own_identity(monkeypatch, env):
    roster = [{"id": "a1", "companyId": "c1"}, {"id": "a2", "companyId": "c1"}]
    monkeypatch.setattr(mod, "load_agent_roster", AsyncMock(return_value=roster))

    assert await mod.main(["--owner-id", "org_1"]) == 0
    assert env == [
        (
            "org_1",
            "a1",
            {"PAPERCLIP_API_URL": "http://pc:3100", "PAPERCLIP_AGENT_ID": "a1", "PAPERCLIP_COMPANY_ID": "c1"},
            {},
        ),
        (
            "org_1",
            "a2",
            {"PAPERCLIP_API_URL": "http://pc:3100", "PAPERCLIP_AGENT_ID": "a2", "PAPERCLIP_COMPANY_ID": "c1"},
            {},
        ),
    ]


@pytest.mark.asyncio
async def test_dry_run_never_writes_and_incomplete_identity_fails_the_run(monkeypatch, env):
    monkeypatch.setattr(
        mod, "load_agent_roster", AsyncMock(return_value=[{"id": "a1", "companyId": "c1"}, {"id": "a2"}])
    )

    assert await mod.main(["--owner-id", "org_1", "--dry-run"]) == 1
    assert env == []


@pytest.mark.asyncio
async def test_a_failing_owner_does_not_stop_the_fleet(monkeypatch, env):
    monkeypatch.setattr(
        mod.container_repo,
        "scan_all",
        AsyncMock(
            return_value=[
                {"owner_id": "org_bad", "agentcore_runtime_arn": "arn:x"},
                {"owner_id": "org_ok", "agentcore_runtime_arn": "arn:y"},
                {"owner_id": "org_none"},
            ]
        ),
    )

    async def roster(owner_id):
        if owner_id == "org_bad":
            raise RuntimeError("paperclip down")
        return [{"id": "a1", "companyId": "c1"}]

    monkeypatch.setattr(mod, "load_agent_roster", roster)
    assert await mod.main(["--all"]) == 1
    assert [w[0] for w in env] == ["org_ok"]
