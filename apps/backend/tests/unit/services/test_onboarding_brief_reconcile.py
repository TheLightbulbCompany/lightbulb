"""Fulfilment half of the onboarding funnel: reconcile_pending_brief.

``POST /onboarding/brief`` records the answer and returns (see
tests/unit/routers/test_onboarding.py). THIS is what turns that answer into a
funnel claim and a staged BRIEF.md in the dispatch target's profile (no Paperclip).

Two properties carry the design, and most of what follows pins one of them:

* **A failure anywhere must leave the pending item in place.** It is the only
  record of what the user typed; the old fused endpoint held it in the browser,
  so a closed tab lost it. Retirement therefore happens on proof of fulfilment
  and nothing weaker.
* **The retry engine has to be here.** The /container/status poll looks like a
  free one and isn't: dismissing the funnel opens chat, chat makes
  runtime_phase "ready", and the client's poll interval drops to 0 — the poll
  stops at exactly the moment a stranded brief needs another attempt.

``_RETRY_BACKOFF_S`` is patched to zeros throughout: the real cadence is
minutes, and every retry path below would otherwise sleep through it.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import core.repositories.workspace_repo as repo_mod
import core.services.onboarding_brief as brief_mod
import core.services.clerk_admin as clerk_mod
import core.services.agent_registry as registry_mod
from core.repositories.workspace_repo import FunnelClaimResult
from core.services.onboarding_brief import reconcile_pending_brief

FRIDAY = {"id": "friday-uuid", "role": "ceo", "name": "Friday"}
ATLAS = {"id": "atlas-uuid", "role": "recruiter", "name": "Atlas"}

PENDING = {
    "user_id": "__pending_brief__:org_abc",
    "kind": "pending_brief",
    "owner_id": "org_abc",
    "path": "grow",
    "input": "https://acme.com",
    "brief_text": "work on acme.com",
    "submitted_by": "member_zed",
    "target_agent_id": "friday-uuid",
}


# The workspace row is keyed by the owner (org) id, and every onboarding write
# keys on it. ``provisioner_user_id`` is deliberately DIFFERENT from both the
# owner id and the submitter.
def _owner_row(**overrides):
    base = dict(
        org_id="org_abc",
        provisioner_user_id="provisioner-user",
        member_user_ids={"provisioner-user", "member-b"},
        company_id="comp-1",
        status="active",
        onboarding_generation="01GEN",
        onboarding_goal_id=None,
        funnel_state="not_started",
        funnel_submitted_at=None,
        starter_agent_ids={
            "ceo": "friday-uuid",
            "engineering": "engineering-uuid",
            "finance": "finance-uuid",
            "growth": "growth-uuid",
            "analytics": "analytics-uuid",
            "customer_operations": "customer-operations-uuid",
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def env(monkeypatch):
    repo = MagicMock()
    repo.get_pending_brief = AsyncMock(return_value=dict(PENDING))
    repo.resolve_owner_row = AsyncMock(return_value=_owner_row())
    repo.claim_funnel_submission = AsyncMock(return_value=FunnelClaimResult.WON)
    repo.complete_funnel_submission = AsyncMock(return_value=True)
    repo.revert_funnel_submission = AsyncMock()
    repo.delete_pending_brief = AsyncMock()
    monkeypatch.setattr(repo_mod, "WorkspaceRepo", lambda **kwargs: repo)

    # The workspace's agents as agent_registry holds them (roster rows carry `id`, get rows `agentId`).
    agents = [FRIDAY, ATLAS]
    registry = SimpleNamespace(agents=agents)
    registry.get = AsyncMock(
        side_effect=lambda _ws, aid: next(({"agentId": a["id"], **a} for a in registry.agents if a["id"] == aid), None)
    )
    registry.roster = AsyncMock(side_effect=lambda _ws: list(registry.agents))
    monkeypatch.setattr(registry_mod, "get", registry.get)
    monkeypatch.setattr(registry_mod, "roster", registry.roster)
    monkeypatch.setattr(clerk_mod, "get_primary_email", AsyncMock(return_value="max@acme.com"))

    stage = AsyncMock(return_value="BRIEF.md")
    monkeypatch.setattr("core.services.hermes_profiles.write_profile_file", stage)

    repo.record_pending_brief_failure = AsyncMock(return_value=1)
    repo.fail_pending_brief = AsyncMock()
    # Real cadence is 2s..60s. Keep the same NUMBER of attempts (the failure
    # budget is asserted below) and drop only the waiting.
    monkeypatch.setattr(brief_mod, "_RETRY_BACKOFF_S", (0,) * len(brief_mod._RETRY_BACKOFF_S))
    # The drain dedupes concurrent runs per owner in a module-level set; a test
    # that raises mid-drain must not leave an owner poisoned for the next one.
    brief_mod._draining.clear()

    return SimpleNamespace(repo=repo, registry=registry, stage=stage)


# --- Nothing to do -----------------------------------------------------------


async def test_no_pending_brief_is_a_cheap_noop(env):
    """The overwhelmingly common case: every /container/status poll for an
    owner who has no outstanding answer."""
    env.repo.get_pending_brief.return_value = None

    await reconcile_pending_brief("org_abc")

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

    await reconcile_pending_brief("org_abc")

    env.repo.claim_funnel_submission.assert_not_awaited()
    env.repo.delete_pending_brief.assert_not_awaited()


async def test_friday_not_seeded_yet_keeps_the_brief(env):
    env.registry.agents = [ATLAS]

    await reconcile_pending_brief("org_abc")

    env.repo.claim_funnel_submission.assert_not_awaited()
    env.repo.delete_pending_brief.assert_not_awaited()


async def test_legacy_targetless_pending_brief_uses_existing_root_ceo(env):
    legacy_pending = dict(PENDING)
    legacy_pending.pop("target_agent_id")
    env.repo.get_pending_brief.return_value = legacy_pending
    env.registry.agents = [
        {
            "id": "imported-ceo-uuid",
            "role": "ceo",
            "name": "Imported CEO",
            "reportsTo": "friday-uuid",
        },
        {**FRIDAY, "reportsTo": None},
    ]

    await reconcile_pending_brief("org_abc")

    assert env.stage.await_args.args[1:3] == ("friday-uuid", "BRIEF.md")


async def test_legacy_targetless_pending_without_root_keeps_the_brief(env):
    legacy_pending = dict(PENDING)
    legacy_pending.pop("target_agent_id")
    env.repo.get_pending_brief.return_value = legacy_pending
    env.registry.agents = [
        ATLAS,
        {
            "id": "imported-ceo-uuid",
            "role": "ceo",
            "name": "Imported CEO",
            "reportsTo": "missing-parent",
        },
    ]

    await reconcile_pending_brief("org_abc")

    env.repo.claim_funnel_submission.assert_not_awaited()
    env.stage.assert_not_awaited()
    env.repo.delete_pending_brief.assert_not_awaited()


async def test_registry_unavailable_keeps_the_brief_and_claims_nothing(env):
    """Reads happen BEFORE the claim, so a registry blip costs nothing --
    there is no claim to unwind."""
    env.registry.get.side_effect = RuntimeError("registry down")

    await reconcile_pending_brief("org_abc")

    env.repo.claim_funnel_submission.assert_not_awaited()
    env.repo.revert_funnel_submission.assert_not_awaited()
    env.repo.delete_pending_brief.assert_not_awaited()


# --- The happy path ----------------------------------------------------------


async def test_brief_staged_without_paperclip(env, monkeypatch):
    """The first room message stages BRIEF.md once, for the target the registry knows, with no Paperclip call."""
    from core.services.messaging import default_room_id

    env.repo.resolve_owner_row.return_value = _owner_row(
        starter_agent_ids={
            "ceo": "friday-uuid",
            "finance": "finance-uuid",
            "growth": "growth-uuid",
            "analytics": "analytics-uuid",
            "customer_operations": "customer-operations-uuid",
        }
    )
    first = {
        "roomId": default_room_id("org_abc"),
        "messageSeq": 1,
        "messageId": "message-first",
        "text": "Build a calmer scheduling tool for clinics",
        "author": {"kind": "human", "id": "member_zed"},
        "dispatchTargets": [{"agentId": "friday-uuid", "eventKind": "room_event"}],
    }

    async def store_brief(_owner_id, **values):
        env.repo.get_pending_brief.return_value = {
            **PENDING,
            "input": values["user_input"],
            "brief_text": values["brief_text"],
            "submitted_by": values["submitted_by"],
            "target_agent_id": values["target_agent_id"],
        }
        return True

    env.repo.put_pending_brief = AsyncMock(side_effect=store_brief)
    monkeypatch.setattr(
        brief_mod.messaging_repo,
        "list_messages_between",
        AsyncMock(
            side_effect=lambda _room, _start, through: (
                [] if through == 0 else [first, {"messageSeq": 2, "author": {"kind": "agent"}}]
            )
        ),
    )

    assert await brief_mod.capture_first_workspace_message("org_abc", first) is True
    assert await brief_mod.capture_first_workspace_message("org_abc", {**first, "messageSeq": 3}) is False

    env.repo.put_pending_brief.assert_awaited_once()
    env.stage.assert_awaited_once()
    assert env.stage.await_args.args[:3] == ("org_abc", "friday-uuid", "BRIEF.md")


async def test_brief_goes_to_captured_dispatch_target_not_starter_ceo(env):
    specialist = {"id": "engineering-uuid", "role": "engineer", "name": "Ada"}
    env.repo.get_pending_brief.return_value = {
        **PENDING,
        "target_agent_id": specialist["id"],
    }
    env.registry.agents = [FRIDAY, specialist]

    await reconcile_pending_brief("org_abc")

    assert env.stage.await_args.args[1:3] == ("engineering-uuid", "BRIEF.md")
    # Only the root CEO files approve_ceo_strategy; a specialist's one decision is a board approval.
    assert "request_board_approval" in env.stage.await_args.args[3]
    assert "approve_ceo_strategy" not in env.stage.await_args.args[3]


async def test_drain_claims_stages_completes_and_retires_without_synthetic_dispatch(env):
    await reconcile_pending_brief("org_abc")

    # Claim is fenced on the workspace row (the owner id) + generation, and
    # carries the submitting MEMBER so the kickoff addresses the right session.
    env.repo.claim_funnel_submission.assert_awaited_once_with(
        "org_abc",
        onboarding_generation="01GEN",
        submitted_by="member_zed",
        brief_text="work on acme.com",
    )
    brief_call = env.stage.await_args_list[0]
    assert brief_call.args[0] == "org_abc"  # owner id -- the profile prefix
    assert brief_call.args[1:3] == ("friday-uuid", "BRIEF.md")
    assert "acme.com" in brief_call.args[3]
    assert "approve_ceo_strategy" in brief_call.args[3]  # the target is the root CEO
    env.repo.complete_funnel_submission.assert_awaited_once_with("org_abc", onboarding_generation="01GEN")
    env.repo.delete_pending_brief.assert_awaited_once_with("org_abc")


# --- Losing the claim --------------------------------------------------------


async def test_stale_generation_keeps_the_brief_and_reverts_nothing(env):
    """Reprovisioned underneath us: our view is stale, not duplicate. The
    next pass re-reads the new generation and drains against that."""
    env.repo.claim_funnel_submission.return_value = FunnelClaimResult.STALE_GENERATION

    await reconcile_pending_brief("org_abc")

    env.stage.assert_not_awaited()
    env.repo.delete_pending_brief.assert_not_awaited()
    env.repo.revert_funnel_submission.assert_not_awaited()


async def test_lost_claim_retires_the_brief_once_the_funnel_completed(env):
    """`funnel_state == "complete"` is set only by mark_bootstrap_delivered,
    i.e. Friday's kickoff was OBSERVED to deliver. That is unambiguous."""
    env.repo.claim_funnel_submission.return_value = FunnelClaimResult.ALREADY_SUBMITTED
    env.repo.resolve_owner_row.return_value = _owner_row(funnel_state="complete", onboarding_goal_id="goal-1")

    await reconcile_pending_brief("org_abc")

    env.repo.delete_pending_brief.assert_awaited_once_with("org_abc")
    env.stage.assert_not_awaited()


async def test_lost_claim_keeps_the_brief_while_the_winner_could_still_revert(env):
    """A young claim is a winner mid-flight that can still fail and revert; deleting the item
    then would lose the only record of what the user asked for."""
    env.repo.claim_funnel_submission.return_value = FunnelClaimResult.ALREADY_SUBMITTED
    env.repo.resolve_owner_row.return_value = _owner_row(
        funnel_state="submitted",
        funnel_submitted_at=datetime.now(timezone.utc),  # winner is mid-flight
    )

    await reconcile_pending_brief("org_abc")

    env.repo.delete_pending_brief.assert_not_awaited()


async def test_a_stale_claim_with_nothing_seeded_is_reverted_so_it_can_be_re_won(env):
    """A drain that won the CAS then died before seeding (rolling deploy,
    cancelled task) leaves the row "submitted" with nothing behind it. The
    generation fence has no stale-steal of its own, so without this the row is
    unclaimable forever while the funnel stays dismissed -- and
    maybe_dispatch_bootstrap, which guards only on "submitted", would wake
    Friday into a company with no goal and no project."""
    env.repo.claim_funnel_submission.return_value = FunnelClaimResult.ALREADY_SUBMITTED
    env.repo.resolve_owner_row.return_value = _owner_row(
        funnel_state="submitted",
        onboarding_goal_id=None,
        funnel_submitted_at=datetime.now(timezone.utc) - timedelta(seconds=brief_mod.STALE_CLAIM_S + 5),
    )

    await reconcile_pending_brief("org_abc")

    env.repo.revert_funnel_submission.assert_awaited_with("org_abc", onboarding_generation="01GEN")
    env.repo.delete_pending_brief.assert_not_awaited()


async def test_a_young_claim_with_nothing_seeded_yet_is_left_alone(env):
    """Seconds old with no ids is a peer mid-seed, not a corpse."""
    env.repo.claim_funnel_submission.return_value = FunnelClaimResult.ALREADY_SUBMITTED
    env.repo.resolve_owner_row.return_value = _owner_row(
        funnel_state="submitted", onboarding_goal_id=None, funnel_submitted_at=datetime.now(timezone.utc)
    )

    await reconcile_pending_brief("org_abc")

    env.repo.revert_funnel_submission.assert_not_awaited()
    env.repo.delete_pending_brief.assert_not_awaited()


# --- Failing after the claim -------------------------------------------------


async def test_stage_failure_reverts_the_claim_and_keeps_the_brief(env):
    env.stage.side_effect = RuntimeError("s3 down")

    await reconcile_pending_brief("org_abc")

    env.repo.revert_funnel_submission.assert_awaited_with("org_abc", onboarding_generation="01GEN")
    env.repo.complete_funnel_submission.assert_not_awaited()
    env.repo.delete_pending_brief.assert_not_awaited()


# --- The failure budget ------------------------------------------------------


async def test_a_deterministic_failure_gives_up_and_hands_the_funnel_back(env):
    """The client's 6-attempt bound was deleted with the retry loop; without a
    server-side one a deterministic failure re-ran forever at poll cadence,
    while the user sat in an empty chat with an agent that never started and no way to
    resubmit. Exhausting the budget flips the item to `failed`, which is what
    brings the funnel BACK."""
    env.stage.side_effect = RuntimeError("deterministically broken")
    env.repo.record_pending_brief_failure.side_effect = list(range(1, brief_mod.MAX_DRAIN_FAILURES + 1))

    await reconcile_pending_brief("org_abc")

    env.repo.fail_pending_brief.assert_awaited_once()
    assert env.repo.fail_pending_brief.await_args.kwargs["reason"]
    # And it stopped: no further attempts after the budget ran out.
    assert env.stage.await_count == brief_mod.MAX_DRAIN_FAILURES


async def test_a_read_failure_burns_the_budget(env):
    env.registry.get.side_effect = RuntimeError("down")
    env.repo.record_pending_brief_failure.side_effect = list(range(1, brief_mod.MAX_DRAIN_FAILURES + 1))

    await reconcile_pending_brief("org_abc")

    env.repo.fail_pending_brief.assert_awaited_once()


async def test_not_ready_never_burns_a_failure_attempt(env):
    """Provisioning taking its time is the expected path, not a failure. If
    waiting counted against the budget, a slow provision would exhaust it
    before fulfilment was even possible."""
    env.repo.resolve_owner_row.return_value = None

    await reconcile_pending_brief("org_abc")

    env.repo.record_pending_brief_failure.assert_not_awaited()
    env.repo.fail_pending_brief.assert_not_awaited()
    env.repo.delete_pending_brief.assert_not_awaited()


async def test_an_item_we_already_gave_up_on_is_not_drained_again(env):
    """A `failed` item has been handed back to the user via the funnel; the
    drain must leave it for them to overwrite."""
    env.repo.get_pending_brief.return_value = {**PENDING, "brief_state": "failed"}

    await reconcile_pending_brief("org_abc")

    env.repo.resolve_owner_row.assert_not_awaited()
    env.repo.claim_funnel_submission.assert_not_awaited()


async def test_a_second_drain_for_the_same_owner_returns_while_one_is_in_flight(env):
    """The poll fires this every ~3s per tab and the endpoint fires it again.
    Without dedupe each call independently does a Clerk lookup and a registry read.

    The first drain is suspended mid-flight on purpose: with every dependency
    mocked there is no suspension point, so calls would simply run one after
    another and the test would prove nothing about concurrency.
    """
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_get(_ws, _aid):
        in_flight.set()
        await release.wait()
        return {"agentId": FRIDAY["id"], **FRIDAY}

    env.registry.get.side_effect = _blocking_get

    first = asyncio.create_task(reconcile_pending_brief("org_abc"))
    # Bounded: if the drain fails before the registry read, in_flight never fires --
    # fail loudly instead of hanging the suite.
    await asyncio.wait_for(in_flight.wait(), timeout=5)

    await reconcile_pending_brief("org_abc")  # must be a no-op
    assert env.registry.get.await_count == 1

    release.set()
    await first
    assert env.stage.await_count == 1


# --- Analytics ---------------------------------------------------------------


async def test_the_conversion_event_fires_at_fulfilment_not_intake(env, monkeypatch):
    """This is the metric that answers "did onboarding work". Emitted at
    intake it would report a 100% funnel straight through a fleet-wide
    fulfilment outage -- the exact failure this mechanism exists to surface."""
    capture = MagicMock()
    monkeypatch.setattr(brief_mod, "capture_product_event", capture)

    await reconcile_pending_brief("org_abc")

    assert capture.call_args.kwargs["event"] == "first_run_brief_submitted"
    # Attributed to the member who actually submitted, not the workspace's provisioner.
    assert capture.call_args.kwargs["distinct_id"] == "member_zed"
    assert capture.call_args.kwargs["properties"] == {"path": "grow", "no_idea": False}


async def test_no_conversion_event_when_fulfilment_fails(env, monkeypatch):
    capture = MagicMock()
    monkeypatch.setattr(brief_mod, "capture_product_event", capture)
    env.stage.side_effect = RuntimeError("upstream down")

    await reconcile_pending_brief("org_abc")

    capture.assert_not_called()


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

    await reconcile_pending_brief("org_abc")  # must not raise


async def test_cancellation_still_propagates(env):
    """BaseException handling exists for the cleanup, not to swallow a
    shutdown: CancelledError must keep propagating after the rollback."""
    env.stage.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await reconcile_pending_brief("org_abc")

    env.repo.revert_funnel_submission.assert_awaited_once()
