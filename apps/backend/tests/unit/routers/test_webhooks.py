"""Unit tests for the Clerk webhook handlers — tenancy-invariant observer.

These tests focus on the defense-in-depth observer in
``_handle_organization_membership_created``. The observer logs an
error and emits a metric when a new org member already has an active
personal billing row (a tenancy-invariant violation).

After Task 7 the membership webhook is record-keeping + invariant
probe only — provisioning is deferred to the first /container/status
poll (Task 8). The probe must still run regardless, and a probe
failure must not raise (record-keeping must still complete).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_membership_created_with_active_personal_logs_violation(caplog):
    """Defense-in-depth: if a new org member already has an active personal
    billing row, log loudly + emit a metric. The webhook still completes
    (no exception bubbles)."""
    payload = {
        "organization": {"id": "org_xyz", "created_by": "user_admin"},
        "public_user_data": {
            "user_id": "user_member_dirty",
            "identifier": "member@example.com",
        },
    }

    with (
        patch("routers.webhooks.billing_repo") as mock_billing,
        patch("routers.webhooks.put_metric") as mock_metric,
    ):
        mock_billing.get_by_owner_id = AsyncMock(
            return_value={
                "owner_id": "user_member_dirty",
                "owner_type": "personal",
                "subscription_status": "active",
            }
        )

        with caplog.at_level(logging.ERROR, logger="routers.webhooks"):
            from routers.webhooks import _handle_organization_membership_created

            await _handle_organization_membership_created(payload)

    # Loud log emitted
    assert any("tenancy_invariant.violated" in rec.message for rec in caplog.records)
    # Metric emitted with the right name
    metric_names = [call.args[0] for call in mock_metric.call_args_list]
    assert "tenancy_invariant.violation" in metric_names


@pytest.mark.asyncio
async def test_membership_created_without_personal_billing_emits_no_violation(caplog):
    """No personal billing row → no violation log, no violation metric."""
    payload = {
        "organization": {"id": "org_xyz", "created_by": "user_admin"},
        "public_user_data": {
            "user_id": "user_member_clean",
            "identifier": "member@example.com",
        },
    }

    with (
        patch("routers.webhooks.billing_repo") as mock_billing,
        patch("routers.webhooks.put_metric") as mock_metric,
    ):
        mock_billing.get_by_owner_id = AsyncMock(return_value=None)

        with caplog.at_level(logging.ERROR, logger="routers.webhooks"):
            from routers.webhooks import _handle_organization_membership_created

            await _handle_organization_membership_created(payload)

    assert not any("tenancy_invariant.violated" in rec.message for rec in caplog.records)
    metric_names = [call.args[0] for call in mock_metric.call_args_list]
    assert "tenancy_invariant.violation" not in metric_names


@pytest.mark.asyncio
async def test_membership_created_invariant_probe_failure_does_not_raise(caplog):
    """If the personal-billing lookup itself raises (e.g. DDB throttling),
    the observer must swallow + log via logger.exception. The webhook
    must complete cleanly so Clerk receives a 2xx — otherwise svix
    redelivers the same event indefinitely."""
    payload = {
        "organization": {"id": "org_xyz", "created_by": "user_admin"},
        "public_user_data": {
            "user_id": "user_member_x",
            "identifier": "member@example.com",
        },
    }

    with patch("routers.webhooks.billing_repo") as mock_billing:
        mock_billing.get_by_owner_id = AsyncMock(side_effect=RuntimeError("ddb boom"))

        with caplog.at_level(logging.ERROR, logger="routers.webhooks"):
            from routers.webhooks import _handle_organization_membership_created

            # Must not raise.
            await _handle_organization_membership_created(payload)

    # Probe failure routed through logger.exception, not raised
    assert any("tenancy invariant probe failed" in rec.message for rec in caplog.records)
