"""Tests for the provision.gate.blocked{code} metric emission.

A blocked gate emits EXACTLY ONE ``provision.gate.blocked{code}``. ``put_metric``
publishes the {env,service} rollup from that same record, which is what the W64
spike alarm reads, so the extra dimensionless emission this used to make would
now count every block twice and halve W64's threshold. An allowed gate (returns
None) emits nothing. Mirrors the mocking patterns in ``test_provision_gate.py``
/ ``test_provision_gate_credits.py``.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import provision_gate


@pytest.mark.asyncio
async def test_blocked_gate_emits_metric_with_code():
    # A canceled subscription (in TRIAL_BLOCKED_STATUSES) blocks at Layer 1
    # with code="subscription_required" — no provider/credits/oauth layers
    # are reached, so no further mocks are needed.
    account = {"subscription_status": "canceled", "stripe_subscription_id": "sub_x"}
    with (
        patch("core.services.provision_gate.put_metric") as mock_put,
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=account),
        ),
    ):
        gate = await provision_gate.evaluate_provision_gate(owner_id="org_x", is_admin=True)
    assert gate is not None
    assert gate.code == "subscription_required"
    calls = [c for c in mock_put.call_args_list if c.args and c.args[0] == "provision.gate.blocked"]
    # ONE emission. put_metric adds the {env,service} rollup W64 reads, so a
    # second dimensionless call would land on that same identity and count
    # every block twice.
    dims = [c.kwargs.get("dimensions") for c in calls]
    assert dims == [{"code": gate.code}]


@pytest.mark.asyncio
async def test_trial_expired_gate_emits_metric_and_dedup_telemetry():
    """The trial_expired gate is both metric-coded (like any other blocked
    code, covered above) AND fires the trial_expired_block PostHog capture
    (Task 16) — /container/status polls every ~3s while blocked, so the
    capture must be the DEDUPED helper, not a bare capture_product_event."""
    import time

    account = {
        "subscription_status": "trial_local",
        "trial_end": int(time.time()) - 60,
    }
    with (
        patch("core.services.provision_gate.put_metric"),
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=account),
        ),
        patch("core.services.provision_gate.capture_once_per_owner_per_day") as mock_capture,
    ):
        gate = await provision_gate.evaluate_provision_gate(
            owner_id="org_x", is_admin=True, actor_user_id="user_poller"
        )
    assert gate is not None
    assert gate.code == "trial_expired"
    mock_capture.assert_called_once_with(owner_id="org_x", event="trial_expired_block", distinct_id="user_poller")


@pytest.mark.asyncio
async def test_non_trial_expired_blocked_gate_does_not_fire_trial_expired_telemetry():
    """A DIFFERENT blocked code (e.g. subscription_required) must never fire
    the trial_expired_block capture."""
    account = {"subscription_status": "canceled", "stripe_subscription_id": "sub_x"}
    with (
        patch("core.services.provision_gate.put_metric"),
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=account),
        ),
        patch("core.services.provision_gate.capture_once_per_owner_per_day") as mock_capture,
    ):
        gate = await provision_gate.evaluate_provision_gate(owner_id="org_x", is_admin=True)
    assert gate is not None
    assert gate.code == "subscription_required"
    mock_capture.assert_not_called()


@pytest.mark.asyncio
async def test_allowed_provision_emits_no_metric():
    # Active bedrock subscription, credits available → gate passes (None).
    account = {
        "subscription_status": "active",
        "stripe_subscription_id": "sub_x",
        "provider_choice": "bedrock_claude",
    }
    with (
        patch("core.services.provision_gate.put_metric") as mock_put,
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=account),
        ),
        # metronome_gate.is_owner_blocked is lazy-imported inside the function
        # (`from core.services.metronome_gate import is_owner_blocked`) — patch
        # it at the source module.
        patch(
            "core.services.metronome_gate.is_owner_blocked",
            new=AsyncMock(return_value=False),
        ),
    ):
        gate = await provision_gate.evaluate_provision_gate(owner_id="org_x", is_admin=True)
    assert gate is None
    blocked = [c for c in mock_put.call_args_list if c.args and c.args[0] == "provision.gate.blocked"]
    assert blocked == []
