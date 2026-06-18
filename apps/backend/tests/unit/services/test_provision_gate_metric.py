"""Tests for the provision.gate.blocked{code} metric emission.

A blocked gate must emit BOTH a dimensionless ``provision.gate.blocked``
(matched by the W64 spike alarm, which queries the {env,service} stream) and
a ``provision.gate.blocked{code}`` (per-reason breakdown). An allowed gate
(returns None) emits nothing. Mirrors the mocking patterns in
``test_provision_gate.py`` / ``test_provision_gate_credits.py``.
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
    # Two emissions: one dimensionless (for the W64 total alarm) + one coded
    # (for the per-code breakdown). EMF = one stream per full dim set, so the
    # alarm querying {env,service} needs the dimensionless stream to exist.
    dims = [c.kwargs.get("dimensions") for c in calls]
    assert None in dims, "expected a dimensionless emission for the alarm"
    assert {"code": gate.code} in dims, "expected a coded emission for the breakdown"
    # Exactly one of each — no double-emit.
    assert dims.count(None) == 1
    assert dims.count({"code": gate.code}) == 1


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
