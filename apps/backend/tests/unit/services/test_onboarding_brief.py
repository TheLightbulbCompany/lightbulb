"""Unit tests for the first-run brief staging service.

Mirrors tests/unit/services/test_friday_identity.py: Workspace is a
MagicMock; we assert exact workspace-relative paths. Staging itself is
unconditional now (the company-scoped onboarding claim in
paperclip_repo.py is the one-shot marker, not BRIEF.md's existence).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services.onboarding_brief import (
    FIRST_RUN_NO_BRIEF_MESSAGE,
    build_first_run_message,
    build_onboarding_brief_text,
    derive_goal_title,
    project_name_for,
    render_brief_md,
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


# --- First-run kickoff message: the user's own words as their first message ---


def test_brief_text_new_path_keeps_the_idea_verbatim():
    assert (
        build_onboarding_brief_text(path="new", user_input="a social media agency for small restaurants", detail=None)
        == "a social media agency for small restaurants"
    )


def test_brief_text_new_path_weaves_in_the_audience_when_present():
    """The optional "who's it for" (detail) is woven in naturally so the
    kickoff message carries the full ask."""
    text = build_onboarding_brief_text(path="new", user_input="a social media agency", detail="busy restaurant owners")
    assert text == "a social media agency — it's for busy restaurant owners"


def test_brief_text_grow_path_reads_as_work_on_the_existing_business():
    """The grow input is a website, not an idea -- "I'm starting {website}"
    would misread, so it becomes "work on {domain}"."""
    assert (
        build_onboarding_brief_text(path="grow", user_input="https://www.ferrosocial.com/pricing", detail=None)
        == "work on ferrosocial.com"
    )


def test_brief_text_is_none_on_the_you_pick_path():
    assert build_onboarding_brief_text(path="new", user_input=None, detail=None) is None
    # Blank/whitespace-only is the same signal.
    assert build_onboarding_brief_text(path="new", user_input="   ", detail=None) is None


def test_first_run_message_with_brief_leads_with_the_users_ask():
    brief = "a social media agency for small restaurants — it's for busy owners"
    msg = build_first_run_message(brief)
    assert msg.startswith(f"I'm starting {brief}.")
    # The research → write-up → three-next-moves ask is woven into the SAME
    # message (deterministic, not a hoped-for system-prompt behavior).
    assert "Dig into the business and market" in msg
    assert "three best next moves in the backlog" in msg
    # Tasks land in the backlog and are NOT auto-started — the user prioritizes.
    # Auto-starting them is what burned the trial cap / stormed in round 1 (#1426).
    assert "don't start any of them yet" in msg
    assert "I'm watching" in msg


def test_first_run_message_grow_brief_reads_naturally():
    msg = build_first_run_message("work on ferrosocial.com")
    assert msg.startswith("I'm starting work on ferrosocial.com.")


def test_first_run_message_no_brief_falls_back_to_the_you_pick_default():
    # startswith, not equality: every kickoff now carries the closing steps
    # (email the owner). The fallback body itself is unchanged — it just isn't
    # the whole message any more.
    assert build_first_run_message(None).startswith(FIRST_RUN_NO_BRIEF_MESSAGE)
    assert build_first_run_message("").startswith(FIRST_RUN_NO_BRIEF_MESSAGE)
    assert build_first_run_message("   ").startswith(FIRST_RUN_NO_BRIEF_MESSAGE)
    # The default still reads as the user and still names the same ask.
    assert "I'm not sure what to build yet" in FIRST_RUN_NO_BRIEF_MESSAGE
    assert "in the backlog" in FIRST_RUN_NO_BRIEF_MESSAGE
    assert "Don't start any of them yet" in FIRST_RUN_NO_BRIEF_MESSAGE
    assert "I'm watching" in FIRST_RUN_NO_BRIEF_MESSAGE


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
    )

    assert ws.write_file.call_count == 1
    ws.write_file_if_missing.assert_not_called()


# --- First-run kickoff: recommend integrations -----------------------------
#
# Same discovery problem as the email step below. The first prod run on this
# kickoff picked three next moves that each needed an integration already in
# providers.yaml (waitlist email, social posting, payments) and recommended
# none of them, because nothing in the kickoff asked and it never opened the
# isol8 skill. The catalog endpoint is handed over directly instead.

CATALOG = "/api/v1/platform-info"


@pytest.mark.parametrize("brief", ["a coffee subscription business", None])
def test_first_run_message_asks_for_integration_recommendations(brief):
    from core.services.onboarding_brief import build_first_run_message

    msg = build_first_run_message(brief)
    # Both kickoff paths: the "you pick" fallback is the same product moment.
    assert CATALOG in msg
    assert "integrations to connect" in msg


def test_kickoff_names_the_flags_that_gate_a_one_click_promise():
    """`requestable` / `byo_required` decide whether a row is one click, a BYO
    OAuth form, or an email to a human. Recommending one without reading them
    is how the owner gets promised a click that isn't there."""
    from core.services.onboarding_brief import build_first_run_message

    msg = build_first_run_message("a coffee subscription business")
    assert "requestable" in msg
    assert "byo_required" in msg
    # The user's own ask still leads; integrations come after the moves.
    assert msg.index("coffee subscription") < msg.index(CATALOG)


# --- First-run kickoff: name the company, then email the owner -------------
#
# Discovery is not enough on its own. A live prod run showed the agent asked to
# email its owner hunting for gog/himalaya/an OpenClaw email channel and
# concluding none was configured — it never opened the isol8 skill. The kickoff
# hands it the command directly so first contact does not depend on the agent
# making that leap.

SCRIPT = "/app/skills/isol8/scripts/isol8-owner-email.sh"


def test_first_run_message_instructs_naming_and_email():
    from core.services.onboarding_brief import build_first_run_message

    msg = build_first_run_message("a coffee subscription business")
    assert SCRIPT in msg
    assert "name" in msg.lower()
    # The user's own ask must still lead the message.
    assert msg.index("coffee subscription") < msg.index(SCRIPT)


def test_no_brief_path_also_instructs_email():
    """The 'you pick' path gets first contact too — it is the same product
    moment."""
    from core.services.onboarding_brief import build_first_run_message

    msg = build_first_run_message(None)
    assert SCRIPT in msg


def test_kickoff_says_the_name_becomes_the_address():
    """The name is frozen at the first send, so the agent must know that
    BEFORE it sends, not after."""
    from core.services.onboarding_brief import build_first_run_message

    msg = build_first_run_message("a coffee subscription business").lower()
    assert "email address" in msg or "address" in msg


def test_kickoff_names_the_company_before_the_board():
    """The issue prefix freezes when the first task is minted, and Paperclip
    only re-derives it on rename while the board is still empty — so the
    naming ask must come BEFORE the backlog ask on both kickoff paths, or
    every board keys its tasks off the signup placeholder (PRA-1 instead of
    GOO-1)."""
    from core.services.onboarding_brief import build_first_run_message

    for brief in ("a coffee subscription business", None):
        msg = build_first_run_message(brief)
        assert msg.index("company's name") < msg.index("three best next moves")
