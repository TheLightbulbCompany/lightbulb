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


async def test_superseded_exits_20_without_row_read(monkeypatch, manager, repo):
    manager.provision_user_container.side_effect = ProvisionSuperseded("taken over")
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_SUPERSEDED


async def test_wait_for_running_superseded_exits_20(monkeypatch, manager, repo):
    """The fenced write inside wait_for_running (via _await_running_transition)
    can ALSO raise ProvisionSuperseded -- e.g. a straggler that got past its
    provision_user_container checkpoints but loses the race on the terminal
    running/error write."""
    manager.wait_for_running.side_effect = ProvisionSuperseded("taken over mid-wait")
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
