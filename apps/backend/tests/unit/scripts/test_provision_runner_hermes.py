"""``scripts/provision_runner.run()`` — the M4-R33 ordering fix: hook_token/gateway_token
must exist BEFORE step 0 seeds the roster, because the seed's adapter config bakes hook_token
in as its ``apiKey`` at seed time — plus the Hermes legs' failure recording.
"""

from unittest.mock import AsyncMock

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
    """Enough of run()'s dependencies wired for a full run() call. ``container_repo`` is
    imported at function-call time inside run(), so the SOURCE module is what gets patched."""
    import core.repositories.container_repo as _repo_module

    monkeypatch.setattr(_repo_module, "mark_provision_error", AsyncMock())
    monkeypatch.setattr(_repo_module, "note_provision_error", AsyncMock())
    monkeypatch.setattr(_repo_module, "get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr("core.services.onboarding_brief.reconcile_pending_brief", AsyncMock())
    monkeypatch.setattr("core.repositories.billing_repo.arm_trial_if_unset", AsyncMock())
    return _repo_module


@pytest.mark.asyncio
async def test_step_0_still_runs_and_still_refuses_a_non_org_owner(monkeypatch, runner_env):
    monkeypatch.setattr(provision_runner.hermes_provisioning, "prepare_hermes_workspace", AsyncMock())
    monkeypatch.setattr(provision_runner.hermes_provisioning, "provision_hermes_runtime", _explodes_if_called)
    monkeypatch.setattr(provision_runner, "_seed_owner_workspace", _refuses_non_org)
    assert await provision_runner.run("user_a") == 10
    assert "not a workspace" in runner_env.mark_provision_error.await_args.kwargs["last_error"]


# --- M4-R33: hook_token must exist BEFORE step 0 seeds the roster (the exact bug) ----------


@pytest.mark.asyncio
async def test_hook_token_exists_before_step_0_seeds_the_roster(monkeypatch, runner_env):
    """The exact bug: a fresh Hermes provision's step-0 seed must see a REAL hook_token on the
    row (not the empty-string fallback), because build_hermes_gateway_adapter_config bakes it
    in as the native adapter's ``apiKey`` at seed time -- and step 0 used to run before
    anything minted one. It now REFUSES the empty string outright (AdapterConfigError), so the
    failure is loud rather than a roster of agents whose every run 401s."""
    import core.repositories.container_repo as repo_mod

    row: dict = {}

    async def _fake_get_by_owner_id(owner_id, *, consistent=False):
        return dict(row)

    async def _fake_prepare(owner_id):
        row["hook_token"] = "minted-hook-token"
        row["gateway_token"] = "minted-gateway-token"

    hook_token_seen_at_seed_time = {}

    async def _spy_seed(owner_id):
        current = await repo_mod.get_by_owner_id(owner_id)
        hook_token_seen_at_seed_time["value"] = (current or {}).get("hook_token")
        return provision_runner.EXIT_OK

    monkeypatch.setattr(repo_mod, "get_by_owner_id", _fake_get_by_owner_id)
    monkeypatch.setattr(provision_runner.hermes_provisioning, "prepare_hermes_workspace", _fake_prepare)
    monkeypatch.setattr(provision_runner, "_seed_owner_workspace", _spy_seed)
    monkeypatch.setattr(
        provision_runner.hermes_provisioning, "provision_hermes_runtime", AsyncMock(return_value="arn:r1")
    )
    monkeypatch.setattr(provision_runner.hermes_provisioning, "reconcile_runtime", AsyncMock(return_value="current"))

    await provision_runner.run("org_a")
    assert hook_token_seen_at_seed_time["value"] == "minted-hook-token"


# --- C2/M4-R36: a Hermes-leg failure is recorded on the row, never an open spinner ----------


@pytest.mark.asyncio
async def test_a_transient_prepare_hermes_workspace_failure_stops_the_run_without_an_error_row(monkeypatch, runner_env):
    monkeypatch.setattr(
        provision_runner.hermes_provisioning, "prepare_hermes_workspace", AsyncMock(side_effect=RuntimeError("boom"))
    )
    monkeypatch.setattr(provision_runner, "_seed_owner_workspace", _explodes_if_called)  # must never be reached
    monkeypatch.setattr(provision_runner.hermes_provisioning, "provision_hermes_runtime", _explodes_if_called)

    # Transient: the machine retries it, so no error row is written (MarkFailedExhausted is the backstop).
    assert await provision_runner.run("org_a") == provision_runner.EXIT_TRANSIENT
    runner_env.mark_provision_error.assert_not_awaited()
    # ...but the reason is recorded (status untouched) so MarkFailedExhausted leaves it showing.
    runner_env.note_provision_error.assert_awaited_once()
    assert "boom" in runner_env.note_provision_error.await_args.args[1]
