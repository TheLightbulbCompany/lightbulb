"""``scripts/provision_runner.run()`` — step 1 provisions the AgentCore runtime, and the
M4-R33 ordering fix: hook_token/gateway_token must exist BEFORE step 0 seeds the roster,
because the seed's adapter config bakes hook_token in as its ``apiKey`` at seed time.

The ``settings.HERMES_RUNTIME`` flag these tests were written around is deleted -- there is
one leg -- and so is the ECS manager they stubbed to prove the other one never ran.
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
    """Enough of run()'s dependencies wired for a full run() call, so these tests can call
    run() end-to-end rather than mocking around it. ``container_repo`` is imported at
    function-call time inside run(), so the SOURCE module is what gets patched."""
    import core.repositories.container_repo as _repo_module

    monkeypatch.setattr(_repo_module, "claim_attempt", AsyncMock())
    monkeypatch.setattr(_repo_module, "get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(_repo_module, "release_stranded_stop_to_error", AsyncMock(return_value=True))
    monkeypatch.setattr(_repo_module, "release_provision_to_error", AsyncMock())
    monkeypatch.setattr(provision_runner, "_arm_nightly_ceo_after_ready", AsyncMock())


@pytest.mark.asyncio
async def test_step_1_provisions_the_agentcore_runtime(monkeypatch, runner_env):
    prepare = AsyncMock()
    provision = AsyncMock(return_value="arn:r1")
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _ok)
    monkeypatch.setattr(provision_runner.hermes_provisioning, "prepare_hermes_workspace", prepare)
    monkeypatch.setattr(provision_runner.hermes_provisioning, "provision_hermes_runtime", provision)
    assert await provision_runner.run("org_a", "claim-1") == provision_runner.EXIT_OK
    # claim_id/attempt_token are threaded through for the fenced writes (M4-R28/R33) -- no
    # PROVISION_ATTEMPT_TOKEN env var here, so attempt_token falls back to claim_id.
    prepare.assert_awaited_once_with("org_a", claim_id="claim-1", attempt_token="claim-1")
    provision.assert_awaited_once_with("org_a", claim_id="claim-1", attempt_token="claim-1")


@pytest.mark.asyncio
async def test_nightly_ceo_is_armed_only_after_the_runtime_is_ready(monkeypatch, runner_env):
    events = []

    async def provision(*args, **kwargs):
        events.append("ready")

    async def arm(*args, **kwargs):
        events.append("armed")

    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _ok)
    monkeypatch.setattr(provision_runner.hermes_provisioning, "prepare_hermes_workspace", AsyncMock())
    monkeypatch.setattr(provision_runner.hermes_provisioning, "provision_hermes_runtime", provision)
    monkeypatch.setattr(provision_runner, "_arm_nightly_ceo_after_ready", arm)

    assert await provision_runner.run("org_a", "claim-1") == provision_runner.EXIT_OK
    assert events == ["ready", "armed"]


@pytest.mark.asyncio
async def test_there_is_no_boot_poll_to_wait_on(monkeypatch, runner_env):
    """CreateAgentRuntime + a short READY poll inside provision_hermes_runtime replaces the
    SFN-watched, many-minutes ECS boot wait run() used to own in the foreground."""
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _ok)
    monkeypatch.setattr(provision_runner.hermes_provisioning, "prepare_hermes_workspace", AsyncMock())
    monkeypatch.setattr(
        provision_runner.hermes_provisioning, "provision_hermes_runtime", AsyncMock(return_value="arn:r1")
    )
    await provision_runner.run("org_a", "claim-1")


@pytest.mark.asyncio
async def test_step_0_still_runs_and_still_refuses_a_non_org_owner(monkeypatch, runner_env):
    monkeypatch.setattr(provision_runner.hermes_provisioning, "prepare_hermes_workspace", AsyncMock())
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _refuses_non_org)
    assert await provision_runner.run("user_a", "claim-1") == 10


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

    async def _fake_prepare(owner_id, *, claim_id, attempt_token=None):
        row["hook_token"] = "minted-hook-token"
        row["gateway_token"] = "minted-gateway-token"

    hook_token_seen_at_seed_time = {}

    async def _spy_seed(owner_id, *, runtime_host=None):
        current = await repo_mod.get_by_owner_id(owner_id)
        hook_token_seen_at_seed_time["value"] = (current or {}).get("hook_token")
        return provision_runner.EXIT_OK

    monkeypatch.setattr(repo_mod, "get_by_owner_id", _fake_get_by_owner_id)
    monkeypatch.setattr(provision_runner.hermes_provisioning, "prepare_hermes_workspace", _fake_prepare)
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _spy_seed)
    monkeypatch.setattr(
        provision_runner.hermes_provisioning, "provision_hermes_runtime", AsyncMock(return_value="arn:r1")
    )

    await provision_runner.run("org_a", "claim-1")
    assert hook_token_seen_at_seed_time["value"] == "minted-hook-token"


# --- C2/M4-R36: the Hermes leg releases the lease to error on failure -----------------------


@pytest.mark.asyncio
async def test_a_provision_hermes_runtime_failure_releases_the_lease_to_error(monkeypatch, runner_env):
    monkeypatch.setattr(provision_runner.hermes_provisioning, "prepare_hermes_workspace", AsyncMock())
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _ok)
    monkeypatch.setattr(
        provision_runner.hermes_provisioning,
        "provision_hermes_runtime",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    released = {}

    async def _fake_release(
        owner_id, claim_id, *, last_error=None, retryable=True, attempt_token=None, generation=None
    ):
        released.update(
            owner_id=owner_id,
            claim_id=claim_id,
            last_error=last_error,
            retryable=retryable,
            attempt_token=attempt_token,
        )

    import core.repositories.container_repo as repo_mod

    monkeypatch.setattr(repo_mod, "release_provision_to_error", _fake_release)

    exit_code = await provision_runner.run("org_a", "claim-1")
    assert exit_code != provision_runner.EXIT_OK
    assert released["owner_id"] == "org_a"
    assert released["claim_id"] == "claim-1"
    assert released["attempt_token"] == "claim-1"
    assert "boom" in released["last_error"]


@pytest.mark.asyncio
async def test_a_prepare_hermes_workspace_failure_releases_the_lease_to_error(monkeypatch, runner_env):
    monkeypatch.setattr(
        provision_runner.hermes_provisioning, "prepare_hermes_workspace", AsyncMock(side_effect=RuntimeError("boom"))
    )
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _explodes_if_called)  # must never be reached
    monkeypatch.setattr(provision_runner.hermes_provisioning, "provision_hermes_runtime", _explodes_if_called)

    released = {}

    async def _fake_release(
        owner_id, claim_id, *, last_error=None, retryable=True, attempt_token=None, generation=None
    ):
        released.update(owner_id=owner_id, claim_id=claim_id, last_error=last_error)

    import core.repositories.container_repo as repo_mod

    monkeypatch.setattr(repo_mod, "release_provision_to_error", _fake_release)

    exit_code = await provision_runner.run("org_a", "claim-1")
    assert exit_code != provision_runner.EXIT_OK
    assert released["owner_id"] == "org_a"
    assert "boom" in released["last_error"]


@pytest.mark.asyncio
async def test_a_superseded_hermes_leg_stands_down_quietly_not_as_an_error(monkeypatch, runner_env):
    """ProvisionSuperseded must never be treated as a failure to release -- a takeover already
    owns the row."""
    from core.repositories.container_repo import ProvisionSuperseded

    monkeypatch.setattr(provision_runner.hermes_provisioning, "prepare_hermes_workspace", AsyncMock())
    monkeypatch.setattr(provision_runner, "_seed_owner_paperclip", _ok)
    monkeypatch.setattr(
        provision_runner.hermes_provisioning,
        "provision_hermes_runtime",
        AsyncMock(side_effect=ProvisionSuperseded("stale")),
    )

    import core.repositories.container_repo as repo_mod

    release = AsyncMock()
    monkeypatch.setattr(repo_mod, "release_provision_to_error", release)
    # _stand_down_superseded re-reads the row to verify who owns it -- absent row (the
    # runner_env default) is the "gone/real successor" branch, which exits quietly.
    monkeypatch.setattr(repo_mod, "get_by_owner_id", AsyncMock(return_value=None))

    exit_code = await provision_runner.run("org_a", "claim-1")
    assert exit_code == provision_runner.EXIT_SUPERSEDED
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_ready_nightly_arm_checks_the_actual_routine(monkeypatch):
    from core.services import user_provision

    arm = AsyncMock()
    monkeypatch.setattr(user_provision, "arm_nightly_ceo", arm)
    await provision_runner._arm_nightly_ceo_after_ready("org_test")
    arm.assert_awaited_once_with("org_test", trust_cache=False)


@pytest.mark.asyncio
async def test_retry_after_ready_repairs_the_nightly_routine_without_reprovision(monkeypatch, runner_env):
    from core.repositories import container_repo

    monkeypatch.setattr(container_repo, "claim_attempt", AsyncMock(side_effect=container_repo.ProvisionSuperseded()))
    monkeypatch.setattr(
        container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"status": "running", "agentcore_runtime_arn": "arn:ready"}),
    )
    prepare = AsyncMock()
    arm = AsyncMock()
    monkeypatch.setattr(provision_runner.hermes_provisioning, "prepare_hermes_workspace", prepare)
    monkeypatch.setattr(provision_runner, "_arm_nightly_ceo_after_ready", arm)
    assert await provision_runner.run("org_test", "finished-claim") == provision_runner.EXIT_SUPERSEDED
    prepare.assert_not_awaited()
    arm.assert_awaited_once_with("org_test")
