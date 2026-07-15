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


def test_seeded_ids_identify_renamed_atlas():
    renamed = _bare_atlas() | {"name": "Scout"}
    patches = compute_agent_patches([_bare_friday(), renamed], seeded_agent_ids=["agent-friday", "agent-atlas"])
    assert patches["agent-atlas"]["title"] == ATLAS_TITLE
    assert patches["agent-atlas"]["reportsTo"] == "agent-friday"


def test_seeded_ids_protect_lookalike_marketplace_hire():
    lookalike = {"id": "agent-hire", "name": "Atlas", "role": "general"}
    renamed_real = _bare_atlas() | {"name": "Scout"}
    patches = compute_agent_patches(
        [_bare_friday(), lookalike, renamed_real],
        seeded_agent_ids=["agent-friday", "agent-atlas"],
    )
    assert "agent-hire" not in patches
    assert patches["agent-atlas"]["title"] == ATLAS_TITLE


def test_empty_seeded_ids_falls_back_to_name_match():
    patches = compute_agent_patches([_bare_friday(), _bare_atlas()], seeded_agent_ids=[])
    assert patches["agent-atlas"]["title"] == ATLAS_TITLE


def test_seeded_marker_present_but_atlas_gone_never_name_matches():
    """A row WITH a seeded marker whose non-ceo seeded agent is absent from
    the roster must not fall back to name matching — a lookalike hire named
    'Atlas' would get stamped with recruiter metadata."""
    lookalike = {"id": "agent-hire", "name": "Atlas", "role": "general"}
    patches = compute_agent_patches(
        [_bare_friday(), lookalike],
        seeded_agent_ids=["agent-friday", "agent-atlas"],  # atlas id not in roster
    )
    assert "agent-hire" not in patches
    assert set(patches) == {"agent-friday"}


def test_seeded_marker_gates_friday_selection_too():
    """Teams allows extra role=ceo agents; when the marker exists, a
    non-seeded ceo ordered first must not be stamped as Friday, and Atlas's
    reportsTo must point at the real seeded Friday."""
    impostor = {"id": "agent-user-ceo", "name": "Boss", "role": "ceo"}
    patches = compute_agent_patches(
        [impostor, _bare_friday(), _bare_atlas()],
        seeded_agent_ids=["agent-friday", "agent-atlas"],
    )
    assert "agent-user-ceo" not in patches
    assert patches["agent-friday"]["title"] == FRIDAY_TITLE
    assert patches["agent-atlas"]["reportsTo"] == "agent-friday"


def test_seeded_marker_present_but_friday_gone_skips_friday():
    impostor = {"id": "agent-user-ceo", "name": "Boss", "role": "ceo"}
    patches = compute_agent_patches(
        [impostor, _bare_atlas()],
        seeded_agent_ids=["agent-friday", "agent-atlas"],  # friday id absent
    )
    assert "agent-user-ceo" not in patches
    # Atlas still patched, but unparented — no impostor reportsTo.
    assert "reportsTo" not in patches["agent-atlas"]


def test_role_edited_friday_never_gets_atlas_metadata():
    """Owners can re-role the seeded Friday away from ceo (chat agents.update
    mirrors role edits). Seed identity is the marker's ORDER, so a demoted
    Friday must not be picked as 'the seeded non-ceo id' and stamped with
    recruiter metadata — and its own CEO stamp is skipped too (the owner's
    role edit outranks the backfill)."""
    demoted_friday = _bare_friday() | {"role": "engineer"}
    patches = compute_agent_patches(
        [demoted_friday, _bare_atlas()],
        seeded_agent_ids=["agent-friday", "agent-atlas"],
    )
    assert "agent-friday" not in patches
    assert patches["agent-atlas"]["title"] == ATLAS_TITLE
    # Atlas still parents under the seeded Friday id.
    assert patches["agent-atlas"]["reportsTo"] == "agent-friday"


def test_lone_seeded_id_disambiguated_by_role_and_name():
    # Lone seed with role ceo → Friday.
    patches = compute_agent_patches([_bare_friday()], seeded_agent_ids=["agent-friday"])
    assert patches["agent-friday"]["title"] == FRIDAY_TITLE
    # Lone seed named Atlas → Atlas (Friday mint failed at provision).
    patches = compute_agent_patches([_bare_atlas()], seeded_agent_ids=["agent-atlas"])
    assert patches["agent-atlas"]["title"] == ATLAS_TITLE
    assert "reportsTo" not in patches["agent-atlas"]


def test_lone_ambiguous_seed_is_skipped():
    """A lone seeded id that is neither ceo-roled nor named Atlas (re-roled
    AND renamed) is ambiguous — skip rather than guess."""
    mystery = {"id": "agent-friday", "name": "Jarvis", "role": "engineer"}
    assert compute_agent_patches([mystery], seeded_agent_ids=["agent-friday"]) == {}


def test_role_edited_atlas_keeps_owner_customization():
    """Symmetric to the Friday guard: a seeded Atlas the owner re-roled
    (e.g. promoted) is never stamped with recruiter metadata."""
    promoted_atlas = _bare_atlas() | {"role": "engineer"}
    patches = compute_agent_patches(
        [_bare_friday(), promoted_atlas],
        seeded_agent_ids=["agent-friday", "agent-atlas"],
    )
    assert "agent-atlas" not in patches
    assert patches["agent-friday"]["title"] == FRIDAY_TITLE


def test_lone_seed_with_conflicting_signals_is_skipped():
    """A lone seed that is ceo-roled AND named Atlas (promoted Atlas or a
    renamed Friday — indistinguishable) is skipped, never classified."""
    conflicted = {"id": "agent-lone", "name": "Atlas", "role": "ceo"}
    assert compute_agent_patches([conflicted], seeded_agent_ids=["agent-lone"]) == {}
