"""Break-glass script: dry-run plans without writing; --apply puts the DIRECT
policy + reprovisions; --revert puts the FENCE policy back. Only Bedrock-path
owners are touched."""

from unittest.mock import AsyncMock, patch

import pytest

from scripts import bedrock_gate_break_glass as mod

_ARNS = {"sonnet": "arn:aws:bedrock:us-east-1:1:application-inference-profile/s"}


def _patches(*, rows, billing_by_owner):
    put_policy = AsyncMock()
    # The re-render is a provision dispatch now: it re-writes the owner's agent profiles and
    # boot secret for the current SSM mode. There is no ECS service to reprovision.
    reprovision = AsyncMock()

    async def _billing(owner_id):
        return billing_by_owner.get(owner_id)

    async def _row(owner_id):
        for r in rows:
            if r and r.get("owner_id") == owner_id:
                return r
        return None

    cms = [
        patch("core.repositories.container_repo.scan_all", AsyncMock(return_value=rows)),
        patch("core.repositories.container_repo.get_by_owner_id", AsyncMock(side_effect=_row)),
        patch("core.repositories.billing_repo.get_by_owner_id", AsyncMock(side_effect=_billing)),
        patch("core.billing.bedrock_aip.put_owner_bedrock_policy", put_policy),
        patch("core.billing.bedrock_aip.aip_fan_out_regions", lambda: ("us-east-1",)),
        patch("core.services.provision_dispatch.dispatch_provision", reprovision),
    ]
    return put_policy, reprovision, cms


async def _run(cms, **kw):
    for c in cms:
        c.start()
    try:
        return await mod.main(**kw)
    finally:
        for c in cms:
            c.stop()


@pytest.mark.asyncio
async def test_dry_run_plans_but_writes_nothing():
    put_policy, reprovision, cms = _patches(
        rows=[{"owner_id": "u1", "bedrock_aip_arns": _ARNS}],
        billing_by_owner={"u1": {"subscription_status": "trial_local"}},
    )
    rc = await _run(cms, env="dev", apply=False, revert=False, owner=None)
    assert rc == 0
    put_policy.assert_not_called()
    reprovision.assert_not_called()


@pytest.mark.asyncio
async def test_apply_puts_direct_policy_and_reprovisions():
    put_policy, reprovision, cms = _patches(
        rows=[{"owner_id": "u1", "bedrock_aip_arns": _ARNS}],
        billing_by_owner={"u1": {"subscription_status": "trial_local"}},
    )
    with patch("core.billing.bedrock_aip.build_owner_bedrock_direct_policy", return_value={"doc": "direct"}) as direct:
        rc = await _run(cms, env="dev", apply=True, revert=False, owner=None)
    assert rc == 0
    direct.assert_called_once()
    put_policy.assert_awaited_once_with("u1", {"doc": "direct"})
    reprovision.assert_awaited_once_with("u1")


@pytest.mark.asyncio
async def test_revert_puts_fence_policy():
    put_policy, reprovision, cms = _patches(
        rows=[{"owner_id": "u1", "bedrock_aip_arns": _ARNS}],
        billing_by_owner={"u1": {"provider_choice": "bedrock_claude"}},
    )
    with patch("core.billing.bedrock_aip.build_owner_bedrock_policy", return_value={"doc": "fence"}) as fence:
        rc = await _run(cms, env="dev", apply=True, revert=True, owner=None)
    assert rc == 0
    fence.assert_called_once()
    put_policy.assert_awaited_once_with("u1", {"doc": "fence"})


@pytest.mark.asyncio
async def test_non_bedrock_owners_excluded():
    put_policy, reprovision, cms = _patches(
        rows=[
            {"owner_id": "u_byok", "bedrock_aip_arns": _ARNS},
            {"owner_id": "u_no_aips"},
        ],
        billing_by_owner={"u_byok": {"provider_choice": "byo_key"}},
    )
    rc = await _run(cms, env="dev", apply=True, revert=False, owner=None)
    assert rc == 0
    put_policy.assert_not_called()
    reprovision.assert_not_called()
