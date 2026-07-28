"""Fulfilment half of the onboarding funnel: reconcile_pending_brief.

``POST /onboarding/brief`` records the answer and returns (see
tests/unit/routers/test_onboarding.py). THIS is what turns that answer into a
funnel claim, a seeded goal + project, a staged BRIEF.md and Friday's kickoff
-- driven off the /container/status poll, so it runs repeatedly and must be
safe to run when there is nothing to do.

The property that matters most here, and the reason the split exists: a
failure ANYWHERE must leave the pending item in place. It is the only record
of what the user typed. The old fused endpoint held it in the browser, so a
closed tab lost it.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import core.containers as containers_mod
import core.repositories.paperclip_repo as repo_mod
import core.services.bootstrap_dispatcher as dispatcher_mod
import core.services.clerk_admin as clerk_mod
import core.services.paperclip_admin_client as admin_mod
import core.services.paperclip_user_session as session_mod
from core.repositories.paperclip_repo import FunnelClaimResult
from core.services.onboarding_brief import reconcile_pending_brief

FRIDAY = {"id": "friday-uuid", "role": "ceo", "name": "Friday"}
ATLAS = {"id": "atlas-uuid", "role": "recruiter", "name": "Atlas"}

PENDING = {
    "user_id": "__pending_brief__:user_abc",
    "kind": "pending_brief",
    "owner_id": "user_abc",
    "path": "grow",
    "input": "https://acme.com",
    "brief_text": "work on acme.com",
    "submitted_by": "member_zed",
}


# ``user_id`` is deliberately DIFFERENT from the owner_id to catch any write
# that keys on the owner id instead of the row's PK -- for an org those are
# genuinely different values (see PaperclipRepo.resolve_owner_row).
def _owner_row(**overrides):
    base = dict(
        user_id="owner-row-user",
        company_id="comp-1",
        status="active",
        onboarding_generation="01GEN",
        onboarding_goal_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def env(monkeypatch):
    repo = MagicMock()
    repo.get_pending_brief = AsyncMock(return_value=dict(PENDING))
    repo.resolve_owner_row = AsyncMock(return_value=_owner_row())
    repo.claim_funnel_submission = AsyncMock(return_value=FunnelClaimResult.WON)
    repo.record_onboarding_ids = AsyncMock()
    repo.revert_funnel_submission = AsyncMock()
    repo.delete_pending_brief = AsyncMock()
    monkeypatch.setattr(repo_mod, "PaperclipRepo", lambda **kwargs: repo)

    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=[FRIDAY, ATLAS])
    admin.create_goal = AsyncMock(return_value={"id": "goal-1"})
    admin.create_project = AsyncMock(return_value={"id": "proj-1"})
    admin.delete_goal = AsyncMock()
    admin.delete_project = AsyncMock()
    monkeypatch.setattr(admin_mod, "shared_admin_client", lambda: admin)

    monkeypatch.setattr(session_mod, "get_user_session_cookie", AsyncMock(return_value="cookie"))
    monkeypatch.setattr(clerk_mod, "get_primary_email", AsyncMock(return_value="max@acme.com"))

    ws = MagicMock()
    ws.write_file = MagicMock()
    monkeypatch.setattr(containers_mod, "get_workspace", lambda: ws)

    dispatch = AsyncMock()
    monkeypatch.setattr(dispatcher_mod, "maybe_dispatch_bootstrap", dispatch)

    return SimpleNamespace(repo=repo, admin=admin, ws=ws, dispatch=dispatch)


# --- Nothing to do -----------------------------------------------------------


async def test_no_pending_brief_is_a_cheap_noop(env):
    """The overwhelmingly common case: every /container/status poll for an
    owner who has no outstanding answer."""
    env.repo.get_pending_brief.return_value = None

    await reconcile_pending_brief("user_abc")

    env.repo.resolve_owner_row.assert_not_awaited()
    env.repo.claim_funnel_submission.assert_not_awaited()


@pytest.mark.parametrize(
    "row",
    [
        None,  # provisioning hasn't written the row yet -- the first-run case
        _owner_row(onboarding_generation=None),  # predates the generation fence
        _owner_row(status="seeding"),  # mid-provision
        _owner_row(status="failed"),  # seed failed; a later reconcile retries
    ],
    ids=["no-row", "no-generation", "seeding", "failed"],
)
async def test_workspace_not_ready_keeps_the_brief_for_the_next_poll(env, row):
    env.repo.resolve_owner_row.return_value = row

    await reconcile_pending_brief("user_abc")

    env.repo.claim_funnel_submission.assert_not_awaited()
    env.repo.delete_pending_brief.assert_not_awaited()


async def test_friday_not_seeded_yet_keeps_the_brief(env):
    env.admin.list_agents.return_value = [ATLAS]

    await reconcile_pending_brief("user_abc")

    env.repo.claim_funnel_submission.assert_not_awaited()
    env.repo.delete_pending_brief.assert_not_awaited()


async def test_paperclip_unavailable_keeps_the_brief_and_claims_nothing(env):
    """Reads happen BEFORE the claim, so a Paperclip blip costs nothing --
    there is no claim to unwind."""
    env.admin.list_agents.side_effect = RuntimeError("paperclip down")

    await reconcile_pending_brief("user_abc")

    env.repo.claim_funnel_submission.assert_not_awaited()
    env.repo.revert_funnel_submission.assert_not_awaited()
    env.repo.delete_pending_brief.assert_not_awaited()


# --- The happy path ----------------------------------------------------------


async def test_drain_claims_seeds_stages_then_retires_the_pending_item(env):
    await reconcile_pending_brief("user_abc")

    # Claim is fenced on the ROW's PK + generation, and carries the submitting
    # MEMBER (not the row's PK) so the kickoff addresses the right session.
    env.repo.claim_funnel_submission.assert_awaited_once_with(
        "owner-row-user",
        onboarding_generation="01GEN",
        submitted_by="member_zed",
        owner_id="user_abc",
        brief_text="work on acme.com",
    )
    env.admin.create_goal.assert_awaited_once()
    env.admin.create_project.assert_awaited_once()
    env.repo.record_onboarding_ids.assert_awaited_once_with(
        "owner-row-user",
        onboarding_generation="01GEN",
        goal_id="goal-1",
        project_id="proj-1",
        owner_id="user_abc",
    )
    brief_call = env.ws.write_file.call_args_list[0]
    assert brief_call.args[0] == "user_abc"  # owner id -- the workspace path
    assert brief_call.args[1] == "workspace-friday-uuid/BRIEF.md"
    assert "acme.com" in brief_call.args[2]
    # Only now is the answer retired, and only now does Friday get woken.
    env.repo.delete_pending_brief.assert_awaited_once_with("user_abc")
    env.dispatch.assert_awaited_once_with("user_abc")


async def test_session_is_minted_for_the_owner_row_not_the_submitter(env, monkeypatch):
    """An org member who submitted the funnel may have no Paperclip row of
    their own yet; the row we resolved is guaranteed to."""
    mint = AsyncMock(return_value="cookie")
    monkeypatch.setattr(session_mod, "get_user_session_cookie", mint)

    await reconcile_pending_brief("user_abc")

    assert mint.await_args.kwargs["user_id"] == "owner-row-user"


async def test_no_board_seeding_on_drain(env):
    """The board fills at the END of Friday's research session with the
    next-move tasks she proposes -- not here."""
    await reconcile_pending_brief("user_abc")

    env.admin.create_issue.assert_not_called()


# --- Losing the claim --------------------------------------------------------


async def test_stale_generation_keeps_the_brief_and_reverts_nothing(env):
    """Reprovisioned underneath us: our view is stale, not duplicate. The
    next poll re-reads the new generation and drains against that."""
    env.repo.claim_funnel_submission.return_value = FunnelClaimResult.STALE_GENERATION

    await reconcile_pending_brief("user_abc")

    env.admin.create_goal.assert_not_awaited()
    env.repo.delete_pending_brief.assert_not_awaited()
    env.repo.revert_funnel_submission.assert_not_awaited()


async def test_already_submitted_retires_the_brief_only_once_seeding_landed(env):
    """A peer drain won the claim AND finished (the row carries the ids), so
    this answer is fulfilled -- retire it."""
    env.repo.claim_funnel_submission.return_value = FunnelClaimResult.ALREADY_SUBMITTED
    env.repo.resolve_owner_row.return_value = _owner_row(onboarding_goal_id="goal-1")

    await reconcile_pending_brief("user_abc")

    env.repo.delete_pending_brief.assert_awaited_once_with("user_abc")
    env.admin.create_goal.assert_not_awaited()


async def test_already_submitted_keeps_the_brief_while_the_winner_is_still_running(env):
    """THE race that would silently eat a brief: a peer holds the claim but
    has not finished seeding. It can still fail and revert -- deleting now
    would leave the company with a submitted funnel, no goal/project, and no
    record of what the user asked for."""
    env.repo.claim_funnel_submission.return_value = FunnelClaimResult.ALREADY_SUBMITTED
    env.repo.resolve_owner_row.return_value = _owner_row(onboarding_goal_id=None)

    await reconcile_pending_brief("user_abc")

    env.repo.delete_pending_brief.assert_not_awaited()


# --- Failing after the claim -------------------------------------------------


async def test_seed_failure_reverts_the_claim_and_keeps_the_brief(env):
    env.admin.create_goal.side_effect = RuntimeError("upstream down")

    await reconcile_pending_brief("user_abc")

    env.repo.revert_funnel_submission.assert_awaited_once_with(
        "owner-row-user", onboarding_generation="01GEN", owner_id="user_abc"
    )
    env.ws.write_file.assert_not_called()
    env.repo.delete_pending_brief.assert_not_awaited()
    env.dispatch.assert_not_awaited()


async def test_seed_only_failure_does_not_attempt_rollback(env):
    """seed_goal_and_project already deletes the goal it created when the
    project create fails, so there is nothing left for us to roll back."""
    env.admin.create_goal.side_effect = RuntimeError("upstream down")

    await reconcile_pending_brief("user_abc")

    env.admin.delete_project.assert_not_awaited()
    env.admin.delete_goal.assert_not_awaited()


async def test_post_seed_failure_rolls_back_goal_and_project_then_reverts(env):
    """Once ids exist, a later failure (a DynamoDB throttle on record, an EFS
    error on stage) must delete both seeded rows -- otherwise the next drain
    wins a fresh claim and seeds a SECOND pair for the same company."""
    env.repo.record_onboarding_ids.side_effect = Exception("dynamodb throttled")

    await reconcile_pending_brief("user_abc")

    env.admin.delete_project.assert_awaited_once_with(session_cookie="cookie", project_id="proj-1")
    env.admin.delete_goal.assert_awaited_once_with(session_cookie="cookie", goal_id="goal-1")
    env.repo.revert_funnel_submission.assert_awaited_once_with(
        "owner-row-user", onboarding_generation="01GEN", owner_id="user_abc"
    )
    env.repo.delete_pending_brief.assert_not_awaited()


async def test_cleanup_failures_do_not_stop_the_remaining_cleanup(env):
    """A failed project delete must not skip the goal delete or the revert."""
    env.repo.record_onboarding_ids.side_effect = Exception("dynamodb throttled")
    env.admin.delete_project.side_effect = Exception("project delete also failed")

    await reconcile_pending_brief("user_abc")

    env.admin.delete_goal.assert_awaited_once_with(session_cookie="cookie", goal_id="goal-1")
    env.repo.revert_funnel_submission.assert_awaited_once()


@pytest.mark.parametrize(
    "failing",
    ["get_pending_brief", "resolve_owner_row", "claim_funnel_submission", "delete_pending_brief"],
)
async def test_never_raises_at_its_callers(env, failing):
    """Both call sites fire this as a background task -- an escaping exception
    would surface as an unretrieved-task error, not a retry. Parametrized over
    every unguarded await, including the ones AFTER the claim: those are the
    ones a single try around the reads would have missed."""
    getattr(env.repo, failing).side_effect = Exception("ddb down")

    await reconcile_pending_brief("user_abc")  # must not raise


async def test_cancellation_still_propagates(env):
    """BaseException handling exists for the cleanup, not to swallow a
    shutdown: CancelledError must keep propagating after the rollback."""
    env.repo.record_onboarding_ids.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await reconcile_pending_brief("user_abc")

    env.repo.revert_funnel_submission.assert_awaited_once()
