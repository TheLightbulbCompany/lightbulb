"""Tests for scripts/provision_runner.py exit-code mapping."""

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


async def test_running_row_exits_zero(monkeypatch, manager):
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
    )
    manager.wait_for_running.assert_awaited_once_with("user_a")


async def test_superseded_exits_20_without_row_read(monkeypatch, manager):
    manager.provision_user_container.side_effect = ProvisionSuperseded("taken over")
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_SUPERSEDED


async def test_deterministic_error_exits_10(monkeypatch, manager):
    manager.provision_user_container.side_effect = Exception(
        "byo_provider is required when provider_choice == 'byo_key'"
    )
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_DETERMINISTIC


async def test_transient_error_exits_11(monkeypatch, manager):
    manager.provision_user_container.side_effect = Exception("ECS ThrottlingException")
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_TRANSIENT


async def test_error_row_maps_by_retryable_field(monkeypatch, manager):
    _row(monkeypatch, {"status": "error", "retryable": False})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_DETERMINISTIC
    _row(monkeypatch, {"status": "error", "retryable": True})
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_TRANSIENT


async def test_external_state_change_exits_zero(monkeypatch, manager):
    _row(monkeypatch, {"status": "stopped"})  # admin stop mid-wait: not ours to fight
    assert await provision_runner.run("user_a", "claim") == provision_runner.EXIT_OK
