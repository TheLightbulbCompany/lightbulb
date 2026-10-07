"""refresh_owner_facts: rewrite every provisioned owner's lightbulb-facts.json through the backend's
own writer, so a new fact (e.g. app_url) reaches workspaces without waiting for their next
provision / provider switch / billing webhook."""

from unittest.mock import AsyncMock

import pytest

from scripts import refresh_owner_facts as mod


@pytest.mark.asyncio
async def test_all_refreshes_only_owners_with_a_runtime(monkeypatch):
    rows = [
        {"owner_id": "org_b", "agentcore_runtime_arn": "arn:b"},
        {"owner_id": "org_a", "agentcore_runtime_arn": "arn:a"},
        {"owner_id": "org_c"},  # no runtime: no profiles to write into
    ]
    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=rows))
    write = AsyncMock()
    monkeypatch.setattr(mod, "write_owner_facts", write)

    assert await mod.main(["--all"]) == 0
    assert [c.args[0] for c in write.await_args_list] == ["org_a", "org_b"]


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(
        mod.container_repo,
        "scan_all",
        AsyncMock(return_value=[{"owner_id": "org_a", "agentcore_runtime_arn": "arn:a"}]),
    )
    write = AsyncMock()
    monkeypatch.setattr(mod, "write_owner_facts", write)
    assert await mod.main(["--all", "--dry-run"]) == 0
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_owner(monkeypatch):
    write = AsyncMock()
    monkeypatch.setattr(mod, "write_owner_facts", write)
    assert await mod.main(["--owner-id", "org_x"]) == 0
    write.assert_awaited_once_with("org_x")
