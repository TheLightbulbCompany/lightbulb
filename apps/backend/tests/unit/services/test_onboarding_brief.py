"""Unit tests for the first-run brief staging service.

Mirrors tests/unit/services/test_friday_identity.py: Workspace is a
MagicMock; we assert exact workspace-relative paths and one-shot
semantics (BRIEF.md write-if-missing decides whether BOOTSTRAP.md is
overwritten).
"""

import pytest
from unittest.mock import MagicMock

from core.services.onboarding_brief import (
    render_brief_md,
    render_first_run_bootstrap,
    stage_first_run_files,
)

FRIDAY_ID = "agent-uuid-123"
OWNER = "user_abc"


def test_render_brief_md_contains_all_fields():
    md = render_brief_md(
        path="grow",
        user_input="https://acmeconstruction.com",
        detail="residential builds in Austin",
        display_name="Max",
        email="max@acme.com",
    )
    assert "# BRIEF.md" in md
    assert "grow" in md
    assert "https://acmeconstruction.com" in md
    assert "residential builds in Austin" in md
    assert "Max" in md
    assert "max@acme.com" in md


def test_render_brief_md_omits_detail_line_when_none():
    md = render_brief_md(
        path="new",
        user_input="a social media agency for restaurants",
        detail=None,
        display_name="Ben",
        email="ben@x.com",
    )
    assert "Extra detail" not in md


def test_render_brief_md_no_idea_tells_friday_to_pick_one():
    """The "you pick" button sends no input; the absence is the instruction."""
    md = render_brief_md(
        path="new",
        user_input=None,
        detail=None,
        display_name="Prabuddha",
        email="prabuddha@gmail.com",
    )
    assert "job zero" in md
    # She must know she has a lead (the email) and a way to follow it.
    assert "prabuddha@gmail.com" in md
    assert "web_search" in md
    # Committing beats stalling — the run has to complete if they tab away.
    assert "do not stall" in md
    # Never render a Python None into the brief.
    assert "None" not in md


def test_render_first_run_bootstrap_tells_friday_to_pick_when_no_idea():
    md = render_first_run_bootstrap(first_name="Max")
    assert "no idea yet" in md
    # The pick happens BEFORE the research step, not after.
    assert md.index("picking one is your first job") < md.index("Understand the business")


def test_render_first_run_bootstrap_researches_before_proposing_tasks():
    md = render_first_run_bootstrap(first_name="Max")
    assert "BRIEF.md" in md
    assert "Max" in md
    # New ordering (spec §4.D): understand the business + write the docs FIRST,
    # then propose the three next-move tasks as the session's closing output.
    # Anchor on step 5's unique "three best next" (the full phrase wraps a line).
    assert md.index("MISSION.md") < md.index("three best next")
    # Task proposals are path-aware: a website-first move for a NEW business,
    # growth moves for a GROW one (they already have a site).
    assert "NEW business" in md
    assert "GROW" in md
    assert "Build [company] website" in md  # the NEW-path first task
    assert "GROWTH moves" in md  # the GROW-path branch
    assert "building a website from scratch" in md  # grow must NOT do that
    # The proposed tasks are for later separate-session runs, not executed now.
    assert "do NOT start them" in md
    # No Set-up-issue claim/seed in the new flow (the board fills at the end).
    assert "check out the issue" not in md


def test_stage_first_run_files_fresh_writes_brief_then_bootstrap():
    ws = MagicMock()
    ws.write_file_if_missing = MagicMock(return_value=True)
    ws.write_file = MagicMock()

    wrote = stage_first_run_files(
        workspace=ws,
        owner_id=OWNER,
        friday_agent_id=FRIDAY_ID,
        brief_md="brief",
        bootstrap_md="bootstrap",
    )

    assert wrote is True
    ws.write_file_if_missing.assert_called_once_with(OWNER, f"workspaces/{FRIDAY_ID}/BRIEF.md", "brief")
    ws.write_file.assert_called_once_with(OWNER, f"workspaces/{FRIDAY_ID}/BOOTSTRAP.md", "bootstrap")


def test_stage_first_run_files_replay_touches_nothing_else():
    ws = MagicMock()
    ws.write_file_if_missing = MagicMock(return_value=False)
    ws.write_file = MagicMock()

    wrote = stage_first_run_files(
        workspace=ws,
        owner_id=OWNER,
        friday_agent_id=FRIDAY_ID,
        brief_md="brief",
        bootstrap_md="bootstrap",
    )

    assert wrote is False
    # Replay must not clobber a BOOTSTRAP the container may have consumed.
    ws.write_file.assert_not_called()


def test_stage_first_run_files_rolls_back_brief_when_bootstrap_write_fails():
    """If BRIEF is claimed but the BOOTSTRAP write raises, the marker is
    deleted and the error propagates — so a retry re-stages both files
    instead of short-circuiting to a half-staged workspace."""
    ws = MagicMock()
    ws.write_file_if_missing = MagicMock(return_value=True)
    ws.write_file = MagicMock(side_effect=RuntimeError("efs boom"))
    ws.delete_file = MagicMock()

    with pytest.raises(RuntimeError, match="efs boom"):
        stage_first_run_files(
            workspace=ws,
            owner_id=OWNER,
            friday_agent_id=FRIDAY_ID,
            brief_md="brief",
            bootstrap_md="bootstrap",
        )

    ws.delete_file.assert_called_once_with(OWNER, f"workspaces/{FRIDAY_ID}/BRIEF.md")
