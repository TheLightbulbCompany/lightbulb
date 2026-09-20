"""Tests for scripts/provision_runner.py exit-code ABI + fencing plumbing.

There is ONE leg: prepare the workspace, create the AgentCore runtime. The ECS-only coverage
(provision_user_container / wait_for_running / the post-wait row status mapping) went with that
runtime, and so did the ``runtime_host`` hint step 0 used to carry.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.repositories.container_repo import ProvisionSuperseded
from scripts import provision_runner

BACKEND_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def hermes(monkeypatch):
    """run()'s only leg.

    Step 0 (the owner Paperclip seed) is unconditional, so every run() test traverses it.
    Neutralize it here so the runtime-work tests below stay about the runtime work; the step-0
    tests re-patch it with their own double, and the _seed_owner_paperclip helper tests never
    take this fixture (they call the real function directly).
    """
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", AsyncMock(return_value=provision_runner.EXIT_OK))
    prepare = AsyncMock()
    provision = AsyncMock(return_value="arn:hermes-1")
    monkeypatch.setattr(provision_runner.hermes_provisioning, "prepare_hermes_workspace", prepare)
    monkeypatch.setattr(provision_runner.hermes_provisioning, "provision_hermes_runtime", provision)
    monkeypatch.setattr(provision_runner, "_arm_nightly_ceo_after_ready", AsyncMock())
    return SimpleNamespace(prepare=prepare, provision=provision)


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


async def test_first_action_is_claim_attempt(monkeypatch, hermes, repo):
    """The runner's FIRST checkpoint, before the workspace prepare, before
    provision_hermes_runtime -- fences out a straggler from a superseded
    attempt (spec §7 hazard 1) as early as possible."""
    await provision_runner.run("org_a", "claim")
    repo.claim_attempt.assert_awaited_once_with("org_a", "claim", "claim")


async def test_attempt_token_from_env_overrides_claim_id_fallback(monkeypatch, hermes, repo):
    monkeypatch.setenv("PROVISION_ATTEMPT_TOKEN", "fresh-retry-token")
    await provision_runner.run("org_a", "claim")
    repo.claim_attempt.assert_awaited_once_with("org_a", "claim", "fresh-retry-token")
    # The claim_id/attempt_token pass-through is the fencing seam: the runner must thread them
    # into the AgentCore leg's fenced writes unchanged, or they fence out its own run.
    hermes.prepare.assert_awaited_once_with("org_a", claim_id="claim", attempt_token="fresh-retry-token")
    hermes.provision.assert_awaited_once_with("org_a", claim_id="claim", attempt_token="fresh-retry-token")


async def test_claim_attempt_superseded_exits_20_before_any_provisioning(monkeypatch, hermes, repo):
    """A straggler whose claim was taken over entirely (not just its attempt)
    must stop at the very first checkpoint -- never touch the runtime."""
    repo.claim_attempt = AsyncMock(side_effect=ProvisionSuperseded("taken over"))
    assert await provision_runner.run("org_a", "claim") == provision_runner.EXIT_SUPERSEDED
    hermes.prepare.assert_not_awaited()
    hermes.provision.assert_not_awaited()


async def test_superseded_exits_20(monkeypatch, hermes, repo):
    hermes.provision.side_effect = ProvisionSuperseded("taken over")
    assert await provision_runner.run("org_a", "claim") == provision_runner.EXIT_SUPERSEDED


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


async def test_superseded_with_successor_claim_stands_down_quietly(monkeypatch, hermes, repo):
    """A claim on the row == a real successor. The quiet exit stays quiet —
    and the runner must NOT second-guess the successor with a release."""
    hermes.provision.side_effect = ProvisionSuperseded("taken over")
    repo.get_by_owner_id = AsyncMock(return_value={"status": "provisioning", "provision_claim_id": "claim-successor"})
    assert await provision_runner.run("org_a", "claim") == provision_runner.EXIT_SUPERSEDED
    repo.release_stranded_stop_to_error.assert_not_awaited()


async def test_superseded_with_row_gone_stands_down_quietly(monkeypatch, hermes, repo):
    """No row at all (deliberate delete) — nothing to release. (The repo
    fixture's get_by_owner_id defaults to None, so this is also the shape the
    older superseded tests above run through.)"""
    hermes.provision.side_effect = ProvisionSuperseded("taken over")
    assert await provision_runner.run("org_a", "claim") == provision_runner.EXIT_SUPERSEDED
    repo.release_stranded_stop_to_error.assert_not_awaited()


async def test_superseded_stranded_stopped_row_is_released_to_error(monkeypatch, hermes, repo):
    """THE strand: superseded, but the re-read shows a stopped row with NO
    claim — nobody running, nothing scheduled. The runner must release it to
    error(retryable) via the CAS so pollers can recover, and still exit 20
    (the machine ABI is unchanged; the row write is the recovery arm)."""
    hermes.provision.side_effect = ProvisionSuperseded("clobbered")
    repo.get_by_owner_id = AsyncMock(return_value={"status": "stopped"})
    assert await provision_runner.run("org_a", "claim") == provision_runner.EXIT_SUPERSEDED
    repo.release_stranded_stop_to_error.assert_awaited_once()
    assert repo.release_stranded_stop_to_error.await_args.args == ("org_a",)
    assert "no successor" in repo.release_stranded_stop_to_error.await_args.kwargs["last_error"]


async def test_superseded_unclaimed_provisioning_row_is_not_touched(monkeypatch, hermes, repo):
    """An UNCLAIMED provisioning row belongs to a wake/resize transition
    (those paths never claim — see main.py's boot resume). The runner logs and
    stands down; flipping it to error would clobber their in-process poller."""
    hermes.provision.side_effect = ProvisionSuperseded("taken over")
    repo.get_by_owner_id = AsyncMock(return_value={"status": "provisioning"})
    assert await provision_runner.run("org_a", "claim") == provision_runner.EXIT_SUPERSEDED
    repo.release_stranded_stop_to_error.assert_not_awaited()


async def test_superseded_release_failure_still_exits_20(monkeypatch, hermes, repo):
    """The stranded-row release is best-effort: a DDB blip there must not
    change the exit-code ABI (the machine still classifies Superseded)."""
    hermes.provision.side_effect = ProvisionSuperseded("clobbered")
    repo.get_by_owner_id = AsyncMock(return_value={"status": "stopped"})
    repo.release_stranded_stop_to_error = AsyncMock(side_effect=RuntimeError("ddb down"))
    assert await provision_runner.run("org_a", "claim") == provision_runner.EXIT_SUPERSEDED


async def test_deterministic_error_exits_10(monkeypatch, hermes, repo):
    hermes.provision.side_effect = Exception("byo_provider is required when provider_choice == 'byo_key'")
    assert await provision_runner.run("org_a", "claim") == provision_runner.EXIT_DETERMINISTIC


async def test_transient_error_exits_11(monkeypatch, hermes, repo):
    hermes.provision.side_effect = Exception("ThrottlingException")
    assert await provision_runner.run("org_a", "claim") == provision_runner.EXIT_TRANSIENT


# ---------------------------------------------------------------------------
# Step 0: owner Paperclip seed under the executor single-flight
# ---------------------------------------------------------------------------
#
# The OWNER company-create seed runs in the runner, BEFORE the container work,
# so it inherits try_claim_provision's single-flight (one execution per owner)
# — closing the duplicate-company race (#1442) at its source. Unconditional:
# there is no flag and no poll-thread alternative.


async def test_seed_step_0_always_runs_then_provisions_runtime(monkeypatch, hermes, repo):
    """Seed OK → the runner continues to the runtime work (step 0 is an
    ADDITIONAL first step, not a replacement). The seed is unconditional and
    runs AFTER claim_attempt (a taken-over straggler stands down first)."""
    seed = AsyncMock(return_value=provision_runner.EXIT_OK)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    assert await provision_runner.run("org_a", "claim") == provision_runner.EXIT_OK
    seed.assert_awaited_once_with("org_a")
    repo.claim_attempt.assert_awaited_once()
    hermes.provision.assert_awaited_once()


async def test_seed_step_0_deterministic_fail_exits_10_skips_runtime_surfaces_error(monkeypatch, hermes, repo):
    """Seed deterministic-fail (10): exit 10 WITHOUT provisioning the runtime,
    and the row is flipped to error(retryable=False) so the poll SURFACES the
    failure instead of looping it."""
    seed = AsyncMock(return_value=provision_runner.EXIT_DETERMINISTIC)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    assert await provision_runner.run("org_a", "claim") == provision_runner.EXIT_DETERMINISTIC
    hermes.provision.assert_not_awaited()
    repo.release_provision_to_error.assert_awaited_once()
    kwargs = repo.release_provision_to_error.await_args.kwargs
    assert kwargs["retryable"] is False
    assert kwargs["attempt_token"] == "claim"


async def test_seed_step_0_transient_fail_exits_11_skips_runtime_retryable_error(monkeypatch, hermes, repo):
    """Seed transient-fail (11): exit 11 (SFN retries) WITHOUT the runtime,
    row flipped to error(retryable=True) for the poll's auto-retry."""
    seed = AsyncMock(return_value=provision_runner.EXIT_TRANSIENT)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    assert await provision_runner.run("org_a", "claim") == provision_runner.EXIT_TRANSIENT
    hermes.provision.assert_not_awaited()
    repo.release_provision_to_error.assert_awaited_once()
    assert repo.release_provision_to_error.await_args.kwargs["retryable"] is True


async def test_seed_step_0_error_release_failure_still_returns_exit_code(monkeypatch, hermes, repo):
    """The container-row error release is best-effort: a DDB blip there must
    not change the exit-code ABI the machine reads."""
    seed = AsyncMock(return_value=provision_runner.EXIT_DETERMINISTIC)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    repo.release_provision_to_error = AsyncMock(side_effect=RuntimeError("ddb down"))
    assert await provision_runner.run("org_a", "claim") == provision_runner.EXIT_DETERMINISTIC
    hermes.provision.assert_not_awaited()


async def test_seed_step_0_superseded_claim_stands_down_before_seeding(monkeypatch, hermes, repo):
    """A straggler whose claim was taken over stops at claim_attempt — the seed
    (like the runtime work) never runs, and the runner exits 20."""
    seed = AsyncMock(return_value=provision_runner.EXIT_OK)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    repo.claim_attempt = AsyncMock(side_effect=ProvisionSuperseded("taken over"))
    assert await provision_runner.run("org_a", "claim") == provision_runner.EXIT_SUPERSEDED
    seed.assert_not_awaited()
    hermes.provision.assert_not_awaited()


# ---- _seed_owner_paperclip direct tests: org/email resolution + classification ----


@pytest.fixture(autouse=True)
def _stub_arm_nightly_ceo(monkeypatch):
    """Run-level post-ready autonomy is covered separately."""
    arm = AsyncMock(return_value="armed")
    monkeypatch.setattr(provision_runner, "_arm_nightly_ceo_after_ready", arm)
    return arm


def _stub_org_seed(monkeypatch, seed=None):
    """Patch the org seed's collaborators: the Clerk creator lookup, the owner
    email lookup, and ``_ensure_paperclip_for_org`` itself (returned)."""
    seed = seed or AsyncMock()
    monkeypatch.setattr("core.services.user_provision._ensure_paperclip_for_org", seed)
    monkeypatch.setattr(
        "core.services.clerk_admin.get_organization",
        AsyncMock(return_value={"created_by": "user_c"}),
    )
    monkeypatch.setattr(
        "core.services.paperclip_owner_email.lookup_owner_email",
        AsyncMock(return_value="c@example.com"),
    )
    return seed


async def test_seed_helper_refuses_non_org_owner(monkeypatch, _stub_arm_nightly_ceo):
    """Personal workspaces no longer exist: a user_ owner is refused with
    EXIT_DETERMINISTIC before Clerk, the seed or the arm is touched — the
    runner then flips the row to error(retryable=False) instead of seeding a
    personal company."""
    seed = _stub_org_seed(monkeypatch)
    get_org = AsyncMock()
    monkeypatch.setattr("core.services.clerk_admin.get_organization", get_org)
    assert await provision_runner._seed_owner_paperclip("user_p") == provision_runner.EXIT_DETERMINISTIC
    get_org.assert_not_awaited()
    seed.assert_not_awaited()
    _stub_arm_nightly_ceo.assert_not_awaited()


async def test_seed_helper_seeds_org_without_arming_unclocked_trial(monkeypatch, _stub_arm_nightly_ceo):
    seed = _stub_org_seed(monkeypatch)
    assert await provision_runner._seed_owner_paperclip("org_x") == provision_runner.EXIT_OK
    seed.assert_awaited_once()
    _stub_arm_nightly_ceo.assert_not_awaited()


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

    _stub_org_seed(monkeypatch, AsyncMock(side_effect=PaperclipApiError("bad request", status_code=400, body={})))
    assert await provision_runner._seed_owner_paperclip("org_p") == provision_runner.EXIT_DETERMINISTIC


async def test_seed_helper_transient_paperclip_5xx_exits_11(monkeypatch):
    """A Paperclip 5xx classifies TRANSIENT → EXIT_TRANSIENT (SFN retries)."""
    from core.services.paperclip_admin_client import PaperclipApiError

    _stub_org_seed(monkeypatch, AsyncMock(side_effect=PaperclipApiError("boom", status_code=503, body={})))
    assert await provision_runner._seed_owner_paperclip("org_p") == provision_runner.EXIT_TRANSIENT


async def test_seed_helper_transient_connection_error_exits_11(monkeypatch):
    """A transport/connection error (not a Paperclip status) defaults TRANSIENT
    → EXIT_TRANSIENT, same as the container path's classifier."""
    import httpx

    _stub_org_seed(monkeypatch, AsyncMock(side_effect=httpx.ConnectError("connection refused")))
    assert await provision_runner._seed_owner_paperclip("org_p") == provision_runner.EXIT_TRANSIENT
