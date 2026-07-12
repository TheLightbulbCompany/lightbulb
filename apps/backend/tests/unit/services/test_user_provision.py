"""Unit tests for the unified ensure_user_ready helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.auth import AuthContext
from core.services.user_provision import ensure_personal_trial_default, ensure_user_ready


@pytest.fixture(autouse=True)
def _mock_ensure_metronome_customer():
    """ensure_user_ready provisions the Metronome customer inline on the
    owner path (best-effort); stub it so these tests never touch billing DDB."""
    with patch(
        "core.services.user_provision.ensure_metronome_customer",
        new=AsyncMock(return_value="cust-uuid"),
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def _mock_create_trial_local_if_absent():
    """ensure_user_ready auto-defaults a genuinely-new owner into
    trial_local before the gate is evaluated (Task 3); stub it so
    pre-existing tests never touch real billing DDB. Tests that care
    about this call inspect/override the mock directly."""
    with patch(
        "core.services.user_provision.billing_repo.create_trial_local_if_absent",
        new=AsyncMock(return_value=None),
    ) as mock:
        yield mock


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
async def test_org_admin_with_no_company_creates_it():
    """Org admin + org has NO active Paperclip company yet → provision_org
    (create), and container provisioning is dispatched (admin owns the box).
    Decided by company existence, not role."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value=True)
    fake_provisioning.release_org_create_claim = AsyncMock()
    fake_provisioning.provision_org = AsyncMock()
    fake_provisioning.provision_member = AsyncMock()
    # Post-create verification sees the active row for THIS org → claim kept.
    fake_provisioning.get_company_row = AsyncMock(return_value=MagicMock(status="active", org_id="org_xyz"))
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    fake_provisioning.try_acquire_org_create_claim.assert_awaited_once_with("org_xyz", holder="user_admin")
    fake_provisioning.provision_org.assert_awaited_once_with(
        owner_id="org_xyz",
        owner_user_id="user_admin",
        owner_email="a@example.com",
    )
    fake_provisioning.provision_member.assert_not_awaited()
    fake_provisioning.release_org_create_claim.assert_not_awaited()
    assert captured == ["background_provision"], f"admin-create should dispatch container, got {captured}"


@pytest.mark.asyncio
async def test_org_admin_create_without_active_row_releases_claim():
    """If the create returns WITHOUT an active row (provision_org's
    409-as-idempotent path after a prior crashed attempt burned the signup),
    the per-org claim must be RELEASED so a retry / another admin can recover
    — otherwise the org is stranded behind a held claim with no company."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value=True)
    fake_provisioning.release_org_create_claim = AsyncMock()
    # provision_org "succeeds" (no raise) but writes no row — simulate the
    # swallowed-409 path by having get_company_row report nothing active.
    fake_provisioning.provision_org = AsyncMock()
    fake_provisioning.get_company_row = AsyncMock(return_value=None)
    fake_provisioning.provision_member = AsyncMock()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    fake_provisioning.release_org_create_claim.assert_awaited_once_with("org_xyz", holder="user_admin")


@pytest.mark.asyncio
async def test_org_admin_create_personal_row_mismatch_releases_claim():
    """If provision_org short-circuits on the admin's pre-existing PERSONAL
    row (rows are keyed per-user), the strongly-consistent read returns an
    active row whose org_id is NOT this org. That must NOT count as a
    successful org create — release the claim so it isn't held with no
    org-indexed company."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value=True)
    fake_provisioning.release_org_create_claim = AsyncMock()
    fake_provisioning.provision_org = AsyncMock()
    # Active row exists, but it belongs to the user's personal context.
    fake_provisioning.get_company_row = AsyncMock(
        return_value=MagicMock(status="active", org_id="user_admin"),
    )
    fake_provisioning.provision_member = AsyncMock()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    fake_provisioning.release_org_create_claim.assert_awaited_once_with("org_xyz", holder="user_admin")


@pytest.mark.asyncio
async def test_org_admin_create_claim_lost_skips_paperclip_create():
    """Cross-instance race: admin sees no company but LOSES the create claim
    (another backend instance is bootstrapping) → must NOT call provision_org
    or provision_member; it joins on a later poll once the winner's company is
    visible. Container provisioning is a SEPARATE, idempotent concern gated on
    admin/personal, so background_provision is still dispatched (the winner
    dispatches it too; concurrent provision_user_container on an absent service
    is exception-safe)."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value=False)
    fake_provisioning.release_org_create_claim = AsyncMock()
    fake_provisioning.provision_org = AsyncMock()
    fake_provisioning.provision_member = AsyncMock()
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    fake_provisioning.provision_org.assert_not_awaited()
    fake_provisioning.provision_member.assert_not_awaited()
    fake_provisioning.release_org_create_claim.assert_not_awaited()
    assert captured == ["background_provision"], (
        f"claim-loser skips paperclip create but still dispatches container, got {captured}"
    )


@pytest.mark.asyncio
async def test_org_admin_create_failure_releases_claim():
    """If provision_org raises after we won the claim, the claim must be
    RELEASED so a retry (this instance or another) can re-acquire it —
    otherwise a crash/error would strand the org's company forever."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value=True)
    fake_provisioning.release_org_create_claim = AsyncMock()
    boom = RuntimeError("paperclip down")
    fake_provisioning.provision_org = AsyncMock(side_effect=boom)

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        with pytest.raises(RuntimeError):
            await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    fake_provisioning.release_org_create_claim.assert_awaited_once_with("org_xyz", holder="user_admin")


@pytest.mark.asyncio
async def test_org_admin_with_existing_company_joins_it():
    """REGRESSION: org admin + org ALREADY has an active company →
    provision_member (join), NOT provision_org. The multi-admin case that
    previously 409'd on /teams."""
    fake_owner_row = MagicMock(status="active", company_id="co_1", user_id="user_first")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_owner_row)
    fake_provisioning.provision_org = AsyncMock()
    fake_provisioning.provision_member = AsyncMock()
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.lookup_owner_email", new=AsyncMock(return_value="owner@example.com")),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_org_admin_auth("user_second_admin", "org_xyz", "b@example.com"))
    fake_provisioning.provision_member.assert_awaited_once_with(
        owner_id="org_xyz",
        user_id="user_second_admin",
        email="b@example.com",
        owner_email="owner@example.com",
    )
    fake_provisioning.provision_org.assert_not_awaited()


@pytest.mark.asyncio
async def test_org_member_with_no_company_waits_does_not_create():
    """A non-admin member polling a not-yet-bootstrapped org must NOT create
    the company (only admins bootstrap, mirroring who spins up the shared
    container) and must NOT dispatch container provisioning. It returns
    gracefully and joins on a later poll once an admin has created it.
    Creating here would seed Friday/Atlas pointing at a container members
    can't provision (a company-without-container window)."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.provision_org = AsyncMock()
    fake_provisioning.provision_member = AsyncMock()
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_org_member_auth())
    fake_provisioning.provision_org.assert_not_awaited()
    fake_provisioning.provision_member.assert_not_awaited()
    assert captured == [], f"member must not dispatch container when waiting, got {captured}"


# ---------------------------------------------------------------------------
# Org-member fast path
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
    """Org-admin (no company yet) → background_provision dispatched (admin owns the box)."""
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision._ensure_paperclip_for_org", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
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

    When an org member joins an org whose owner already has a container,
    /container/status hits the existing-container branch and dispatches
    ensure_user_ready for the member on every poll.
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
async def test_ensure_paperclip_for_owner_idempotent_on_duplicate_409():
    """provision_org raising PaperclipApiError(409) must be treated as
    success — the canonical /container/status trigger
    may fire this on every owner poll until container exists, so duplicate
    calls must be idempotent.

    For owners with no container row yet, the new /container/status poll
    keeps dispatching ensure_user_ready until ECS creates the row. A slow
    first run (~2-3s) followed by a fast second poll could collide on
    Paperclip's sign_up_user (Better Auth 409 on duplicate email). Without
    the 409 swallow the duplicate Better Auth call would surface as a
    PaperclipApiError on every poll until the race resolves.

    Mirrors the member-path fix from commit 6bd01b82 (see
    test_ensure_paperclip_for_member_idempotent_on_duplicate_409).
    """
    from core.services.paperclip_admin_client import PaperclipApiError

    fake_provisioning = MagicMock()
    fake_provisioning.provision_org = AsyncMock(
        side_effect=PaperclipApiError(
            "POST /api/auth/sign-up/email -> 409",
            status_code=409,
            body='{"message":"User already exists"}',
        ),
    )
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
            "core.services.user_provision.background_provision",
            new=AsyncMock(),
        ),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        # Should NOT raise.
        await ensure_user_ready(_personal_auth())
    fake_provisioning.provision_org.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_paperclip_for_owner_reraises_non_409_paperclip_error():
    """Non-409 PaperclipApiError must propagate — only the duplicate-409
    race is treated as idempotent. A blanket swallow would mask transient
    5xx and credential errors from operator visibility (e.g. a Paperclip
    outage would silently fall through to the container background
    dispatch, which would then crash later in ECS without a clean signal
    of why)."""
    from core.services.paperclip_admin_client import PaperclipApiError

    fake_provisioning = MagicMock()
    fake_provisioning.provision_org = AsyncMock(
        side_effect=PaperclipApiError(
            "POST /api/auth/sign-up/email -> 502",
            status_code=502,
            body='{"message":"bad gateway"}',
        ),
    )
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
            await ensure_user_ready(_personal_auth())
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_member_joins_company_but_does_not_dispatch_container():
    """Org member → paperclip join runs, but NO container dispatch (members
    ride the org admin's shared container; they never spin one up)."""
    fake_company = MagicMock(status="active", company_id="co_x", user_id="user_owner")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_company)
    fake_provisioning.provision_member = AsyncMock()
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision.lookup_owner_email", new=AsyncMock(return_value="o@example.com")),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_org_member_auth())
    fake_provisioning.provision_member.assert_awaited_once()
    assert captured == [], f"members must not dispatch container, got {captured}"


@pytest.mark.asyncio
async def test_provision_container_false_skips_container_dispatch_for_admin():
    """RUNNING-container poll path: ensure_user_ready(provision_container=False)
    must run paperclip provisioning but NOT dispatch background_provision —
    otherwise provision_user_container would force-restart the running
    container on every ~3s poll."""
    fake_owner_row = MagicMock(status="active", company_id="co_1", user_id="user_first")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_owner_row)
    fake_provisioning.provision_member = AsyncMock()
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision.lookup_owner_email", new=AsyncMock(return_value="o@example.com")),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"), provision_container=False)
    fake_provisioning.provision_member.assert_awaited_once()
    assert captured == [], f"provision_container=False must skip container dispatch, got {captured}"


@pytest.mark.asyncio
async def test_metronome_failure_does_not_block_provisioning(_mock_ensure_metronome_customer):
    """A Metronome 5xx (or any error) on the inline customer-provision call is
    best-effort: paperclip provisioning + container dispatch still run, and
    nothing raises. The short-circuit keys on the persisted
    metronome_customer_id, so the next /container/status poll retries."""
    _mock_ensure_metronome_customer.side_effect = RuntimeError("metronome 503")
    fake_provisioning = MagicMock()
    fake_provisioning.provision_org = AsyncMock()
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_personal_auth("user_p", "p@example.com"))
    _mock_ensure_metronome_customer.assert_awaited_once_with(owner_id="user_p")
    fake_provisioning.provision_org.assert_awaited_once()
    assert captured == ["background_provision"]


@pytest.mark.asyncio
async def test_metronome_customer_not_provisioned_for_org_member(_mock_ensure_metronome_customer):
    """Org members never provision the org's Metronome customer — that
    belongs to the org owner_id and an admin poll creates it."""
    fake_company = MagicMock(status="active", company_id="co_x", user_id="user_owner")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_company)
    fake_provisioning.provision_member = AsyncMock()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision.lookup_owner_email", new=AsyncMock(return_value="o@example.com")),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_org_member_auth())
    _mock_ensure_metronome_customer.assert_not_awaited()


@pytest.mark.asyncio
async def test_metronome_customer_provisioned_for_org_admin_with_org_owner_id(_mock_ensure_metronome_customer):
    """Org admins provision the Metronome customer keyed on the ORG owner_id
    (the billing row's PK), never their personal user_id."""
    fake_owner_row = MagicMock(status="active", company_id="co_1", user_id="user_first")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_owner_row)
    fake_provisioning.provision_member = AsyncMock()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision.lookup_owner_email", new=AsyncMock(return_value="o@example.com")),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    _mock_ensure_metronome_customer.assert_awaited_once_with(owner_id="org_xyz")


# ---------------------------------------------------------------------------
# Trial auto-default (Task 3)
# ---------------------------------------------------------------------------
#
# A genuinely-new owner (no billing row) is auto-defaulted into trial_local
# BEFORE the gate is evaluated, so the gate (which now accepts trial_local
# as active + provision-eligible, per Task 2) returns None and the normal
# Paperclip-seed + background_provision flow runs. create_trial_local_if_absent
# is an idempotent no-op if any row already exists (Task 1), so this call is
# safe on every ~3s poll and never overwrites a paid/canceled owner. The gate
# itself stays pure -- this is the only new write, and it lives only here.


@pytest.mark.asyncio
async def test_new_personal_owner_creates_trial_before_gate_and_dispatches_provisioning(
    _mock_create_trial_local_if_absent,
):
    """A no-billing NEW personal owner: create_trial_local_if_absent is
    awaited (with owner_id=user_id, owner_type='personal') BEFORE the gate
    is evaluated, the gate then returns None, and background_provision is
    dispatched."""
    call_order: list[str] = []

    async def fake_create_trial(*, owner_id, owner_type):
        call_order.append("create_trial")
        assert owner_id == "user_p"
        assert owner_type == "personal"

    async def fake_gate(*, owner_id, is_admin):
        call_order.append("gate")
        return None

    _mock_create_trial_local_if_absent.side_effect = fake_create_trial
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", side_effect=fake_gate),
        patch("core.services.user_provision._ensure_paperclip_for_owner", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_personal_auth("user_p", "p@example.com"))

    assert call_order == ["create_trial", "gate"], f"expected create-then-gate ordering, got {call_order}"
    _mock_create_trial_local_if_absent.assert_awaited_once_with(owner_id="user_p", owner_type="personal")
    assert captured == ["background_provision"]


@pytest.mark.asyncio
async def test_org_member_no_billing_does_not_create_trial(_mock_create_trial_local_if_absent):
    """A no-billing NON-ADMIN org member does not create a trial row -- the
    predicate `not auth.is_org_context or auth.is_org_admin` excludes them,
    mirroring the container-dispatch predicate. They ride the org admin's
    billing account."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.provision_member = AsyncMock()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_org_member_auth())

    _mock_create_trial_local_if_absent.assert_not_awaited()


@pytest.mark.asyncio
async def test_org_admin_no_billing_does_not_create_trial(_mock_create_trial_local_if_absent):
    """A no-billing NEW org admin does NOT create a trial row. The free tier is
    personal-only and Teams billing is per-seat/no-trial, so the predicate
    `not auth.is_org_context` excludes every org context -- admin included. The
    org-company provisioning flow still runs; only the card-less trial is
    withheld."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value=True)
    fake_provisioning.provision_org = AsyncMock()
    fake_provisioning.get_company_row = AsyncMock(return_value=MagicMock(status="active", org_id="org_xyz"))

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))

    _mock_create_trial_local_if_absent.assert_not_awaited()


@pytest.mark.asyncio
async def test_org_admin_join_existing_company_creates_no_trial(_mock_create_trial_local_if_absent):
    """An org admin joining an already-provisioned company: no trial row is
    created (orgs never get the card-less trial), and the normal join flow for
    an org whose company already exists proceeds exactly as before."""
    fake_owner_row = MagicMock(status="active", company_id="co_1", user_id="user_first")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_owner_row)
    fake_provisioning.provision_member = AsyncMock()
    fake_provisioning.provision_org = AsyncMock()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.lookup_owner_email", new=AsyncMock(return_value="owner@example.com")),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_org_admin_auth("user_second_admin", "org_xyz", "b@example.com"))

    _mock_create_trial_local_if_absent.assert_not_awaited()
    fake_provisioning.provision_member.assert_awaited_once_with(
        owner_id="org_xyz",
        user_id="user_second_admin",
        email="b@example.com",
        owner_email="owner@example.com",
    )
    fake_provisioning.provision_org.assert_not_awaited()


# ---------------------------------------------------------------------------
# trial_started telemetry (Task 16) — fires ONLY on the call that actually
# creates the row, never on a later idempotent no-op (each ~3s poll calls
# ensure_personal_trial_default again).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_personal_trial_default_fires_trial_started_on_create(
    _mock_create_trial_local_if_absent,
):
    """billing_repo.create_trial_local_if_absent returning True (this call
    actually wrote the row) fires trial_started keyed on the owner id."""
    _mock_create_trial_local_if_absent.return_value = True
    with patch("core.services.user_provision.capture_product_event") as mock_capture:
        await ensure_personal_trial_default(_personal_auth("user_p", "p@example.com"), "user_p")

    mock_capture.assert_called_once_with(distinct_id="user_p", event="trial_started")


@pytest.mark.asyncio
async def test_ensure_personal_trial_default_does_not_refire_on_noop(
    _mock_create_trial_local_if_absent,
):
    """A pre-existing row (the idempotent no-op branch, e.g. the SECOND of
    two ~3s polls) returns False -- must not re-fire trial_started."""
    _mock_create_trial_local_if_absent.return_value = False
    with patch("core.services.user_provision.capture_product_event") as mock_capture:
        await ensure_personal_trial_default(_personal_auth("user_p", "p@example.com"), "user_p")

    mock_capture.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_personal_trial_default_org_context_never_captures(
    _mock_create_trial_local_if_absent,
):
    """Org context never even calls create_trial_local_if_absent (free tier
    is personal-only) so trial_started can never fire for an org."""
    with patch("core.services.user_provision.capture_product_event") as mock_capture:
        await ensure_personal_trial_default(_org_admin_auth("user_admin", "org_xyz", "a@example.com"), "org_xyz")

    _mock_create_trial_local_if_absent.assert_not_awaited()
    mock_capture.assert_not_called()
