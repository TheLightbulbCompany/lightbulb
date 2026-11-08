"""Tests for scripts/provision_runner.py's exit-code ABI and error recording.

No lease: the runner takes no claim and holds no fence. Three legs — prepare
the workspace, seed the workspace row (step 0), create the AgentCore runtime — and a
failure is recorded on the row with its retryability before the mapped exit.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import provision_runner

BACKEND_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def hermes(monkeypatch):
    """Step 0 is neutralized here so the runtime tests stay about the runtime
    work; the step-0 tests re-patch it, and the _seed_owner_workspace helper
    tests never take this fixture."""
    monkeypatch.setattr(provision_runner, "_seed_owner_workspace", AsyncMock(return_value=provision_runner.EXIT_OK))
    prepare = AsyncMock()
    provision = AsyncMock(return_value="arn:hermes-1")
    monkeypatch.setattr(provision_runner.hermes_provisioning, "prepare_hermes_workspace", prepare)
    monkeypatch.setattr(provision_runner.hermes_provisioning, "provision_hermes_runtime", provision)
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
    reconcile = AsyncMock(return_value="current")
    monkeypatch.setattr(provision_runner.hermes_provisioning, "reconcile_runtime", reconcile)
    drain = AsyncMock()
    monkeypatch.setattr("core.services.onboarding_brief.reconcile_pending_brief", drain)
    arm_trial = AsyncMock(return_value=None)
    monkeypatch.setattr("core.repositories.billing_repo.arm_trial_if_unset", arm_trial)
    vendor_room = AsyncMock()
    monkeypatch.setattr("core.services.messaging.ensure_vendor_room", vendor_room)
    join_room = AsyncMock()
    monkeypatch.setattr("core.services.messaging.join_default_room", join_room)
    join_feed = AsyncMock()
    monkeypatch.setattr("core.services.messaging.join_feed_room", join_feed)
    members = AsyncMock(return_value=[{"user_id": "user_creator"}, {"user_id": "user_early"}])
    monkeypatch.setattr("core.services.clerk_admin.list_organization_members", members)
    return SimpleNamespace(
        prepare=prepare,
        provision=provision,
        reconcile=reconcile,
        drain=drain,
        arm_trial=arm_trial,
        vendor_room=vendor_room,
        join_room=join_room,
        join_feed=join_feed,
    )


@pytest.fixture
def repo(monkeypatch):
    """The REAL container_repo module (imported lazily inside run()), with its
    error writer stubbed."""
    import core.repositories.container_repo as _repo_module

    monkeypatch.setattr(_repo_module, "mark_provision_error", AsyncMock())
    return _repo_module


def test_module_is_invocable_as_dash_m():
    # The manual CLI: `uv run python -m scripts.provision_runner --owner …` from the backend root.
    result = subprocess.run(
        [sys.executable, "-m", "scripts.provision_runner", "--help"],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--claim" not in result.stdout


async def test_happy_path_runs_every_leg_and_exits_0(monkeypatch, hermes, repo):
    assert await provision_runner.run("org_a") == provision_runner.EXIT_OK
    hermes.prepare.assert_awaited_once_with("org_a")
    hermes.provision.assert_awaited_once_with("org_a")
    hermes.reconcile.assert_awaited_once_with("org_a")
    repo.mark_provision_error.assert_not_awaited()


async def test_the_brief_drains_after_step_0_and_the_trial_arms_after_the_runtime(monkeypatch, hermes, repo):
    """The two duties the status poll used to run: the pending onboarding brief once the
    workspace is active (step 0), the trial clock once the runtime is running."""
    order: list[str] = []
    monkeypatch.setattr(
        provision_runner, "_seed_owner_workspace", AsyncMock(side_effect=lambda _o: order.append("seed") or 0)
    )
    hermes.drain.side_effect = lambda _o: order.append("drain")
    hermes.vendor_room.side_effect = lambda _o: order.append("vendor_room")
    hermes.provision.side_effect = lambda _o: order.append("runtime")
    hermes.arm_trial.side_effect = lambda _o: order.append("arm_trial")
    hermes.reconcile.side_effect = lambda _o: order.append("reconcile")

    assert await provision_runner.run("org_a") == provision_runner.EXIT_OK
    assert order == ["seed", "drain", "vendor_room", "runtime", "arm_trial", "reconcile"]


async def test_every_teammate_is_seated_once_the_company_is_active(monkeypatch, hermes, repo):
    """The status poll used to create the vendor room and seat teammates the membership webhook
    missed (one who joined before #general existed). The executor does it after step 0; a
    failure never fails the provision."""
    hermes.join_room.side_effect = [RuntimeError("ddb"), True]
    assert await provision_runner.run("org_a") == provision_runner.EXIT_OK
    hermes.vendor_room.assert_awaited_once_with("org_a")
    assert [c.args for c in hermes.join_room.await_args_list] == [("org_a", "user_creator"), ("org_a", "user_early")]
    # ...and in the workspace feed, the same way.
    assert [c.args for c in hermes.join_feed.await_args_list] == [("org_a", "user_creator"), ("org_a", "user_early")]
    repo.mark_provision_error.assert_not_awaited()


async def test_a_roster_read_failure_never_fails_the_provision(monkeypatch, hermes, repo):
    monkeypatch.setattr(
        "core.services.clerk_admin.list_organization_members", AsyncMock(side_effect=RuntimeError("clerk"))
    )
    assert await provision_runner.run("org_a") == provision_runner.EXIT_OK
    hermes.provision.assert_awaited_once_with("org_a")


async def test_drain_and_trial_arm_failures_never_fail_the_provision(monkeypatch, hermes, repo):
    hermes.drain.side_effect = RuntimeError("paperclip down")
    hermes.arm_trial.side_effect = RuntimeError("ddb down")
    assert await provision_runner.run("org_a") == provision_runner.EXIT_OK
    hermes.reconcile.assert_awaited_once_with("org_a")
    repo.mark_provision_error.assert_not_awaited()


async def test_a_slow_brief_drain_is_bounded(monkeypatch, hermes, repo):
    """The drain sleeps between its own retries; it must not eat the runtime leg's Lambda budget."""
    import asyncio

    async def _hang(_owner_id):
        await asyncio.sleep(3600)

    hermes.drain.side_effect = _hang
    monkeypatch.setattr(provision_runner, "BRIEF_DRAIN_TIMEOUT_S", 0.01)
    assert await provision_runner.run("org_a") == provision_runner.EXIT_OK
    hermes.provision.assert_awaited_once_with("org_a")


async def test_a_failed_runtime_leg_does_not_arm_the_trial(monkeypatch, hermes, repo):
    hermes.provision.side_effect = RuntimeError("create failed")
    await provision_runner.run("org_a")
    hermes.arm_trial.assert_not_awaited()


async def test_a_live_row_skips_the_early_prepare_so_the_reconcile_sees_the_boot_change(monkeypatch, hermes, repo):
    """C1: step -1 on a live row rewrote the boot secret first, so the end reconcile saw no
    boot-time change and never restarted the warm session holding the old bearer."""
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id",
        AsyncMock(return_value={"agentcore_runtime_arn": "arn:rt", "status": "running"}),
    )
    assert await provision_runner.run("org_a") == provision_runner.EXIT_OK
    hermes.prepare.assert_not_awaited()
    hermes.reconcile.assert_awaited_once_with("org_a")


async def test_a_failed_end_reconcile_is_not_a_provision_failure(monkeypatch, hermes, repo):
    """The runtime is already serving; reconcile_runtime reports its own failure by metric."""
    hermes.reconcile.return_value = "failed"
    assert await provision_runner.run("org_a") == provision_runner.EXIT_OK
    repo.mark_provision_error.assert_not_awaited()


async def test_a_failed_provision_skips_the_end_reconcile(monkeypatch, hermes, repo):
    hermes.provision.side_effect = RuntimeError("create failed")
    await provision_runner.run("org_a")
    hermes.reconcile.assert_not_awaited()


async def test_deterministic_error_exits_10_and_is_recorded(monkeypatch, hermes, repo):
    hermes.provision.side_effect = Exception("byo_provider is required when provider_choice == 'byo_key'")
    assert await provision_runner.run("org_a") == provision_runner.EXIT_DETERMINISTIC
    assert repo.mark_provision_error.await_args.kwargs["retryable"] is False


async def test_transient_error_exits_11_and_writes_no_error_row(monkeypatch, hermes, repo):
    """The machine retries a transient failure within the execution; an error row now would
    flash "Setup failed" over a provision about to succeed. MarkFailedExhausted is the backstop."""
    hermes.provision.side_effect = Exception("ThrottlingException")
    assert await provision_runner.run("org_a") == provision_runner.EXIT_TRANSIENT
    repo.mark_provision_error.assert_not_awaited()


async def test_error_record_failure_still_returns_the_exit_code(monkeypatch, hermes, repo):
    hermes.provision.side_effect = Exception("ThrottlingException")
    repo.mark_provision_error = AsyncMock(side_effect=RuntimeError("ddb down"))
    assert await provision_runner.run("org_a") == provision_runner.EXIT_TRANSIENT


async def test_openrouter_claim_conflict_retries_without_flashing_an_error_row(monkeypatch, hermes, repo):
    """The claim loser: a concurrent prepare already won this owner's OpenRouter
    key claim. SFN retries in 10s (the winner normally finishes in a couple),
    so the row must NOT be written to status=error -- that write is exactly
    what showed the user a provision-error flash for a self-healing race."""
    from core.services.openrouter_keys import KeyClaimConflict

    hermes.prepare.side_effect = KeyClaimConflict("OpenRouter key provisioning is already in progress")
    assert await provision_runner.run("org_a") == provision_runner.EXIT_TRANSIENT
    repo.mark_provision_error.assert_not_awaited()


# ---------------------------------------------------------------------------
# Step 0: owner workspace seed, before the runtime work
# ---------------------------------------------------------------------------


async def test_seed_step_0_always_runs_then_provisions_runtime(monkeypatch, hermes, repo):
    seed = AsyncMock(return_value=provision_runner.EXIT_OK)
    monkeypatch.setattr(provision_runner, "_seed_owner_workspace", seed)
    assert await provision_runner.run("org_a") == provision_runner.EXIT_OK
    seed.assert_awaited_once_with("org_a")
    hermes.provision.assert_awaited_once()


async def test_seed_step_0_deterministic_fail_exits_10_skips_runtime_surfaces_error(monkeypatch, hermes, repo):
    """Seed deterministic-fail (10): exit 10 WITHOUT provisioning the runtime,
    and the row is flipped to error(retryable=False) so the poll SURFACES the
    failure instead of looping it."""
    seed = AsyncMock(return_value=provision_runner.EXIT_DETERMINISTIC)
    monkeypatch.setattr(provision_runner, "_seed_owner_workspace", seed)
    assert await provision_runner.run("org_a") == provision_runner.EXIT_DETERMINISTIC
    hermes.provision.assert_not_awaited()
    repo.mark_provision_error.assert_awaited_once()
    assert repo.mark_provision_error.await_args.kwargs["retryable"] is False


async def test_seed_step_0_transient_fail_exits_11_skips_runtime_writes_no_error(monkeypatch, hermes, repo):
    seed = AsyncMock(return_value=provision_runner.EXIT_TRANSIENT)
    monkeypatch.setattr(provision_runner, "_seed_owner_workspace", seed)
    assert await provision_runner.run("org_a") == provision_runner.EXIT_TRANSIENT
    hermes.provision.assert_not_awaited()
    repo.mark_provision_error.assert_not_awaited()


async def test_seed_step_0_error_record_failure_still_returns_exit_code(monkeypatch, hermes, repo):
    seed = AsyncMock(return_value=provision_runner.EXIT_DETERMINISTIC)
    monkeypatch.setattr(provision_runner, "_seed_owner_workspace", seed)
    repo.mark_provision_error = AsyncMock(side_effect=RuntimeError("ddb down"))
    assert await provision_runner.run("org_a") == provision_runner.EXIT_DETERMINISTIC
    hermes.provision.assert_not_awaited()


# ---- _seed_owner_workspace direct tests: creator resolution + classification ----


@pytest.fixture(autouse=True)
def _stub_proactive(monkeypatch):
    """Step 0 ensures the proactive layer right after the seed; stub it so the
    seed tests exercise only seed + classification."""
    ensure = AsyncMock()
    monkeypatch.setattr("core.services.proactive.ensure", ensure)
    return ensure


def _stub_org_seed(monkeypatch, seed=None):
    """Patch the org seed's collaborators: the Clerk creator lookup and
    ``_ensure_workspace_for_org`` itself (returned)."""
    seed = seed or AsyncMock()
    monkeypatch.setattr("core.services.user_provision._ensure_workspace_for_org", seed)
    monkeypatch.setattr(
        "core.services.clerk_admin.get_organization",
        AsyncMock(return_value={"created_by": "user_c"}),
    )
    return seed


async def test_seed_helper_refuses_non_org_owner(monkeypatch, _stub_proactive):
    """Personal workspaces no longer exist: a user_ owner is refused with
    EXIT_DETERMINISTIC before Clerk, the seed or the arm is touched — the
    runner then flips the row to error(retryable=False) instead of seeding a
    personal workspace."""
    seed = _stub_org_seed(monkeypatch)
    get_org = AsyncMock()
    monkeypatch.setattr("core.services.clerk_admin.get_organization", get_org)
    assert await provision_runner._seed_owner_workspace("user_p") == provision_runner.EXIT_DETERMINISTIC
    get_org.assert_not_awaited()
    seed.assert_not_awaited()
    _stub_proactive.assert_not_awaited()


async def test_seed_helper_ensures_proactive_after_org_seed(monkeypatch, _stub_proactive):
    """The proactive ensure runs AFTER _ensure_workspace_for_org, with the org_id."""
    order = MagicMock()
    order.attach_mock(_stub_org_seed(monkeypatch), "seed")
    order.attach_mock(_stub_proactive, "ensure")
    assert await provision_runner._seed_owner_workspace("org_x") == provision_runner.EXIT_OK
    assert [c[0] for c in order.mock_calls] == ["seed", "ensure"]
    _stub_proactive.assert_awaited_once_with("org_x")


async def test_seed_helper_proactive_failure_never_fails_the_seed(monkeypatch, _stub_proactive):
    _stub_org_seed(monkeypatch)
    _stub_proactive.side_effect = RuntimeError("boom")
    assert await provision_runner._seed_owner_workspace("org_p") == provision_runner.EXIT_OK


async def test_seed_helper_org_resolves_creator_then_calls_ensure_workspace_for_org(monkeypatch):
    """Org owner_id (org_...): the runner has only owner_id (dispatch drops the
    triggering user), so the bootstrap identity is the org CREATOR resolved from
    Clerk (created_by), always an admin."""
    ensure_org = AsyncMock()
    monkeypatch.setattr("core.services.user_provision._ensure_workspace_for_org", ensure_org)
    monkeypatch.setattr(
        "core.services.clerk_admin.get_organization",
        AsyncMock(return_value={"created_by": "user_creator"}),
    )
    assert await provision_runner._seed_owner_workspace("org_xyz") == provision_runner.EXIT_OK
    ensure_org.assert_awaited_once_with(org_id="org_xyz", user_id="user_creator", is_admin=True)


async def test_seed_helper_org_no_resolvable_creator_is_transient(monkeypatch):
    """No Clerk creator (org lookup returns None / a Clerk blip): defer as
    TRANSIENT so SFN retries rather than failing the provision on a blip. The
    seed helper is never entered."""
    monkeypatch.setattr("core.services.clerk_admin.get_organization", AsyncMock(return_value=None))
    ensure_org = AsyncMock()
    monkeypatch.setattr("core.services.user_provision._ensure_workspace_for_org", ensure_org)
    assert await provision_runner._seed_owner_workspace("org_xyz") == provision_runner.EXIT_TRANSIENT
    ensure_org.assert_not_awaited()


async def test_seed_helper_deterministic_error_exits_10(monkeypatch):
    """A shape/config bug out of the seed classifies DETERMINISTIC → EXIT_DETERMINISTIC
    (SFN stops, surfaced not looped)."""
    _stub_org_seed(monkeypatch, AsyncMock(side_effect=ValueError("bad roster")))
    assert await provision_runner._seed_owner_workspace("org_p") == provision_runner.EXIT_DETERMINISTIC


async def test_seed_helper_transient_store_error_exits_11(monkeypatch):
    """A registry/DynamoDB outage classifies TRANSIENT → EXIT_TRANSIENT (SFN retries)."""
    from core.services.agent_registry import RegistryUnavailable

    _stub_org_seed(monkeypatch, AsyncMock(side_effect=RegistryUnavailable("ddb down")))
    assert await provision_runner._seed_owner_workspace("org_p") == provision_runner.EXIT_TRANSIENT


async def test_seed_helper_transient_connection_error_exits_11(monkeypatch):
    """A transport/connection error defaults TRANSIENT
    → EXIT_TRANSIENT, same as the container path's classifier."""
    import httpx

    _stub_org_seed(monkeypatch, AsyncMock(side_effect=httpx.ConnectError("connection refused")))
    assert await provision_runner._seed_owner_workspace("org_p") == provision_runner.EXIT_TRANSIENT
