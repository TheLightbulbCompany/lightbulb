"""Unit tests for the first-run brief staging service.

Mirrors tests/unit/services/test_friday_identity.py: Workspace is a
MagicMock; we assert exact workspace-relative paths and one-shot
semantics (BRIEF.md write-if-missing decides whether BOOTSTRAP.md is
overwritten and whether the caller should create the Set-up issue).
"""

from unittest.mock import MagicMock

from core.services.onboarding_brief import (
    render_brief_md,
    render_first_run_bootstrap,
    setup_issue_payload,
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


def test_render_first_run_bootstrap_orders_board_first():
    md = render_first_run_bootstrap(first_name="Max")
    assert "BRIEF.md" in md
    # Board-first ordering: claiming the Set-up issue must be instructed
    # before deep research (spec §4.D). Anchor on the deep-research step's
    # own text, not the word "research" (step 2's comment contains it too).
    assert md.index("check out") < md.index("Deep research")
    # Missing-issue tolerance: step 2 tells Friday to create the Set-up
    # issue herself when issue creation failed server-side.
    assert "create it yourself" in md
    assert "Max" in md


def test_setup_issue_payload_assigns_friday_camelcase():
    payload = setup_issue_payload(label="Acme Construction", friday_agent_id=FRIDAY_ID)
    assert payload["title"] == "Set up Acme Construction"
    assert payload["assigneeAgentId"] == FRIDAY_ID
    assert "description" in payload


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
