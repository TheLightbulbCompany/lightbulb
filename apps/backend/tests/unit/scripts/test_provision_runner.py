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
    monkeypatch.setattr(provision_runner, "_get_manager", lambda: mgr)
    monkeypatch.setattr(
        provision_runner,
        "_resolve_provider",
        AsyncMock(return_value=("bedrock_claude", None)),
    )
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
# Step 0: owner Paperclip seed under the executor single-flight (SEED_IN_EXECUTOR)
# ---------------------------------------------------------------------------
#
# When the flag is ON the OWNER company-create seed runs in the runner, BEFORE
# the container work, so it inherits try_claim_provision's single-flight (one
# execution per owner) — closing the duplicate-company race (#1442) at its
# source. Default OFF: the seed never runs here (poll thread owns it), so every
# test above (which does not touch the flag) is a flag-OFF regression proof.


async def test_seed_step_0_not_run_when_flag_off(monkeypatch, manager, repo):
    """Default (flag OFF): the runner NEVER seeds — byte-identical to today.
    The poll thread owns the seed; the runner only does the container work."""
    seed = AsyncMock(return_value=provision_runner.EXIT_OK)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    monkeypatch.setattr(provision_runner.settings, "SEED_IN_EXECUTOR", False)
    _row(monkeypatch, {"status": "running"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK
    seed.assert_not_awaited()
    manager.provision_user_container.assert_awaited_once()


async def test_seed_step_0_success_then_provisions_container(monkeypatch, manager, repo):
    """Flag ON, seed OK → the runner continues to the container work exactly as
    before (step 0 is an ADDITIONAL first step, not a replacement), and the seed
    runs AFTER claim_attempt (a taken-over straggler stands down first)."""
    seed = AsyncMock(return_value=provision_runner.EXIT_OK)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    monkeypatch.setattr(provision_runner.settings, "SEED_IN_EXECUTOR", True)
    _row(monkeypatch, {"status": "running"})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK
    seed.assert_awaited_once_with("user_a")
    repo.claim_attempt.assert_awaited_once()
    manager.provision_user_container.assert_awaited_once()


async def test_seed_step_0_deterministic_fail_exits_10_skips_container_surfaces_error(monkeypatch, manager, repo):
    """Flag ON, seed deterministic-fail (10): exit 10 WITHOUT touching the
    container, and the row is flipped to error(retryable=False) so the poll
    SURFACES the failure instead of looping it."""
    seed = AsyncMock(return_value=provision_runner.EXIT_DETERMINISTIC)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    monkeypatch.setattr(provision_runner.settings, "SEED_IN_EXECUTOR", True)
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_DETERMINISTIC
    manager.provision_user_container.assert_not_awaited()
    repo.release_provision_to_error.assert_awaited_once()
    kwargs = repo.release_provision_to_error.await_args.kwargs
    assert kwargs["retryable"] is False
    assert kwargs["attempt_token"] == "claim"


async def test_seed_step_0_transient_fail_exits_11_skips_container_retryable_error(monkeypatch, manager, repo):
    """Flag ON, seed transient-fail (11): exit 11 (SFN retries) WITHOUT the
    container, row flipped to error(retryable=True) for the poll's auto-retry."""
    seed = AsyncMock(return_value=provision_runner.EXIT_TRANSIENT)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    monkeypatch.setattr(provision_runner.settings, "SEED_IN_EXECUTOR", True)
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_TRANSIENT
    manager.provision_user_container.assert_not_awaited()
    repo.release_provision_to_error.assert_awaited_once()
    assert repo.release_provision_to_error.await_args.kwargs["retryable"] is True


async def test_seed_step_0_error_release_failure_still_returns_exit_code(monkeypatch, manager, repo):
    """The container-row error release is best-effort: a DDB blip there must
    not change the exit-code ABI the machine reads."""
    seed = AsyncMock(return_value=provision_runner.EXIT_DETERMINISTIC)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    monkeypatch.setattr(provision_runner.settings, "SEED_IN_EXECUTOR", True)
    repo.release_provision_to_error = AsyncMock(side_effect=RuntimeError("ddb down"))
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_DETERMINISTIC
    manager.provision_user_container.assert_not_awaited()


async def test_seed_step_0_superseded_claim_stands_down_before_seeding(monkeypatch, manager, repo):
    """A straggler whose claim was taken over stops at claim_attempt — the seed
    (like the container work) never runs, and the runner exits 20."""
    seed = AsyncMock(return_value=provision_runner.EXIT_OK)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", seed)
    monkeypatch.setattr(provision_runner.settings, "SEED_IN_EXECUTOR", True)
    repo.claim_attempt = AsyncMock(side_effect=ProvisionSuperseded("taken over"))
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_SUPERSEDED
    seed.assert_not_awaited()
    manager.provision_user_container.assert_not_awaited()


# ---- _seed_owner_paperclip direct tests: org/email resolution + classification ----


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
