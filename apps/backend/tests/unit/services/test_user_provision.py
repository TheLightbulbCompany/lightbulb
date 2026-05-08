"""Unit tests for the unified ensure_user_ready helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.auth import AuthContext
from core.services.user_provision import ensure_user_ready


def _personal_auth(user_id: str = "user_123", email: str = "u@example.com") -> AuthContext:
    """Personal-context AuthContext (no org)."""
    return AuthContext(
        user_id=user_id,
        org_id=None,
        org_role=None,
        email=email,
    )


def _org_admin_auth(
    user_id: str = "user_123",
    org_id: str = "org_456",
    email: str = "u@example.com",
) -> AuthContext:
    """Org-admin AuthContext. is_org_context + is_org_admin are derived properties."""
    return AuthContext(
        user_id=user_id,
        org_id=org_id,
        org_role="org:admin",
        email=email,
    )


def _org_member_auth(
    user_id: str = "user_456",
    org_id: str = "org_456",
    email: str = "m@example.com",
) -> AuthContext:
    """Org-member AuthContext. is_org_admin returns False because role != org:admin."""
    return AuthContext(
        user_id=user_id,
        org_id=org_id,
        org_role="org:member",
        email=email,
    )


@pytest.mark.asyncio
async def test_ensure_user_ready_no_op_when_gate_blocks():
    """If evaluate_provision_gate returns a Gate (not None), nothing
    is provisioned. Caller's pre-condition responsibility."""
    fake_gate = MagicMock()  # truthy non-None
    fake_gate.code = "subscription_required"
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=fake_gate),
        ),
        patch(
            "core.services.user_provision._ensure_paperclip_for_owner",
            new=AsyncMock(),
        ) as mock_paperclip,
        patch(
            "core.services.user_provision.background_provision",
            new=AsyncMock(),
        ) as mock_container,
    ):
        await ensure_user_ready(_personal_auth())
    mock_paperclip.assert_not_awaited()
    mock_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_paperclip_for_owner_personal_calls_provision_org():
    """Personal user → provision_org with owner_id == user_id."""
    fake_provisioning = MagicMock()
    fake_provisioning.provision_org = AsyncMock()
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.user_provision.build_paperclip_provisioning",
            new=AsyncMock(return_value=fake_provisioning),
        ),
        patch(
            "core.services.user_provision.close_paperclip_provisioning",
            new=AsyncMock(),
        ),
        patch(
            "core.services.user_provision.background_provision",
            new=AsyncMock(),
        ),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c),
    ):
        await ensure_user_ready(_personal_auth("user_p", "p@example.com"))
    fake_provisioning.provision_org.assert_awaited_once_with(
        owner_id="user_p",
        owner_user_id="user_p",
        owner_email="p@example.com",
    )


@pytest.mark.asyncio
async def test_ensure_paperclip_for_owner_org_admin_passes_org_id():
    """Org admin → provision_org with owner_id == org_id, owner_user_id == user_id."""
    fake_provisioning = MagicMock()
    fake_provisioning.provision_org = AsyncMock()
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.user_provision.build_paperclip_provisioning",
            new=AsyncMock(return_value=fake_provisioning),
        ),
        patch(
            "core.services.user_provision.close_paperclip_provisioning",
            new=AsyncMock(),
        ),
        patch(
            "core.services.user_provision.background_provision",
            new=AsyncMock(),
        ),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    fake_provisioning.provision_org.assert_awaited_once_with(
        owner_id="org_xyz",
        owner_user_id="user_admin",
        owner_email="a@example.com",
    )


# ---------------------------------------------------------------------------
# Org-member fast path (Task 4)
# ---------------------------------------------------------------------------
#
# Implementation note: ``paperclip_repo`` exposes no module-level
# ``get_by_org_id`` helper (see Task 4 Step 4.1 verification — only
# ``PaperclipRepo.get_org_company_id(org_id) -> str | None`` exists).
# ``_ensure_paperclip_for_member`` therefore reuses
# ``PaperclipProvisioning._find_org_owner``, which already filters to
# ``status="active"`` rows. The "company missing" and "company not yet
# active" cases collapse into a single ``_find_org_owner -> None`` branch
# in the unit under test, so these tests assert the same returned-None
# behavior for both.


@pytest.mark.asyncio
async def test_ensure_paperclip_for_member_returns_when_owner_company_missing():
    """Org member's ensure_user_ready when org's Paperclip company
    doesn't exist yet → return without raising, no provision_member
    call. The pending state is implicit; resolves on the next poll
    when owner is ready."""
    fake_provisioning = MagicMock()
    fake_provisioning._find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.provision_member = AsyncMock()
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.user_provision.build_paperclip_provisioning",
            new=AsyncMock(return_value=fake_provisioning),
        ),
        patch(
            "core.services.user_provision.close_paperclip_provisioning",
            new=AsyncMock(),
        ),
    ):
        # Should NOT raise.
        await ensure_user_ready(_org_member_auth())
    fake_provisioning.provision_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_paperclip_for_member_returns_when_owner_company_inactive():
    """Same gracefully-return behaviour when owner's company exists
    but is in 'provisioning' or 'failed' state.

    ``_find_org_owner`` filters out non-active rows internally (see
    paperclip_provisioning.py docstring), so from the unit-under-test's
    perspective an inactive company looks identical to a missing one:
    both surface as ``_find_org_owner -> None``."""
    fake_provisioning = MagicMock()
    fake_provisioning._find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.provision_member = AsyncMock()
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.user_provision.build_paperclip_provisioning",
            new=AsyncMock(return_value=fake_provisioning),
        ),
        patch(
            "core.services.user_provision.close_paperclip_provisioning",
            new=AsyncMock(),
        ),
    ):
        await ensure_user_ready(_org_member_auth())
    fake_provisioning.provision_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_paperclip_for_member_calls_provision_member_when_owner_active():
    """Org member with an existing active company row → provision_member."""
    fake_owner_row = MagicMock(status="active", company_id="co_123")
    fake_provisioning = MagicMock()
    fake_provisioning._find_org_owner = AsyncMock(return_value=fake_owner_row)
    fake_provisioning.provision_member = AsyncMock()
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.user_provision.build_paperclip_provisioning",
            new=AsyncMock(return_value=fake_provisioning),
        ),
        patch(
            "core.services.user_provision.close_paperclip_provisioning",
            new=AsyncMock(),
        ),
        patch(
            "core.services.user_provision.lookup_owner_email",
            new=AsyncMock(return_value="owner@example.com"),
        ),
    ):
        await ensure_user_ready(_org_member_auth("user_m", "org_o", "m@example.com"))
    fake_provisioning.provision_member.assert_awaited_once_with(
        owner_id="org_o",
        user_id="user_m",
        email="m@example.com",
        owner_email="owner@example.com",
    )
