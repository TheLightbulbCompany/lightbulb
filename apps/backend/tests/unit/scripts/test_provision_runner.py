"""Tests for scripts/provision_runner.py exit-code ABI + fencing plumbing."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.repositories.container_repo import ProvisionSuperseded
from scripts import provision_runner

BACKEND_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def manager(monkeypatch):
    mgr = MagicMock()
    mgr.provision_user_container = AsyncMock(return_value="svc-name")
    mgr.wait_for_running = AsyncMock()
    # Container-leg liveness probe: default to "nothing discoverable" so every
    # pre-existing test still takes the full-provision path. The skip tests
    # below drive these explicitly.
    mgr.resolve_running_container = AsyncMock(return_value=(None, None))
    mgr.is_healthy = MagicMock(return_value=False)
    # Service-liveness gate in front of the probe: default to "live" so the
    # probe stays the deciding check in every pre-existing test. The draining
    # test below flips it to False.
    mgr.has_live_service = AsyncMock(return_value=True)
    monkeypatch.setattr(provision_runner, "_get_manager", lambda: mgr)
    monkeypatch.setattr(
        provision_runner,
        "_resolve_provider",
        AsyncMock(return_value=("bedrock_claude", None)),
    )
    # Step 0 (the owner Paperclip seed) is unconditional, so every run() test
    # traverses it. Neutralize it here so the container-work tests below stay
    # about the container work; the step-0 tests re-patch it with their own
    # double, and the _seed_owner_paperclip helper tests never take this
    # fixture (they call the real function directly).
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", AsyncMock(return_value=provision_runner.EXIT_OK))
    return mgr


@pytest.fixture
def repo(monkeypatch):
    """Patch core.repositories.container_repo as imported INTO provision_runner
    (it's imported at function-call time inside run(), so patch the source
    module — matches ecs_manager's own lazy-import test pattern). Returns the
    REAL module object (not a wrapper) so a test's later
    ``repo.claim_attempt = AsyncMock(...)`` reassignment is what run() sees —
    a wrapper mock's attribute would silently stop being the patched value."""
    import core.repositories.container_repo as _repo_module

    monkeypatch.setattr(_repo_module, "claim_attempt", AsyncMock())
    monkeypatch.setattr(_repo_module, "get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(_repo_module, "release_stranded_stop_to_error", AsyncMock(return_value=True))
    monkeypatch.setattr(_repo_module, "release_provision_to_error", AsyncMock())
    return _repo_module


def _row(monkeypatch, row):
    monkeypatch.setattr(provision_runner, "_get_row", AsyncMock(return_value=row))


def test_module_is_invocable_as_dash_m():
    # Transport smoke: the state machine launches the runner as
    # `uv run python -m scripts.provision_runner …` from the backend root.
    # Pins that the module resolves under -m with argparse wired — a rename
    # or import-time crash fails here, not at the first real dispatch.
    result = subprocess.run(
        [sys.executable, "-m", "scripts.provision_runner", "--help"],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


async def test_running_row_exits_zero(monkeypatch, manager, repo):
    _row(monkeypatch, {"status": "running"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK
    # The claim_id pass-through is the fencing seam: the runner must thread
    # the machine's claim into provision_user_container unchanged, or the
    # leased checkpoints fence out the machine's own run.
    manager.provision_user_container.assert_awaited_once_with(
        "user_a",
        provider_choice="bedrock_claude",
        byo_provider=None,
        owner_type="personal",
        claim_id="claim",
        generation=None,
        attempt_token="claim",  # no PROVISION_ATTEMPT_TOKEN env -> falls back to claim_id
    )
    manager.wait_for_running.assert_awaited_once_with(
        "user_a", claim_id="claim", generation=None, attempt_token="claim"
    )


async def test_first_action_is_claim_attempt(monkeypatch, manager, repo):
    """The runner's FIRST checkpoint, before provider resolution, before
    provision_user_container -- fences out a straggler from a superseded
    attempt (spec §7 hazard 1) as early as possible."""
    _row(monkeypatch, {"status": "running"})
    await provision_runner.run("user_a", "claim")
    repo.claim_attempt.assert_awaited_once_with("user_a", "claim", "claim")


async def test_reads_generation_off_the_row_not_the_cli(monkeypatch, manager, repo):
    """provision_generation is read fresh off the containers row at startup —
    NOT threaded through the SFN input/command line (see module docstring:
    embedding it in the deterministic StartExecution input would break
    Branch-A idempotency once create_user_service mints it mid-execution)."""
    repo.get_by_owner_id = AsyncMock(return_value={"provision_generation": "01ARZGENERATION"})
    _row(monkeypatch, {"status": "running"})
    await provision_runner.run("user_a", "claim")
    kwargs = manager.provision_user_container.await_args.kwargs
    assert kwargs["generation"] == "01ARZGENERATION"


async def test_wait_for_running_fences_on_post_mint_generation(monkeypatch, manager, repo):
    """The full-provision path (existing row, missing/INACTIVE service — or a
    brand-new owner) MINTS a fresh provision_generation inside
    provision_user_container. wait_for_running must fence on the CURRENT
    (post-mint) value, not the pre-create read — fencing on the stale one
    would make the runner's own running write raise ProvisionSuperseded
    against itself and wedge the row in "provisioning"."""
    repo.get_by_owner_id = AsyncMock(
        side_effect=[
            {"provision_generation": "gen-OLD"},  # pre-provision read
            {"provision_generation": "gen-MINTED"},  # post-provision re-read
        ]
    )
    _row(monkeypatch, {"status": "running"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK

    # provision_user_container got the pre-mint value (correct — the row
    # still carried it when the run started)...
    assert manager.provision_user_container.await_args.kwargs["generation"] == "gen-OLD"
    # ...and wait_for_running fences on the refreshed, post-mint value.
    manager.wait_for_running.assert_awaited_once_with(
        "user_a", claim_id="claim", generation="gen-MINTED", attempt_token="claim"
    )


async def test_attempt_token_from_env_overrides_claim_id_fallback(monkeypatch, manager, repo):
    monkeypatch.setenv("PROVISION_ATTEMPT_TOKEN", "fresh-retry-token")
    _row(monkeypatch, {"status": "running"})
    await provision_runner.run("user_a", "claim")
    repo.claim_attempt.assert_awaited_once_with("user_a", "claim", "fresh-retry-token")
    kwargs = manager.provision_user_container.await_args.kwargs
    assert kwargs["attempt_token"] == "fresh-retry-token"


async def test_claim_attempt_superseded_exits_20_before_any_provisioning(monkeypatch, manager, repo):
    """A straggler whose claim was taken over entirely (not just its attempt)
    must stop at the very first checkpoint -- never touch ECS."""
    repo.claim_attempt = AsyncMock(side_effect=ProvisionSuperseded("taken over"))
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_SUPERSEDED
    manager.provision_user_container.assert_not_awaited()


async def test_superseded_exits_20(monkeypatch, manager, repo):
    manager.provision_user_container.side_effect = ProvisionSuperseded("taken over")
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_SUPERSEDED


async def test_wait_for_running_superseded_exits_20(monkeypatch, manager, repo):
    """The fenced write inside wait_for_running (via _await_running_transition)
    can ALSO raise ProvisionSuperseded -- e.g. a straggler that got past its
    provision_user_container checkpoints but loses the race on the terminal
    running/error write."""
    manager.wait_for_running.side_effect = ProvisionSuperseded("taken over mid-wait")
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_SUPERSEDED


# ---------------------------------------------------------------------------
# Superseded-without-successor verification (#1428)
# ---------------------------------------------------------------------------
#
# ProvisionSuperseded used to be trusted as proof a successor owns the row.
# It failed live: a conversion-marker get→put clobber erased the lease, this
# runner "stood down" behind a successor that did not exist, and the row sat
# stopped/claim-less/error-less with nothing running and nothing scheduled.
# The superseded exit now re-reads the row and, on that exact stranded shape,
# flips it to error(retryable) so the status poll's auto-retry re-dispatches.


async def test_superseded_with_successor_claim_stands_down_quietly(monkeypatch, manager, repo):
    """A claim on the row == a real successor. The quiet exit stays quiet —
    and the runner must NOT second-guess the successor with a release."""
    manager.provision_user_container.side_effect = ProvisionSuperseded("taken over")
    repo.get_by_owner_id = AsyncMock(return_value={"status": "provisioning", "provision_claim_id": "claim-successor"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_SUPERSEDED
    repo.release_stranded_stop_to_error.assert_not_awaited()


async def test_superseded_with_row_gone_stands_down_quietly(monkeypatch, manager, repo):
    """No row at all (deliberate delete) — nothing to release. (The repo
    fixture's get_by_owner_id defaults to None, so this is also the shape the
    older superseded tests above run through.)"""
    manager.provision_user_container.side_effect = ProvisionSuperseded("taken over")
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_SUPERSEDED
    repo.release_stranded_stop_to_error.assert_not_awaited()


async def test_superseded_stranded_stopped_row_is_released_to_error(monkeypatch, manager, repo):
    """THE strand: superseded, but the re-read shows a stopped row with NO
    claim — nobody running, nothing scheduled. The runner must release it to
    error(retryable) via the CAS so pollers can recover, and still exit 20
    (the machine ABI is unchanged; the row write is the recovery arm)."""
    manager.provision_user_container.side_effect = ProvisionSuperseded("clobbered")
    repo.get_by_owner_id = AsyncMock(return_value={"status": "stopped"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_SUPERSEDED
    repo.release_stranded_stop_to_error.assert_awaited_once()
    assert repo.release_stranded_stop_to_error.await_args.args == ("user_a",)
    assert "no successor" in repo.release_stranded_stop_to_error.await_args.kwargs["last_error"]


async def test_superseded_unclaimed_provisioning_row_is_not_touched(monkeypatch, manager, repo):
    """An UNCLAIMED provisioning row belongs to a wake/resize transition
    (those paths never claim — see main.py's boot resume). The runner logs and
    stands down; flipping it to error would clobber their in-process poller."""
    manager.provision_user_container.side_effect = ProvisionSuperseded("taken over")
    repo.get_by_owner_id = AsyncMock(return_value={"status": "provisioning"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_SUPERSEDED
    repo.release_stranded_stop_to_error.assert_not_awaited()


async def test_superseded_release_failure_still_exits_20(monkeypatch, manager, repo):
    """The stranded-row release is best-effort: a DDB blip there must not
    change the exit-code ABI (the machine still classifies Superseded)."""
    manager.provision_user_container.side_effect = ProvisionSuperseded("clobbered")
    repo.get_by_owner_id = AsyncMock(return_value={"status": "stopped"})
    repo.release_stranded_stop_to_error = AsyncMock(side_effect=RuntimeError("ddb down"))
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_SUPERSEDED


async def test_deterministic_error_exits_10(monkeypatch, manager, repo):
    manager.provision_user_container.side_effect = Exception(
        "byo_provider is required when provider_choice == 'byo_key'"
    )
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_DETERMINISTIC


async def test_transient_error_exits_11(monkeypatch, manager, repo):
    manager.provision_user_container.side_effect = Exception("ECS ThrottlingException")
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_TRANSIENT


async def test_error_row_maps_by_retryable_field(monkeypatch, manager, repo):
    _row(monkeypatch, {"status": "error", "retryable": False})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_DETERMINISTIC
    _row(monkeypatch, {"status": "error", "retryable": True})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_TRANSIENT


async def test_external_state_change_exits_zero(monkeypatch, manager, repo):
    _row(monkeypatch, {"status": "stopped"})  # admin stop mid-wait: not ours to fight
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK


# ---------------------------------------------------------------------------
# Container leg: skipped when the owner's box is already up and healthy
# ---------------------------------------------------------------------------
#
# Every claimable row shape (absent / error / stopped / stale provisioning) can
# sit on top of a healthy live task — the commonest case being a step-0 seed
# failure, which flips a RUNNING owner's row to error while the box keeps
# serving chat. Re-running provision_user_container there rotates the owner's
# bedrock-gate token, re-puts IAM, rewrites configs and force-redeploys. So the
# runner probes first and skips straight to the terminal transition. This is
# what makes ONE dispatch safe for a caller who only needs the seed.


def _live_box(manager, ip="10.0.0.7", healthy=True):
    manager.resolve_running_container = AsyncMock(return_value=({"status": "error"}, ip))
    manager.is_healthy = MagicMock(return_value=healthy)


async def test_container_leg_skipped_when_box_already_healthy(monkeypatch, manager, repo):
    """Healthy live task → NO provision_user_container, but wait_for_running
    still runs: it is the fenced writer of the terminal running transition, so
    the claimed row must not be left sitting in "provisioning"."""
    repo.get_by_owner_id = AsyncMock(return_value={"service_name": "openclaw-user_a", "provision_generation": "gen-1"})
    _live_box(manager)
    _row(monkeypatch, {"status": "running"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK
    manager.provision_user_container.assert_not_awaited()
    manager.wait_for_running.assert_awaited_once_with(
        "user_a", claim_id="claim", generation="gen-1", attempt_token="claim"
    )


async def test_seed_still_runs_when_container_leg_is_skipped(monkeypatch, manager, repo):
    """The whole point: an owner whose box is already up still gets step 0.
    Skipping the container work must never skip the seed."""
    seed = AsyncMock(return_value=provision_runner.EXIT_OK)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    repo.get_by_owner_id = AsyncMock(return_value={"service_name": "openclaw-user_a"})
    _live_box(manager)
    _row(monkeypatch, {"status": "running"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK
    seed.assert_awaited_once_with("user_a")
    manager.provision_user_container.assert_not_awaited()


async def test_container_leg_runs_for_brand_new_owner(monkeypatch, manager, repo):
    """No service_name on the row (a first provision) → no probe, full
    provision, exactly as before."""
    repo.get_by_owner_id = AsyncMock(return_value=None)
    _live_box(manager)  # would say "healthy" if it were ever consulted
    _row(monkeypatch, {"status": "running"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK
    manager.resolve_running_container.assert_not_awaited()
    manager.provision_user_container.assert_awaited_once()


async def test_container_leg_runs_when_task_is_undiscoverable(monkeypatch, manager, repo):
    """Service exists but ECS has no task IP → provision (this is the crashed
    box the self-heal is for)."""
    repo.get_by_owner_id = AsyncMock(return_value={"service_name": "openclaw-user_a"})
    manager.resolve_running_container = AsyncMock(return_value=({"status": "error"}, None))
    _row(monkeypatch, {"status": "running"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK
    manager.provision_user_container.assert_awaited_once()


async def test_container_leg_runs_when_gateway_is_unhealthy(monkeypatch, manager, repo):
    """Task is up but the gateway port never opened (the wedged-box signature).
    The redeploy is the ONLY thing that heals that, so the skip must not
    trigger — this is why the probe is is_healthy and not has_live_service
    (an ACTIVE service with desiredCount>0 is true for a wedged box)."""
    repo.get_by_owner_id = AsyncMock(return_value={"service_name": "openclaw-user_a"})
    _live_box(manager, healthy=False)
    _row(monkeypatch, {"status": "running"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK
    manager.provision_user_container.assert_awaited_once()


async def test_container_leg_runs_when_service_is_draining(monkeypatch, manager, repo):
    """desiredCount=0 with the old task still DRAINING: the gateway probe
    still answers (a stopped task serves for ~30s+ past the stop), but
    nothing will ever scale the service back up — a deliberate stop
    (provider change / admin reprovision) exposed the claimable row in the
    first place. Skipping here parked the runner in wait_for_running for the
    full attempt timeout against a service pinned at 0 (founder-org 30-min
    provision hang, 2026-08-20). has_live_service is the guard: not live →
    full provision, whose scale-from-zero branch restarts the box."""
    repo.get_by_owner_id = AsyncMock(return_value={"service_name": "openclaw-user_a"})
    _live_box(manager)  # the draining task would still pass the probe
    manager.has_live_service = AsyncMock(return_value=False)
    _row(monkeypatch, {"status": "running"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK
    manager.provision_user_container.assert_awaited_once()
    # The probe must not even run: its answer is meaningless during a drain.
    manager.resolve_running_container.assert_not_awaited()


async def test_container_leg_runs_when_probe_raises(monkeypatch, manager, repo):
    """An indeterminate probe (ECS throttle, describe blip) provisions as
    today — the skip is the special case, never the fallback."""
    repo.get_by_owner_id = AsyncMock(return_value={"service_name": "openclaw-user_a"})
    manager.resolve_running_container = AsyncMock(side_effect=RuntimeError("throttled"))
    _row(monkeypatch, {"status": "running"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK
    manager.provision_user_container.assert_awaited_once()


# ---------------------------------------------------------------------------
# Step 0: owner Paperclip seed under the executor single-flight
# ---------------------------------------------------------------------------
#
# The OWNER company-create seed runs in the runner, BEFORE the container work,
# so it inherits try_claim_provision's single-flight (one execution per owner)
# — closing the duplicate-company race (#1442) at its source. Unconditional:
# there is no flag and no poll-thread alternative.


async def test_seed_step_0_always_runs_then_provisions_container(monkeypatch, manager, repo):
    """Seed OK → the runner continues to the container work (step 0 is an
    ADDITIONAL first step, not a replacement). The seed is unconditional and
    runs AFTER claim_attempt (a taken-over straggler stands down first)."""
    seed = AsyncMock(return_value=provision_runner.EXIT_OK)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    _row(monkeypatch, {"status": "running"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK
    seed.assert_awaited_once_with("user_a")
    repo.claim_attempt.assert_awaited_once()
    manager.provision_user_container.assert_awaited_once()


async def test_seed_step_0_deterministic_fail_exits_10_skips_container_surfaces_error(monkeypatch, manager, repo):
    """Seed deterministic-fail (10): exit 10 WITHOUT touching the container,
    and the row is flipped to error(retryable=False) so the poll SURFACES the
    failure instead of looping it."""
    seed = AsyncMock(return_value=provision_runner.EXIT_DETERMINISTIC)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_DETERMINISTIC
    manager.provision_user_container.assert_not_awaited()
    repo.release_provision_to_error.assert_awaited_once()
    kwargs = repo.release_provision_to_error.await_args.kwargs
    assert kwargs["retryable"] is False
    assert kwargs["attempt_token"] == "claim"


async def test_seed_step_0_transient_fail_exits_11_skips_container_retryable_error(monkeypatch, manager, repo):
    """Seed transient-fail (11): exit 11 (SFN retries) WITHOUT the container,
    row flipped to error(retryable=True) for the poll's auto-retry."""
    seed = AsyncMock(return_value=provision_runner.EXIT_TRANSIENT)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_TRANSIENT
    manager.provision_user_container.assert_not_awaited()
    repo.release_provision_to_error.assert_awaited_once()
    assert repo.release_provision_to_error.await_args.kwargs["retryable"] is True


async def test_seed_step_0_error_release_failure_still_returns_exit_code(monkeypatch, manager, repo):
    """The container-row error release is best-effort: a DDB blip there must
    not change the exit-code ABI the machine reads."""
    seed = AsyncMock(return_value=provision_runner.EXIT_DETERMINISTIC)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    repo.release_provision_to_error = AsyncMock(side_effect=RuntimeError("ddb down"))
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_DETERMINISTIC
    manager.provision_user_container.assert_not_awaited()


async def test_seed_step_0_superseded_claim_stands_down_before_seeding(monkeypatch, manager, repo):
    """A straggler whose claim was taken over stops at claim_attempt — the seed
    (like the container work) never runs, and the runner exits 20."""
    seed = AsyncMock(return_value=provision_runner.EXIT_OK)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    repo.claim_attempt = AsyncMock(side_effect=ProvisionSuperseded("taken over"))
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_SUPERSEDED
    seed.assert_not_awaited()
    manager.provision_user_container.assert_not_awaited()


# ---- _seed_owner_paperclip direct tests: org/email resolution + classification ----


@pytest.fixture(autouse=True)
def _stub_arm_nightly_ceo(monkeypatch):
    """Step 0 arms the nightly CEO routine right after the seed; stub it so the
    seed tests exercise only seed + classification."""
    arm = AsyncMock(return_value="armed")
    monkeypatch.setattr("core.services.user_provision.arm_nightly_ceo", arm)
    return arm


def _stub_personal_seed(monkeypatch):
    seed = AsyncMock()
    monkeypatch.setattr("core.services.user_provision._ensure_paperclip_for_owner", seed)
    monkeypatch.setattr(
        "core.services.paperclip_owner_email.lookup_owner_email",
        AsyncMock(return_value="p@example.com"),
    )
    return seed


async def test_seed_helper_arms_nightly_ceo_after_personal_seed(monkeypatch, _stub_arm_nightly_ceo):
    """The nightly CEO arm runs AFTER the personal seed, with the owner_id."""
    order = MagicMock()
    order.attach_mock(_stub_personal_seed(monkeypatch), "seed")
    order.attach_mock(_stub_arm_nightly_ceo, "arm")
    assert await provision_runner._seed_owner_paperclip("user_p") == provision_runner.EXIT_OK
    assert [c[0] for c in order.mock_calls] == ["seed", "arm"]
    _stub_arm_nightly_ceo.assert_awaited_once_with("user_p", trust_cache=False)


async def test_seed_helper_arms_nightly_ceo_after_org_seed(monkeypatch, _stub_arm_nightly_ceo):
    """Same for an org owner: arm AFTER _ensure_paperclip_for_org, with the org_id."""
    seed = AsyncMock()
    monkeypatch.setattr("core.services.user_provision._ensure_paperclip_for_org", seed)
    monkeypatch.setattr(
        "core.services.clerk_admin.get_organization",
        AsyncMock(return_value={"created_by": "user_c"}),
    )
    monkeypatch.setattr(
        "core.services.paperclip_owner_email.lookup_owner_email",
        AsyncMock(return_value="c@example.com"),
    )
    order = MagicMock()
    order.attach_mock(seed, "seed")
    order.attach_mock(_stub_arm_nightly_ceo, "arm")
    assert await provision_runner._seed_owner_paperclip("org_x") == provision_runner.EXIT_OK
    assert [c[0] for c in order.mock_calls] == ["seed", "arm"]
    _stub_arm_nightly_ceo.assert_awaited_once_with("org_x", trust_cache=False)


async def test_seed_helper_arm_5xx_exits_11(monkeypatch, _stub_arm_nightly_ceo):
    """An arm failure classifies like a seed failure: 5xx → EXIT_TRANSIENT."""
    from core.services.paperclip_admin_client import PaperclipApiError

    _stub_personal_seed(monkeypatch)
    _stub_arm_nightly_ceo.side_effect = PaperclipApiError("boom", status_code=503, body={})
    assert await provision_runner._seed_owner_paperclip("user_p") == provision_runner.EXIT_TRANSIENT


async def test_seed_helper_arm_4xx_exits_10(monkeypatch, _stub_arm_nightly_ceo):
    """4xx → EXIT_DETERMINISTIC."""
    from core.services.paperclip_admin_client import PaperclipApiError

    _stub_personal_seed(monkeypatch)
    _stub_arm_nightly_ceo.side_effect = PaperclipApiError("bad", status_code=400, body={})
    assert await provision_runner._seed_owner_paperclip("user_p") == provision_runner.EXIT_DETERMINISTIC


async def test_seed_helper_personal_calls_ensure_paperclip_for_owner(monkeypatch):
    """Personal owner_id: owner_email resolved via lookup_owner_email, then
    _ensure_paperclip_for_owner with owner_id == owner_user_id, org_id=None."""
    ensure_owner = AsyncMock()
    monkeypatch.setattr("core.services.user_provision._ensure_paperclip_for_owner", ensure_owner)
    monkeypatch.setattr(
        "core.services.paperclip_owner_email.lookup_owner_email",
        AsyncMock(return_value="p@example.com"),
    )
    assert await provision_runner._seed_owner_paperclip("user_p") == provision_runner.EXIT_OK
    ensure_owner.assert_awaited_once_with(
        owner_id="user_p",
        owner_user_id="user_p",
        owner_email="p@example.com",
        org_id=None,
    )


async def test_seed_helper_org_resolves_creator_then_calls_ensure_paperclip_for_org(monkeypatch):
    """Org owner_id (org_...): the runner has only owner_id (dispatch drops the
    triggering user), so the bootstrap identity is the org CREATOR resolved from
    Clerk (created_by), always an admin, with its email resolved from the same
    fallback the helpers use."""
    ensure_org = AsyncMock()
    monkeypatch.setattr("core.services.user_provision._ensure_paperclip_for_org", ensure_org)
    monkeypatch.setattr(
        "core.services.clerk_admin.get_organization",
        AsyncMock(return_value={"created_by": "user_creator"}),
    )
    look = AsyncMock(return_value="creator@example.com")
    monkeypatch.setattr("core.services.paperclip_owner_email.lookup_owner_email", look)
    assert await provision_runner._seed_owner_paperclip("org_xyz") == provision_runner.EXIT_OK
    ensure_org.assert_awaited_once_with(
        org_id="org_xyz",
        user_id="user_creator",
        email="creator@example.com",
        is_admin=True,
    )
    look.assert_awaited_once_with(org_id="org_xyz", fallback_user_id="user_creator")


async def test_seed_helper_org_no_resolvable_creator_is_transient(monkeypatch):
    """No Clerk creator (org lookup returns None / a Clerk blip): defer as
    TRANSIENT so SFN retries rather than failing the provision on a blip. The
    seed helper is never entered."""
    monkeypatch.setattr("core.services.clerk_admin.get_organization", AsyncMock(return_value=None))
    ensure_org = AsyncMock()
    monkeypatch.setattr("core.services.user_provision._ensure_paperclip_for_org", ensure_org)
    assert await provision_runner._seed_owner_paperclip("org_xyz") == provision_runner.EXIT_TRANSIENT
    ensure_org.assert_not_awaited()


async def test_seed_helper_deterministic_paperclip_4xx_exits_10(monkeypatch):
    """A non-409/422 Paperclip 4xx that propagates out of the helper classifies
    DETERMINISTIC → EXIT_DETERMINISTIC (SFN stops, surfaced not looped)."""
    from core.services.paperclip_admin_client import PaperclipApiError

    monkeypatch.setattr(
        "core.services.paperclip_owner_email.lookup_owner_email",
        AsyncMock(return_value="p@example.com"),
    )
    monkeypatch.setattr(
        "core.services.user_provision._ensure_paperclip_for_owner",
        AsyncMock(side_effect=PaperclipApiError("bad request", status_code=400, body={})),
    )
    assert await provision_runner._seed_owner_paperclip("user_p") == provision_runner.EXIT_DETERMINISTIC


async def test_seed_helper_transient_paperclip_5xx_exits_11(monkeypatch):
    """A Paperclip 5xx classifies TRANSIENT → EXIT_TRANSIENT (SFN retries)."""
    from core.services.paperclip_admin_client import PaperclipApiError

    monkeypatch.setattr(
        "core.services.paperclip_owner_email.lookup_owner_email",
        AsyncMock(return_value="p@example.com"),
    )
    monkeypatch.setattr(
        "core.services.user_provision._ensure_paperclip_for_owner",
        AsyncMock(side_effect=PaperclipApiError("boom", status_code=503, body={})),
    )
    assert await provision_runner._seed_owner_paperclip("user_p") == provision_runner.EXIT_TRANSIENT


async def test_seed_helper_transient_connection_error_exits_11(monkeypatch):
    """A transport/connection error (not a Paperclip status) defaults TRANSIENT
    → EXIT_TRANSIENT, same as the container path's classifier."""
    import httpx

    monkeypatch.setattr(
        "core.services.paperclip_owner_email.lookup_owner_email",
        AsyncMock(return_value="p@example.com"),
    )
    monkeypatch.setattr(
        "core.services.user_provision._ensure_paperclip_for_owner",
        AsyncMock(side_effect=httpx.ConnectError("connection refused")),
    )
    assert await provision_runner._seed_owner_paperclip("user_p") == provision_runner.EXIT_TRANSIENT
