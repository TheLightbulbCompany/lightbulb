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
# ``PaperclipProvisioning.find_org_owner``, which already filters to
# ``status="active"`` rows. The "company missing" and "company not yet
# active" cases collapse into a single ``find_org_owner -> None`` branch
# in the unit under test, so we only assert the missing-company case
# here; ``find_org_owner``'s active-only filter is its own concern,
# tested at the ``paperclip_provisioning`` layer.


@pytest.mark.asyncio
async def test_ensure_paperclip_for_member_returns_when_owner_company_missing():
    """Org member's ensure_user_ready when org's Paperclip company
    doesn't exist yet (or isn't active) → return without raising, no
    provision_member call. The pending state is implicit; resolves on
    the next poll when owner is ready."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
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
async def test_ensure_paperclip_for_member_calls_provision_member_when_owner_active():
    """Org member with an existing active company row → provision_member."""
    fake_owner_row = MagicMock(status="active", company_id="co_123", user_id="user_owner_123")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_owner_row)
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


@pytest.mark.asyncio
async def test_ensure_paperclip_for_member_threads_owner_user_id_to_lookup_owner_email():
    """The owner_email lookup must pass the owner's Clerk user_id as
    fallback_user_id — passing None makes lookup_owner_email return
    None (paperclip_owner_email.py lines 46-47 short-circuit) and
    provision_member would be called with empty owner_email,
    breaking Paperclip sign-in."""
    fake_company = MagicMock(
        status="active",
        company_id="co_123",
        user_id="user_owner_123",
    )
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_company)
    fake_provisioning.provision_member = AsyncMock()
    lookup_calls = []

    async def capturing_lookup(*, org_id, fallback_user_id):
        lookup_calls.append((org_id, fallback_user_id))
        return "owner@example.com"

    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.user_provision.lookup_owner_email",
            side_effect=capturing_lookup,
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
        await ensure_user_ready(_org_member_auth("user_m", "org_o", "m@example.com"))

    # The owner-email lookup specifically must thread owner's user_id, not None.
    owner_email_lookups = [
        c
        for c in lookup_calls
        if c[0] == "org_o"  # the org-id lookup is for owner-email
    ]
    assert owner_email_lookups, "expected at least one lookup_owner_email(org_id=org_o, ...)"
    assert all(c[1] == "user_owner_123" for c in owner_email_lookups), (
        f"expected fallback_user_id='user_owner_123', got {owner_email_lookups}"
    )


# ---------------------------------------------------------------------------
# Container dispatch contract (Task 5)
# ---------------------------------------------------------------------------
#
# The container background_provision is dispatched as an asyncio task
# only on the owner path. Org members ride the owner's container, so
# ensure_user_ready must NOT dispatch a container task for them.


@pytest.mark.asyncio
async def test_ensure_user_ready_dispatches_container_for_owner():
    """Owner path → background_provision dispatched as task."""
    captured: list[str] = []

    def capture_task(coro):
        # Snapshot the qualname BEFORE closing — coro.close() destroys the
        # coroutine frame, after which __qualname__ inspection is unsafe.
        captured.append(coro.__qualname__)
        # Don't actually await; close the coroutine to avoid the "never awaited" warning.
        coro.close()

    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.user_provision._ensure_paperclip_for_owner",
            new=AsyncMock(),
        ),
        patch(
            "core.services.user_provision.asyncio.create_task",
            side_effect=capture_task,
        ),
    ):
        await ensure_user_ready(_personal_auth("user_p"))
    assert captured == ["background_provision"], (
        f"expected one create_task call dispatching background_provision, got {captured}"
    )


@pytest.mark.asyncio
async def test_ensure_user_ready_dispatches_container_for_org_admin():
    """Org-admin owner path → background_provision dispatched with owner_id=org_id."""
    captured: list[str] = []

    def capture_task(coro):
        # Snapshot the qualname BEFORE closing — coro.close() destroys the
        # coroutine frame, after which __qualname__ inspection is unsafe.
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.user_provision._ensure_paperclip_for_owner",
            new=AsyncMock(),
        ),
        patch(
            "core.services.user_provision.asyncio.create_task",
            side_effect=capture_task,
        ),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    assert captured == ["background_provision"], (
        f"expected one create_task call dispatching background_provision, got {captured}"
    )


@pytest.mark.asyncio
async def test_ensure_paperclip_for_member_idempotent_on_duplicate_409():
    """provision_member raising 409 (member already added) is treated as
    success — the canonical /container/status trigger may fire this on
    every member poll, so duplicate calls must be idempotent.

    Codex P1 on PR #563: when an org member joins an org whose owner
    already has a container, /container/status hits the existing-container
    branch and dispatches ensure_user_ready for the member on every poll.
    The first dispatch successfully adds the member; subsequent dispatches
    see sign_up_user → Better Auth 409 (account already exists). That
    409 must be swallowed, not re-raised — otherwise the member's status
    poll loop logs PaperclipApiError(409) on every iteration.
    """
    from core.services.paperclip_admin_client import PaperclipApiError

    fake_company = MagicMock(status="active", company_id="co_x", user_id="user_owner")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_company)
    duplicate_error = PaperclipApiError(
        "POST /api/auth/sign-up/email -> 409",
        status_code=409,
        body='{"message":"User already exists"}',
    )
    fake_provisioning.provision_member = AsyncMock(side_effect=duplicate_error)

    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.user_provision.lookup_owner_email",
            new=AsyncMock(return_value="o@example.com"),
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

    fake_provisioning.provision_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_paperclip_for_member_reraises_non_409_paperclip_error():
    """Non-409 PaperclipApiError (e.g. 500 from sign_up_user) must
    propagate — only the duplicate-409 case is treated as idempotent
    success. A blanket swallow would mask transient Paperclip errors
    from operator visibility."""
    from core.services.paperclip_admin_client import PaperclipApiError

    fake_company = MagicMock(status="active", company_id="co_x", user_id="user_owner")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_company)
    server_error = PaperclipApiError(
        "POST /api/auth/sign-up/email -> 500",
        status_code=500,
        body='{"message":"internal"}',
    )
    fake_provisioning.provision_member = AsyncMock(side_effect=server_error)

    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.user_provision.lookup_owner_email",
            new=AsyncMock(return_value="o@example.com"),
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
        with pytest.raises(PaperclipApiError) as exc:
            await ensure_user_ready(_org_member_auth())
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_ensure_user_ready_does_not_dispatch_container_for_member():
    """Member path → no container dispatch."""
    fake_company = MagicMock(status="active", company_id="co_x", user_id="user_owner")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_company)
    fake_provisioning.provision_member = AsyncMock()
    captured = []

    def capture_task(coro):
        captured.append(coro)
        coro.close()

    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core.services.user_provision.lookup_owner_email",
            new=AsyncMock(return_value="o@example.com"),
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
            "core.services.user_provision.asyncio.create_task",
            side_effect=capture_task,
        ),
    ):
        await ensure_user_ready(_org_member_auth())
    assert captured == [], f"expected no create_task calls, got {len(captured)}"
