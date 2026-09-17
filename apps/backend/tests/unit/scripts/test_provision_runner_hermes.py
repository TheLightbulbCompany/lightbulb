"""Tests for step 1 dispatching to AgentCore behind ``settings.HERMES_RUNTIME``
(scripts/provision_runner.py `run()`), and for the runtime_host hint threaded
into step 0's Paperclip seed (M4-R8 / M1 Ruling 10) so a FRESH Hermes provision
seeds every starter agent onto the `http` adapter before the containers row
exists to read `runtime_host` from.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import provision_runner


async def _ok(owner_id, **kwargs):
    return provision_runner.EXIT_OK


async def _refuses_non_org(owner_id, **kwargs):
    return provision_runner.EXIT_DETERMINISTIC


def _explodes_if_called(*args, **kwargs):
    raise AssertionError(f"should not have been called: args={args!r} kwargs={kwargs!r}")


@pytest.fixture
def runner_env(monkeypatch):
    """Enough of run()'s dependencies wired for a full run() call — mirrors the
    ``manager``/``repo`` fixtures in test_provision_runner.py, so these tests can
    call run() end-to-end rather than mocking around it."""
    mgr = MagicMock()
    mgr.provision_user_container = AsyncMock(return_value="svc-name")
    mgr.wait_for_running = AsyncMock()
    mgr.resolve_running_container = AsyncMock(return_value=(None, None))
    mgr.is_healthy = MagicMock(return_value=False)
    mgr.has_live_service = AsyncMock(return_value=True)
    monkeypatch.setattr(provision_runner, "_get_manager", lambda: mgr)
    monkeypatch.setattr(provision_runner, "_resolve_provider", AsyncMock(return_value=("bedrock_claude", None)))
    monkeypatch.setattr(provision_runner, "_get_row", AsyncMock(return_value={"status": "running"}))

    import core.repositories.container_repo as _repo_module

    monkeypatch.setattr(_repo_module, "claim_attempt", AsyncMock())
    monkeypatch.setattr(_repo_module, "get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(_repo_module, "release_stranded_stop_to_error", AsyncMock(return_value=True))
    monkeypatch.setattr(_repo_module, "release_provision_to_error", AsyncMock())
    return mgr


@pytest.mark.asyncio
async def test_with_the_flag_on_step_1_provisions_a_hermes_runtime(monkeypatch, runner_env):
    provision = AsyncMock(return_value="arn:r1")
    monkeypatch.setattr(provision_runner.settings, "HERMES_RUNTIME", True)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _ok)
    monkeypatch.setattr(provision_runner.hermes_provisioning, "provision_hermes_runtime", provision)
    monkeypatch.setattr(provision_runner, "_get_manager", _explodes_if_called)
    assert await provision_runner.run("org_a", "claim-1") == provision_runner.EXIT_OK
    # claim_id/attempt_token are threaded through for the fenced terminal write (M4-R28) --
    # no PROVISION_ATTEMPT_TOKEN env var here, so attempt_token falls back to claim_id.
    provision.assert_awaited_once_with("org_a", claim_id="claim-1", attempt_token="claim-1")


@pytest.mark.asyncio
async def test_with_the_flag_on_there_is_no_wait_for_running_poll(monkeypatch, runner_env):
    monkeypatch.setattr(provision_runner.settings, "HERMES_RUNTIME", True)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _ok)
    monkeypatch.setattr(
        provision_runner.hermes_provisioning, "provision_hermes_runtime", AsyncMock(return_value="arn:r1")
    )
    monkeypatch.setattr(provision_runner, "_get_manager", _explodes_if_called)
    await provision_runner.run("org_a", "claim-1")  # _get_manager would raise if the ECS leg ran


@pytest.mark.asyncio
async def test_step_0_still_runs_and_still_refuses_a_non_org_owner(monkeypatch, runner_env):
    monkeypatch.setattr(provision_runner.settings, "HERMES_RUNTIME", True)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _refuses_non_org)
    assert await provision_runner.run("user_a", "claim-1") == 10


@pytest.mark.asyncio
async def test_with_the_flag_off_the_ecs_leg_is_unchanged(monkeypatch, runner_env):
    monkeypatch.setattr(provision_runner.settings, "HERMES_RUNTIME", False)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _ok)
    monkeypatch.setattr(
        provision_runner.hermes_provisioning,
        "provision_hermes_runtime",
        _explodes_if_called,
    )
    await provision_runner.run("org_a", "claim-1")  # the ECS manager double is what runs


# --- M4-R8 / M1 Ruling 10: the runtime_host hint threaded into step 0 -------


@pytest.mark.asyncio
async def test_with_the_flag_on_step_0_is_seeded_with_the_agentcore_runtime_host_hint(monkeypatch, runner_env):
    """run() must pass runtime_host="agentcore" into _seed_owner_paperclip so the
    seed loop's adapter_type_for call picks the http adapter BEFORE the containers
    row carries runtime_host (the row doesn't exist yet at step 0)."""
    seed_calls = []

    async def _spy_seed(owner_id, *, runtime_host=None):
        seed_calls.append(runtime_host)
        return provision_runner.EXIT_OK

    monkeypatch.setattr(provision_runner.settings, "HERMES_RUNTIME", True)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _spy_seed)
    monkeypatch.setattr(
        provision_runner.hermes_provisioning, "provision_hermes_runtime", AsyncMock(return_value="arn:r1")
    )
    monkeypatch.setattr(provision_runner, "_get_manager", _explodes_if_called)

    await provision_runner.run("org_a", "claim-1")
    assert seed_calls == ["agentcore"]


@pytest.mark.asyncio
async def test_with_the_flag_off_step_0_gets_no_runtime_host_hint(monkeypatch, runner_env):
    """With the flag off, the hint is None — the ECS default, unchanged."""
    seed_calls = []

    async def _spy_seed(owner_id, *, runtime_host=None):
        seed_calls.append(runtime_host)
        return provision_runner.EXIT_OK

    monkeypatch.setattr(provision_runner.settings, "HERMES_RUNTIME", False)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _spy_seed)
    monkeypatch.setattr(
        provision_runner.hermes_provisioning,
        "provision_hermes_runtime",
        _explodes_if_called,
    )

    await provision_runner.run("org_a", "claim-1")
    assert seed_calls == [None]
