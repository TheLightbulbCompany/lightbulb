"""Tests for scripts.backfill_seed_team_roster."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services.paperclip_provisioning import (
    ATLAS_CAPABILITIES,
    ATLAS_TITLE,
    FRIDAY_CAPABILITIES,
    FRIDAY_TITLE,
)
from scripts.backfill_seed_team_roster import compute_agent_patches, process_company


def _bare_friday():
    return {"id": "agent-friday", "name": "Friday", "role": "ceo"}


def _bare_atlas():
    return {"id": "agent-atlas", "name": "Atlas", "role": "general"}


def test_bare_seed_team_gets_full_patches():
    patches = compute_agent_patches([_bare_friday(), _bare_atlas()])
    assert patches["agent-friday"] == {
        "title": FRIDAY_TITLE,
        "capabilities": FRIDAY_CAPABILITIES,
    }
    assert patches["agent-atlas"] == {
        "title": ATLAS_TITLE,
        "capabilities": ATLAS_CAPABILITIES,
        "reportsTo": "agent-friday",
    }


def test_customized_fields_are_never_overwritten():
    atlas = _bare_atlas() | {"capabilities": "Owner-tuned recruiter notes", "reportsTo": "agent-other"}
    friday = _bare_friday() | {"title": "Chief of Staff"}
    patches = compute_agent_patches([friday, atlas])
    # Only the still-empty fields are filled.
    assert patches["agent-friday"] == {"capabilities": FRIDAY_CAPABILITIES}
    assert patches["agent-atlas"] == {"title": ATLAS_TITLE}


def test_fully_populated_company_produces_no_patches():
    friday = _bare_friday() | {"title": "CEO", "capabilities": "x"}
    atlas = _bare_atlas() | {"title": "t", "capabilities": "c", "reportsTo": "agent-friday"}
    assert compute_agent_patches([friday, atlas]) == {}


def test_missing_friday_leaves_atlas_unparented():
    patches = compute_agent_patches([_bare_atlas()])
    assert patches["agent-atlas"] == {
        "title": ATLAS_TITLE,
        "capabilities": ATLAS_CAPABILITIES,
    }


def test_renamed_atlas_is_not_matched():
    renamed = _bare_atlas() | {"name": "Scout"}
    patches = compute_agent_patches([_bare_friday(), renamed])
    assert "agent-atlas" not in patches


def test_marketplace_general_agent_is_not_mistaken_for_atlas():
    hire = {"id": "agent-hire", "name": "SEO Pro", "role": "general"}
    patches = compute_agent_patches([_bare_friday(), hire, _bare_atlas()])
    assert "agent-hire" not in patches
    assert patches["agent-atlas"]["reportsTo"] == "agent-friday"


@pytest.mark.asyncio
async def test_process_company_dry_run_patches_nothing():
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=[_bare_friday(), _bare_atlas()])
    admin.patch_agent = AsyncMock()

    count = await process_company(admin, "cookie", "user_1", "co_1", apply=False)

    assert count == 2
    admin.patch_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_company_apply_patches_expected_bodies():
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=[_bare_friday(), _bare_atlas()])
    admin.patch_agent = AsyncMock()

    count = await process_company(admin, "cookie", "user_1", "co_1", apply=True)

    assert count == 2
    assert admin.patch_agent.await_count == 2
    by_id = {c.kwargs["agent_id"]: c.kwargs for c in admin.patch_agent.await_args_list}
    assert by_id["agent-atlas"]["body"]["reportsTo"] == "agent-friday"
    assert by_id["agent-atlas"]["session_cookie"] == "cookie"
    assert by_id["agent-friday"]["body"]["title"] == FRIDAY_TITLE
