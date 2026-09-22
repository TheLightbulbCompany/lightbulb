from unittest.mock import AsyncMock, patch

import pytest

from scripts import extend_short_trials as sweep


@pytest.mark.asyncio
async def test_sweep_extends_short_trials_and_rearms_only_those(capsys):
    rows = [
        {
            "owner_id": "org_short",
            "subscription_status": "trial_local",
            "trial_end": 100,
            "created_at": "2026-09-12T01:26:00+00:00",
        },
        {"owner_id": "org_ok", "subscription_status": "trial_local", "trial_end": 5_000_000_000},
        {"owner_id": "org_unarmed", "subscription_status": "trial_local"},
        {"owner_id": "org_paid", "subscription_status": "active", "trial_end": 100},
    ]
    extend = AsyncMock(side_effect=lambda owner, row: 999 if owner == "org_short" else row["trial_end"])
    arm = AsyncMock()
    with (
        patch.object(sweep.billing_repo, "scan_all", AsyncMock(return_value=rows)),
        patch.object(sweep.billing_repo, "extend_short_trial", extend),
        patch.object(sweep, "arm_nightly_ceo", arm),
    ):
        assert await sweep.main(dry_run=False) == 0
    assert [c.args[0] for c in extend.await_args_list] == ["org_short", "org_ok"]
    arm.assert_awaited_once_with("org_short")
    assert "org_short extended 100 -> 999" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(capsys):
    rows = [
        {
            "owner_id": "org_short",
            "subscription_status": "trial_local",
            "trial_end": 100,
            "created_at": "2026-09-12T01:26:00+00:00",
        }
    ]
    extend, arm = AsyncMock(), AsyncMock()
    with (
        patch.object(sweep.billing_repo, "scan_all", AsyncMock(return_value=rows)),
        patch.object(sweep.billing_repo, "extend_short_trial", extend),
        patch.object(sweep, "arm_nightly_ceo", arm),
    ):
        assert await sweep.main(dry_run=True) == 0
    extend.assert_not_awaited()
    arm.assert_not_awaited()
    assert "org_short would-extend" in capsys.readouterr().out
