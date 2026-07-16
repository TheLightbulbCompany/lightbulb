"""Unit tests for the bedrock-gate cutover ops script.

What matters here is the dry-run-vs-apply gate (a dry run resolves the target
owners but writes NOTHING), the flag-set + reprovision fan-out on --apply, the
--rollback direction (sets the flag false), and cohort filtering that excludes
non-Bedrock owners (BYOK / ChatGPT-OAuth). The AWS/DDB writes themselves are
thin wrappers exercised manually with --apply and real creds, so they are
mocked at the repo / ecs-manager seam.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import scripts.bedrock_gate_cutover as mod


def test_module_imports_and_exposes_main():
    assert hasattr(mod, "main")
    assert mod.FLAG_ATTR == "bedrock_via_gateway"


# --------------------------------------------------------------------------- #
# _classify_plan() — pure cohort classification
# --------------------------------------------------------------------------- #


def test_classify_trial_local():
    assert mod._classify_plan({"subscription_status": "trial_local"}) == "trial"


def test_classify_paid_bedrock():
    assert mod._classify_plan({"provider_choice": "bedrock_claude"}) == "paid"


def test_classify_byok_is_none():
    assert mod._classify_plan({"provider_choice": "byo_key", "byo_provider": "anthropic"}) is None


def test_classify_chatgpt_oauth_is_none():
    assert mod._classify_plan({"provider_choice": "chatgpt_oauth"}) is None


def test_classify_no_billing_is_none():
    assert mod._classify_plan(None) is None
    assert mod._classify_plan({}) is None


# --------------------------------------------------------------------------- #
# main() — dry-run vs apply, with the write seams mocked
# --------------------------------------------------------------------------- #


def _patches(*, container=None, billing=None, scan_rows=None):
    """Patch the collaborators the script lazy-imports inside main.

    Returns (update_fields_mock, reprovision_mock, list_of_context_managers).
    """
    update_fields = AsyncMock()
    reprovision = AsyncMock()
    ecs_manager = MagicMock()
    ecs_manager.reprovision_for_user = reprovision

    cms = [
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            AsyncMock(return_value=container),
        ),
        patch(
            "core.repositories.container_repo.scan_all",
            AsyncMock(return_value=scan_rows or []),
        ),
        patch(
            "core.repositories.container_repo.update_fields",
            update_fields,
        ),
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            billing if isinstance(billing, AsyncMock) else AsyncMock(return_value=billing),
        ),
        patch(
            "core.containers.get_ecs_manager",
            MagicMock(return_value=ecs_manager),
        ),
    ]
    return update_fields, reprovision, cms


async def _run(cms, **kwargs):
    for cm in cms:
        cm.start()
    try:
        return await mod.main(**kwargs)
    finally:
        for cm in cms:
            cm.stop()


# ---- single owner --------------------------------------------------------- #


@pytest.mark.asyncio
async def test_single_owner_dry_run_writes_nothing():
    update_fields, reprovision, cms = _patches(
        container={"owner_id": "org_x"},
        billing={"subscription_status": "trial_local"},
    )
    rc = await _run(cms, apply=False, env="prod", owner="org_x")

    assert rc == 0
    update_fields.assert_not_called()
    reprovision.assert_not_called()


@pytest.mark.asyncio
async def test_single_owner_apply_sets_flag_true_and_reprovisions():
    update_fields, reprovision, cms = _patches(
        container={"owner_id": "org_x"},
        billing={"subscription_status": "trial_local"},
    )
    rc = await _run(cms, apply=True, env="prod", owner="org_x")

    assert rc == 0
    update_fields.assert_awaited_once_with("org_x", {"bedrock_via_gateway": True})
    reprovision.assert_awaited_once_with("org_x")


@pytest.mark.asyncio
async def test_single_owner_rollback_sets_flag_false():
    update_fields, reprovision, cms = _patches(
        container={"owner_id": "org_x"},
        billing={"provider_choice": "bedrock_claude"},
    )
    rc = await _run(cms, apply=True, env="prod", owner="org_x", rollback=True)

    assert rc == 0
    update_fields.assert_awaited_once_with("org_x", {"bedrock_via_gateway": False})
    reprovision.assert_awaited_once_with("org_x")


@pytest.mark.asyncio
async def test_single_owner_byok_is_refused_without_write():
    update_fields, reprovision, cms = _patches(
        container={"owner_id": "org_byok"},
        billing={"provider_choice": "byo_key"},
    )
    rc = await _run(cms, apply=True, env="prod", owner="org_byok")

    # Refused: BYOK owner has no Bedrock in its path.
    assert rc == 1
    update_fields.assert_not_called()
    reprovision.assert_not_called()


@pytest.mark.asyncio
async def test_single_owner_missing_container_refused_without_write():
    update_fields, reprovision, cms = _patches(container=None, billing=None)
    rc = await _run(cms, apply=True, env="prod", owner="org_missing")

    assert rc == 1
    update_fields.assert_not_called()
    reprovision.assert_not_called()


# ---- cohort --------------------------------------------------------------- #


def _billing_router(mapping):
    """AsyncMock whose return depends on the owner_id argument."""

    async def _side_effect(owner_id, *a, **k):
        return mapping.get(owner_id)

    return AsyncMock(side_effect=_side_effect)


@pytest.mark.asyncio
async def test_cohort_trial_dry_run_lists_only_trial_owners_writes_nothing():
    scan_rows = [
        {"owner_id": "user_trial_a"},
        {"owner_id": "user_paid_b"},
        {"owner_id": "user_byok_c"},
        {"owner_id": "user_oauth_d"},
    ]
    billing = _billing_router(
        {
            "user_trial_a": {"subscription_status": "trial_local"},
            "user_paid_b": {"provider_choice": "bedrock_claude"},
            "user_byok_c": {"provider_choice": "byo_key"},
            "user_oauth_d": {"provider_choice": "chatgpt_oauth"},
        }
    )
    update_fields, reprovision, cms = _patches(scan_rows=scan_rows, billing=billing)
    rc = await _run(cms, apply=False, env="dev", cohort="trial")

    assert rc == 0
    # Dry run: nothing written even though a trial owner matched.
    update_fields.assert_not_called()
    reprovision.assert_not_called()


@pytest.mark.asyncio
async def test_cohort_trial_apply_touches_only_trial_owner():
    scan_rows = [
        {"owner_id": "user_trial_a"},
        {"owner_id": "user_paid_b"},
        {"owner_id": "user_byok_c"},
    ]
    billing = _billing_router(
        {
            "user_trial_a": {"subscription_status": "trial_local"},
            "user_paid_b": {"provider_choice": "bedrock_claude"},
            "user_byok_c": {"provider_choice": "byo_key"},
        }
    )
    update_fields, reprovision, cms = _patches(scan_rows=scan_rows, billing=billing)
    rc = await _run(cms, apply=True, env="dev", cohort="trial")

    assert rc == 0
    # Exactly the trial owner — paid + BYOK excluded.
    update_fields.assert_awaited_once_with("user_trial_a", {"bedrock_via_gateway": True})
    reprovision.assert_awaited_once_with("user_trial_a")


@pytest.mark.asyncio
async def test_cohort_paid_apply_touches_only_paid_owners():
    scan_rows = [
        {"owner_id": "user_trial_a"},
        {"owner_id": "user_paid_b"},
        {"owner_id": "user_paid_c"},
        {"owner_id": "user_byok_d"},
    ]
    billing = _billing_router(
        {
            "user_trial_a": {"subscription_status": "trial_local"},
            "user_paid_b": {"provider_choice": "bedrock_claude"},
            "user_paid_c": {"provider_choice": "bedrock_claude"},
            "user_byok_d": {"provider_choice": "byo_key"},
        }
    )
    update_fields, reprovision, cms = _patches(scan_rows=scan_rows, billing=billing)
    rc = await _run(cms, apply=True, env="prod", cohort="paid")

    assert rc == 0
    assert reprovision.await_count == 2
    reprovisioned = {c.args[0] for c in reprovision.await_args_list}
    assert reprovisioned == {"user_paid_b", "user_paid_c"}
    flagged = {c.args[0] for c in update_fields.await_args_list}
    assert flagged == {"user_paid_b", "user_paid_c"}
    # Every write set the flag true.
    for c in update_fields.await_args_list:
        assert c.args[1] == {"bedrock_via_gateway": True}


@pytest.mark.asyncio
async def test_cohort_rollback_apply_sets_flag_false_for_cohort():
    scan_rows = [{"owner_id": "user_trial_a"}, {"owner_id": "user_paid_b"}]
    billing = _billing_router(
        {
            "user_trial_a": {"subscription_status": "trial_local"},
            "user_paid_b": {"provider_choice": "bedrock_claude"},
        }
    )
    update_fields, reprovision, cms = _patches(scan_rows=scan_rows, billing=billing)
    rc = await _run(cms, apply=True, env="prod", cohort="trial", rollback=True)

    assert rc == 0
    update_fields.assert_awaited_once_with("user_trial_a", {"bedrock_via_gateway": False})
    reprovision.assert_awaited_once_with("user_trial_a")


@pytest.mark.asyncio
async def test_cohort_empty_is_noop_success():
    scan_rows = [{"owner_id": "user_byok"}]
    billing = _billing_router({"user_byok": {"provider_choice": "byo_key"}})
    update_fields, reprovision, cms = _patches(scan_rows=scan_rows, billing=billing)
    rc = await _run(cms, apply=True, env="dev", cohort="trial")

    assert rc == 0
    update_fields.assert_not_called()
    reprovision.assert_not_called()


# ---- guardrails ----------------------------------------------------------- #


@pytest.mark.asyncio
async def test_requires_exactly_one_of_owner_or_cohort():
    _, _, cms = _patches()
    # Neither.
    rc = await _run(cms, apply=False, env="dev")
    assert rc == 2

    _, _, cms2 = _patches(container={"owner_id": "o"}, billing={"subscription_status": "trial_local"})
    # Both.
    rc = await _run(cms2, apply=False, env="dev", owner="o", cohort="trial")
    assert rc == 2


@pytest.mark.asyncio
async def test_one_owner_failure_does_not_abort_others_and_returns_1():
    scan_rows = [{"owner_id": "user_a"}, {"owner_id": "user_b"}]
    billing = _billing_router(
        {
            "user_a": {"subscription_status": "trial_local"},
            "user_b": {"subscription_status": "trial_local"},
        }
    )
    update_fields, reprovision, cms = _patches(scan_rows=scan_rows, billing=billing)
    # First reprovision raises, second must still run.
    reprovision.side_effect = [RuntimeError("boom"), None]

    rc = await _run(cms, apply=True, env="dev", cohort="trial")

    assert rc == 1
    assert reprovision.await_count == 2
