"""Unit tests for the first-run brief staging service.

Mirrors tests/unit/services/test_friday_identity.py: Workspace is a
MagicMock; we assert exact workspace-relative paths. Staging itself is
unconditional now (the company-scoped onboarding claim in
paperclip_repo.py is the one-shot marker, not BRIEF.md's existence).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services.onboarding_brief import (
    derive_goal_title,
    project_name_for,
    render_brief_md,
    render_first_run_bootstrap,
    seed_goal_and_project,
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
    md = render_first_run_bootstrap(first_name="Max", goal_id="goal-1", project_id="proj-1")
    assert "no idea yet" in md
    # The pick happens BEFORE the research step, not after.
    assert md.index("picking one is your first job") < md.index("Understand the business")


def test_render_first_run_bootstrap_researches_before_proposing_tasks():
    md = render_first_run_bootstrap(first_name="Max", goal_id="goal-1", project_id="proj-1")
    assert "BRIEF.md" in md
    assert "Max" in md
    # New ordering (spec §4.D): understand the business + write the docs FIRST,
    # then propose the three next-move tasks as the session's closing output.
    # Anchor on step 7's unique "three best next" (the full phrase wraps a line).
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


def test_bootstrap_names_before_researching():
    md = render_first_run_bootstrap(first_name="Max", goal_id="goal-1", project_id="proj-1")
    assert md.index("Name the company") < md.index("web_search")


def test_bootstrap_carries_the_ids():
    md = render_first_run_bootstrap(first_name="Max", goal_id="goal-1", project_id="proj-1")
    assert "goal-1" in md
    assert "proj-1" in md


def test_bootstrap_files_tasks_into_the_project():
    md = render_first_run_bootstrap(first_name="Max", goal_id="goal-1", project_id="proj-1")
    assert "projectId" in md


def test_bootstrap_renders_with_braces_intact():
    """JSON braces in the template must survive str.format()."""
    md = render_first_run_bootstrap(first_name="Max", goal_id="goal-1", project_id="proj-1")
    assert '"projectId"' in md
    assert "{{" not in md


def test_goal_title_new_path_uses_their_words():
    assert (
        derive_goal_title(path="new", user_input="A social media agency for restaurants")
        == "A social media agency for restaurants"
    )


def test_goal_title_new_path_truncates_long_input():
    title = derive_goal_title(path="new", user_input="x" * 300)
    assert len(title) <= 120
    assert title.endswith("…")


def test_goal_title_grow_path_is_a_mission_not_a_url():
    assert derive_goal_title(path="grow", user_input="https://www.ferrosocial.com/pricing?x=1") == (
        "Grow ferrosocial.com"
    )


def test_goal_title_grow_path_caps_a_pasted_paragraph():
    # The website field is free text (up to 2000 chars); a user pasting prose
    # instead of a URL must not produce a ~2000-char title that Paperclip
    # could reject, stalling the client in an identical-input retry loop.
    title = derive_goal_title(path="grow", user_input="x" * 300)
    assert len(title) <= 120
    assert title.endswith("…")


def test_goal_title_no_idea_path_describes_the_actual_situation():
    assert derive_goal_title(path="new", user_input=None) == "Find the right business to build"


def test_project_name_is_never_a_placeholder():
    assert project_name_for("Max") == "Max's Company"


@pytest.mark.asyncio
async def test_seed_creates_goal_then_project_linked_to_it():
    admin = MagicMock()
    admin.create_goal = AsyncMock(return_value={"id": "goal-1"})
    admin.create_project = AsyncMock(return_value={"id": "proj-1"})

    goal_id, project_id = await seed_goal_and_project(
        admin=admin,
        session_cookie="cookie",
        company_id="comp-1",
        friday_agent_id="friday-uuid",
        goal_title="A social media agency",
        project_name="Max's Company",
    )

    assert (goal_id, project_id) == ("goal-1", "proj-1")

    goal_body = admin.create_goal.await_args.kwargs["body"]
    assert goal_body["level"] == "company"
    assert goal_body["status"] == "active"
    assert "parentId" not in goal_body

    project_body = admin.create_project.await_args.kwargs["body"]
    # Regression this guards: a task created with projectId = the onboarding
    # project must resolve goalId to the onboarding goal. Paperclip mirrors
    # goalIds[0] into the legacy projects.goalId column that
    # getProjectDefaultGoalId reads, and resolveIssueGoalId returns
    # projectGoalId ?? null for any issue with a project -- it does NOT fall
    # through to the company default. So if this ever becomes goalId (singular)
    # instead of goalIds (plural, list), or drops goal_id from the list, every
    # task filed into the project goes silently unattributed. This is the
    # highest level our code can assert the fix at -- Paperclip's own mirroring
    # of goalIds[0] -> goalId is upstream behavior, not ours to unit test here.
    assert project_body["goalIds"] == ["goal-1"]
    assert "targetDate" not in project_body


@pytest.mark.asyncio
async def test_seed_rolls_back_the_goal_when_project_create_fails():
    admin = MagicMock()
    admin.create_goal = AsyncMock(return_value={"id": "goal-1"})
    admin.create_project = AsyncMock(side_effect=RuntimeError("upstream down"))
    admin.delete_goal = AsyncMock()

    with pytest.raises(RuntimeError):
        await seed_goal_and_project(
            admin=admin,
            session_cookie="cookie",
            company_id="comp-1",
            friday_agent_id="friday-uuid",
            goal_title="A social media agency",
            project_name="Max's Company",
        )

    admin.delete_goal.assert_awaited_once_with(session_cookie="cookie", goal_id="goal-1")


@pytest.mark.asyncio
async def test_seed_goal_cleanup_failure_does_not_mask_the_original_error():
    """If delete_goal itself raises during rollback, that cleanup failure
    must not REPLACE the original create_project error -- the router maps
    (PaperclipApiError, httpx.HTTPError, RuntimeError) to a retryable 409,
    and an unrelated cleanup exception could fall outside that set and
    surface as a raw 500 instead, reclassifying a retryable failure."""
    admin = MagicMock()
    admin.create_goal = AsyncMock(return_value={"id": "goal-1"})
    admin.create_project = AsyncMock(side_effect=RuntimeError("upstream down"))
    admin.delete_goal = AsyncMock(side_effect=Exception("goal delete also failed"))

    with pytest.raises(RuntimeError, match="upstream down"):
        await seed_goal_and_project(
            admin=admin,
            session_cookie="cookie",
            company_id="comp-1",
            friday_agent_id="friday-uuid",
            goal_title="A social media agency",
            project_name="Max's Company",
        )

    admin.delete_goal.assert_awaited_once_with(session_cookie="cookie", goal_id="goal-1")


def test_staging_overwrites_a_partial_prior_attempt():
    """The claim decides one-shot-ness now, so staging always writes both.

    A prior attempt that staged files and then failed leaves BRIEF.md behind.
    Returning early on that would strand the goal and project this run just
    seeded, so we overwrite instead.
    """
    ws = MagicMock()

    stage_first_run_files(
        workspace=ws,
        owner_id="user_abc",
        friday_agent_id="friday-uuid",
        brief_md="brief",
        bootstrap_md="bootstrap",
    )

    assert ws.write_file.call_count == 2
    ws.write_file_if_missing.assert_not_called()
