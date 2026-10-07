"""Unit tests for oauth_service.maybe_clear_phantom_connector.

Phantom-connector recovery (issue #2864 follow-up): a billing row whose
recorded oauth_connector_user_id names no Clerk user wedges the workspace —
the provision gate waits on a ghost while /start and the provider switch 403
every real human. These tests pin the fail-closed contract: clear only on a
positive Clerk 404, never on uncertainty, never over a changed value.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import oauth_service
from core.services.clerk_admin import ClerkLookupError


@pytest.mark.asyncio
async def test_unclaimed_row_is_noop_without_clerk_call():
    """No recorded connector → False, and Clerk is never consulted."""
    with patch(
        "core.services.clerk_admin.get_user_strict",
        new=AsyncMock(),
    ) as mock_get:
        assert await oauth_service.maybe_clear_phantom_connector("org_x", {}) is False
        assert await oauth_service.maybe_clear_phantom_connector("org_x", None) is False
    mock_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_phantom_connector_is_cleared_with_metric():
    """Clerk 404 → CAS clear; True only when the clear lands, metric emitted."""
    account = {"owner_id": "org_x", "oauth_connector_user_id": "user_ghost"}
    with (
        patch(
            "core.services.clerk_admin.get_user_strict",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.repositories.billing_repo.clear_phantom_oauth_connector",
            new=AsyncMock(return_value=True),
        ) as mock_clear,
        patch(
            "core.observability.metrics.put_metric",
            create=True,
        ) as mock_metric,
    ):
        assert await oauth_service.maybe_clear_phantom_connector("org_x", account) is True
    mock_clear.assert_awaited_once_with("org_x", expected_user_id="user_ghost")
    assert "billing.oauth_connector.phantom_cleared" in [c.args[0] for c in mock_metric.call_args_list]


@pytest.mark.asyncio
async def test_real_connector_is_kept():
    """Clerk knows the user → False, row untouched: no hijack path."""
    account = {"owner_id": "org_x", "oauth_connector_user_id": "user_real"}
    with (
        patch(
            "core.services.clerk_admin.get_user_strict",
            new=AsyncMock(return_value={"id": "user_real"}),
        ),
        patch(
            "core.repositories.billing_repo.clear_phantom_oauth_connector",
            new=AsyncMock(),
        ) as mock_clear,
    ):
        assert await oauth_service.maybe_clear_phantom_connector("org_x", account) is False
    mock_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_clerk_blip_fails_closed():
    """Lookup error (no key, network, non-404) → False, row untouched."""
    account = {"owner_id": "org_x", "oauth_connector_user_id": "user_maybe"}
    with (
        patch(
            "core.services.clerk_admin.get_user_strict",
            new=AsyncMock(side_effect=ClerkLookupError("down")),
        ),
        patch(
            "core.repositories.billing_repo.clear_phantom_oauth_connector",
            new=AsyncMock(),
        ) as mock_clear,
    ):
        assert await oauth_service.maybe_clear_phantom_connector("org_x", account) is False
    mock_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_cas_race_reports_not_cleared():
    """Phantom verified but the field changed underneath → False (no clobber)."""
    account = {"owner_id": "org_x", "oauth_connector_user_id": "user_ghost"}
    with (
        patch(
            "core.services.clerk_admin.get_user_strict",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.repositories.billing_repo.clear_phantom_oauth_connector",
            new=AsyncMock(return_value=False),
        ),
        patch("core.observability.metrics.put_metric", create=True) as mock_metric,
    ):
        assert await oauth_service.maybe_clear_phantom_connector("org_x", account) is False
    assert mock_metric.call_args_list == []
