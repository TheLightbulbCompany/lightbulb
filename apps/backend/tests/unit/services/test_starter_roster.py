"""Starter-roster workspace role contracts."""

from unittest.mock import MagicMock

import pytest

from core.services.starter_roster import pre_stage_starter_roster


def _workspace(existing: dict[str, str] | None = None):
    workspace = MagicMock()
    workspace.files = dict(existing or {})

    def write_if_missing(_owner_id: str, path: str, content: str) -> bool:
        if path in workspace.files:
            return False
        workspace.files[path] = content
        return True

    workspace.write_file_if_missing.side_effect = write_if_missing
    return workspace


@pytest.mark.asyncio
async def test_stages_role_contracts_by_stable_id_after_arbitrary_renames():
    starter_ids = {
        "ceo": "agent-1",
        "engineering": "agent-2",
        "finance": "agent-3",
        "growth": "agent-4",
        "analytics": "agent-5",
        "customer_operations": "agent-6",
    }
    agents = [
        {"id": agent_id, "name": f"Renamed {index}", "role": "general"}
        for index, agent_id in enumerate(starter_ids.values(), start=1)
    ]
    workspace = _workspace()

    await pre_stage_starter_roster(
        owner_id="owner-1",
        paperclip_agents=agents,
        starter_agent_ids=starter_ids,
        workspace=workspace,
    )

    assert set(workspace.files) == {f"workspace-{agent_id}/AGENTS.md" for agent_id in starter_ids.values()}
    assert "prioritize" in workspace.files["workspace-agent-1/AGENTS.md"].lower()
    assert "build" in workspace.files["workspace-agent-2/AGENTS.md"].lower()
    assert "runway" in workspace.files["workspace-agent-3/AGENTS.md"].lower()
    assert "positioning" in workspace.files["workspace-agent-4/AGENTS.md"].lower()
    assert "attribution" in workspace.files["workspace-agent-5/AGENTS.md"].lower()
    assert "refund" in workspace.files["workspace-agent-6/AGENTS.md"].lower()
    for content in workspace.files.values():
        assert "`lightbulb` skill" in content
        assert "`paperclip` skill" in content
        assert "`~/.openclaw/lightbulb-facts.json`" in content


@pytest.mark.asyncio
async def test_skips_a_starter_id_missing_from_the_live_roster():
    workspace = _workspace()

    await pre_stage_starter_roster(
        owner_id="owner-1",
        paperclip_agents=[{"id": "agent-ceo", "name": "Anything"}],
        starter_agent_ids={"ceo": "agent-ceo", "finance": "deleted-finance"},
        workspace=workspace,
    )

    assert set(workspace.files) == {"workspace-agent-ceo/AGENTS.md"}


@pytest.mark.asyncio
async def test_never_overwrites_an_edited_role_contract():
    path = "workspace-agent-ceo/AGENTS.md"
    workspace = _workspace({path: "owner edits"})
    kwargs = {
        "owner_id": "owner-1",
        "paperclip_agents": [{"id": "agent-ceo", "name": "Renamed CEO"}],
        "starter_agent_ids": {"ceo": "agent-ceo"},
        "workspace": workspace,
    }

    await pre_stage_starter_roster(**kwargs)
    await pre_stage_starter_roster(**kwargs)

    assert workspace.files[path] == "owner edits"
    assert workspace.write_file_if_missing.call_count == 2
