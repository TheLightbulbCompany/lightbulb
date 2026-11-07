"""Id-preserving registry backfill and --verify (wave1-R Task 4)."""

import copy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import settings
from core.repositories import agent_registry_repo as repo
from core.services import agent_registry
from core.services.agent_registry import Diff

WS = "org_a"


@pytest.fixture
def paperclip(paperclip_roster):
    """The live Paperclip roster `_paperclip_agents` returns; tests mutate it."""
    state = copy.deepcopy(paperclip_roster)
    with patch.object(agent_registry, "_paperclip_agents", AsyncMock(side_effect=lambda ws: copy.deepcopy(state))):
        yield state


def _live_ids(roster):
    return sorted(a["id"] for a in roster if a["status"] != "terminated")


@pytest.mark.asyncio
async def test_seed_preserves_ids(tasks_table, paperclip):
    report = await agent_registry.seed_from_paperclip(WS)
    assert report == (7, 0, 1, 0)
    assert sorted(r["agentId"] for r in await repo.list_agents(WS)) == _live_ids(paperclip)
    for agent_id in _live_ids(paperclip):
        assert await repo.owner_of(agent_id) == WS


@pytest.mark.asyncio
async def test_seed_idempotent(tasks_table, paperclip):
    await agent_registry.seed_from_paperclip(WS)
    before = await repo.list_agents(WS)
    assert await agent_registry.seed_from_paperclip(WS) == (0, 0, 1, 0)
    assert await repo.list_agents(WS) == before


@pytest.mark.asyncio
async def test_seed_dry_run_writes_nothing(tasks_table, paperclip):
    assert await agent_registry.seed_from_paperclip(WS, dry_run=True) == (7, 0, 1, 0)
    assert await repo.list_agents(WS) == []


@pytest.mark.asyncio
async def test_verify_clean_after_seed(tasks_table, paperclip):
    await agent_registry.seed_from_paperclip(WS)
    assert await agent_registry.verify(WS) == []


@pytest.mark.asyncio
async def test_verify_diff_kinds(tasks_table, paperclip):
    await agent_registry.seed_from_paperclip(WS)
    missing = {**paperclip[1], "id": "33333333-3333-4333-8333-333333333333"}
    extra = paperclip.pop(2)
    paperclip.append(missing)
    paperclip[0]["name"] = "Renamed"
    paperclip[1]["companyId"] = "co-other"
    paperclip[3]["status"] = "running"  # run state, not identity: no diff
    found = await agent_registry.verify(WS)
    assert sorted(found) == sorted(
        [
            Diff("missing_in_registry", missing["id"]),
            Diff("extra_in_registry", extra["id"]),
            Diff("field", paperclip[0]["id"], "name"),
            Diff("field", paperclip[1]["id"], "companyId"),
        ]
    )


@pytest.mark.asyncio
async def test_apply_soft_deletes_agents_terminated_or_gone_in_paperclip(tasks_table, paperclip):
    """Paperclip's own writes (a rejected hire, a clean-slate reset) heal on the next --apply."""
    await agent_registry.seed_from_paperclip(WS)
    terminated, gone = paperclip[0], paperclip.pop(1)
    terminated["status"] = "terminated"
    assert await agent_registry.verify(WS) == [
        Diff("extra_in_registry", i) for i in sorted([terminated["id"], gone["id"]])
    ]
    assert (await agent_registry.seed_from_paperclip(WS, dry_run=True)).deleted == 2
    assert (await agent_registry.seed_from_paperclip(WS)).deleted == 2
    assert {(await repo.get(WS, i))["status"] for i in (terminated["id"], gone["id"])} == {"deleted"}
    assert await agent_registry.verify(WS) == []
    assert (await agent_registry.seed_from_paperclip(WS)).deleted == 0


@pytest.mark.asyncio
async def test_apply_never_deletes_when_the_workspace_has_no_active_company(tasks_table, paperclip):
    """Missing / provisioning / failed workspace row: no company, so the Paperclip list is unknown, not empty."""
    await agent_registry.seed_from_paperclip(WS)
    before = await repo.list_agents(WS)
    with patch.object(agent_registry, "_paperclip_agents", AsyncMock(return_value=None)):
        assert await agent_registry.seed_from_paperclip(WS) == (0, 0, 0, 0)
    assert await repo.list_agents(WS) == before


@pytest.mark.asyncio
async def test_paperclip_agents_is_none_without_an_active_company():
    with (
        patch("core.services.paperclip_openclaw_pairing.resolve_company_id_for_owner", AsyncMock(return_value=None)),
        patch("core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner") as fetch,
    ):
        assert await agent_registry._paperclip_agents(WS) is None
    fetch.assert_not_called()


async def _cli(*argv):
    from scripts import backfill_agent_registry as script

    pc_repo = MagicMock()
    pc_repo.list_active_workspace_members = AsyncMock(return_value={WS: set()})
    with (
        patch.object(script, "PaperclipRepo", return_value=pc_repo),
    ):
        return await script.main(list(argv))


@pytest.mark.asyncio
async def test_verify_output_has_no_values(tasks_table, paperclip, capsys):
    await agent_registry.seed_from_paperclip(WS)
    paperclip[0]["name"] = "Secret Name"
    paperclip[1]["capabilities"] = "Secret persona"
    assert await _cli("--all", "--verify") == 1
    out = capsys.readouterr().out
    assert "field=name" in out and "field=persona" in out
    assert "Secret" not in out and "Friday" not in out and "persona text" not in out


@pytest.mark.asyncio
async def test_cli_verify_exit_code(tasks_table, paperclip):
    assert await _cli("--all", "--verify") == 1  # nothing seeded: every agent missing
    assert await _cli("--all") == 0  # dry run
    assert await repo.list_agents(WS) == []
    assert await _cli("--all", "--apply") == 0
    assert await _cli("--all", "--verify") == 0


async def test_cli_refuses_prod():
    from scripts import backfill_agent_registry as script

    with patch.object(settings, "ENVIRONMENT", "prod"):
        assert await script.main(["--all", "--apply"]) == 2


@pytest.mark.asyncio
async def test_cli_allow_prod_lifts_only_the_prod_refusal(tasks_table, paperclip):
    from scripts import backfill_agent_registry as script

    with patch.object(settings, "ENVIRONMENT", "prod"):
        assert await script.main(["--all", "--apply", "--allow-prod"]) == 0
        assert await script.main(["--all", "--verify", "--allow-prod"]) == 0
