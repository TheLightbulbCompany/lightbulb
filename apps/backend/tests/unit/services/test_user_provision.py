"""Unit tests for the unified ensure_user_ready helper."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.auth import AuthContext
from core.services.provision_gate import has_always_on_entitlement
from core.services.user_provision import (
    _ensure_paperclip_for_member,
    _ensure_paperclip_for_org,
    _ensure_paperclip_for_owner,
    _maybe_arm_nightly_ceo,
    ensure_personal_trial_default,
    ensure_user_ready,
)


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


@pytest.fixture(autouse=True)
def _stub_arm_nightly():
    """ensure_user_ready arms the paid nightly CEO routine for owners/admins
    (issue #1273). Stub it by default so the broad orchestration tests never
    touch billing/Paperclip through the arm path. Tests that assert the arm
    invocation inspect this mock; the arm's own behavior is covered by the
    _maybe_arm_nightly_ceo direct tests (which call the real function via the
    imported reference, unaffected by this module-attr patch)."""
    with patch(
        "core.services.user_provision._maybe_arm_nightly_ceo",
        new=AsyncMock(),
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def _no_container_row():
    """``_provision_or_reconcile`` reads the containers row to decide between
    "dispatch the executor" and "reconcile the seed here". Default to NO row
    (a brand-new owner → dispatch), which is what every pre-existing
    orchestration test assumes; the running-container tests override it."""
    with patch(
        "core.services.user_provision.container_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ) as mock:
        yield mock


@pytest.fixture
def _container_running(_no_container_row):
    """Owner already has a RUNNING container: try_claim_provision refuses that
    row, so no executor run — the seed must be reconciled on this thread."""
    _no_container_row.return_value = {"status": "running", "service_name": "openclaw-x"}
    return _no_container_row


@pytest.fixture
def _stub_trial_default():
    """Neutralize the unconditional trial-default prelude for tests that
    exercise ensure_user_ready's provisioning orchestration rather than the
    trial row itself. The trial-default path has its own dedicated tests
    (test_new_personal_owner_* and test_ensure_personal_trial_default_*),
    which call the real helper; everything else stubs it so the personal
    path never reaches billing/Clerk DDB."""
    with patch(
        "core.services.user_provision.ensure_personal_trial_default",
        new=AsyncMock(),
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
async def test_ensure_user_ready_no_op_when_gate_blocks(_stub_trial_default):
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


# ---------------------------------------------------------------------------
# Gate-up org-context membership-add (the orphan-member incident)
# ---------------------------------------------------------------------------
#
# A provision gate (credits_required, oauth_required, …) blocks CONTAINER
# provisioning and company CREATE — it must NOT swallow an org-context
# caller's idempotent membership-add to an already-active shared company.
# Before this branch existed, an org whose Bedrock credits hit zero silently
# orphaned every joining member: the 200-path dispatch from
# /container/status ran, hit the gate, and returned at DEBUG level with the
# member-add never attempted.


def _up_gate(code: str = "credits_required") -> MagicMock:
    gate = MagicMock()
    gate.code = code
    return gate


@pytest.mark.asyncio
async def test_gate_up_org_member_still_gets_membership_add():
    """Gate up + org member + active company → provision_member still runs
    (join-only), and no container is ever dispatched."""
    fake_owner_row = MagicMock(status="active", company_id="co_1", user_id="user_owner")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_owner_row)
    fake_provisioning.provision_member = AsyncMock()
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=_up_gate()),
        ),
        patch("core.services.user_provision.lookup_owner_email", new=AsyncMock(return_value="o@example.com")),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_org_member_auth("user_m", "org_o", "m@example.com"))
    fake_provisioning.provision_member.assert_awaited_once_with(
        owner_id="org_o",
        user_id="user_m",
        email="m@example.com",
        owner_email="o@example.com",
    )
    assert captured == [], f"gate up must never dispatch a container, got {captured}"


@pytest.mark.asyncio
async def test_gate_up_org_admin_join_only_never_creates_company():
    """Gate up + org ADMIN + no company yet → NOTHING is created. The gate-up
    branch is join-only (_ensure_paperclip_for_member returns gracefully when
    no active company exists); company CREATE stays behind the gate."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.provision_org = AsyncMock()
    fake_provisioning.provision_member = AsyncMock()
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value=True)

    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=_up_gate("team_billing_required")),
        ),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    fake_provisioning.provision_org.assert_not_awaited()
    fake_provisioning.try_acquire_org_create_claim.assert_not_awaited()
    fake_provisioning.provision_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_up_membership_add_409_is_idempotent_no_op():
    """Repeat polls while the gate is up: provision_member's 409 (already
    added / concurrent join) is swallowed — the gate-up membership-add is as
    idempotent as the normal-path one."""
    from core.services.paperclip_admin_client import PaperclipApiError

    fake_owner_row = MagicMock(status="active", company_id="co_1", user_id="user_owner")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_owner_row)
    fake_provisioning.provision_member = AsyncMock(
        side_effect=PaperclipApiError("409", status_code=409, body="dup"),
    )

    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=_up_gate()),
        ),
        patch("core.services.user_provision.lookup_owner_email", new=AsyncMock(return_value="o@example.com")),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
    ):
        # Should NOT raise.
        await ensure_user_ready(_org_member_auth("user_m", "org_o", "m@example.com"))
    fake_provisioning.provision_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_gate_up_membership_add_failure_is_swallowed_and_metered():
    """A non-409 failure inside the gate-up membership-add must not escape
    ensure_user_ready (the status poll keeps answering); it is recorded via
    the paperclip.seed.error metric and retried on the next poll."""
    from core.services.paperclip_admin_client import PaperclipApiError

    fake_owner_row = MagicMock(status="active", company_id="co_1", user_id="user_owner")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_owner_row)
    fake_provisioning.provision_member = AsyncMock(
        side_effect=PaperclipApiError("500", status_code=500, body="boom"),
    )

    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=_up_gate()),
        ),
        patch("core.services.user_provision.lookup_owner_email", new=AsyncMock(return_value="o@example.com")),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision.put_metric") as mock_metric,
    ):
        # Should NOT raise.
        await ensure_user_ready(_org_member_auth("user_m", "org_o", "m@example.com"))
    mock_metric.assert_any_call("paperclip.seed.error")


@pytest.mark.asyncio
async def test_gate_up_personal_owner_stays_a_pure_no_op():
    """The gate-up membership-add is org-context only — a gated PERSONAL
    owner must not touch Paperclip at all (no company exists to join, and
    the personal seed is create-shaped, which stays gated)."""
    fake_provisioning = MagicMock()
    build = AsyncMock(return_value=fake_provisioning)
    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=_up_gate("signup_required")),
        ),
        patch("core.services.user_provision.ensure_personal_trial_default", new=AsyncMock()),
        patch("core.services.user_provision.build_paperclip_provisioning", new=build),
        patch("core.services.user_provision.background_provision", new=AsyncMock()) as mock_container,
    ):
        await ensure_user_ready(_personal_auth())
    build.assert_not_awaited()
    mock_container.assert_not_awaited()


# The owner company-CREATE helpers below are exercised DIRECTLY: their sole
# production caller is scripts.provision_runner._seed_owner_paperclip (step 0
# of the provisioning executor's runner), not ensure_user_ready. The runner's
# own wiring — org-vs-personal resolution, the Clerk-creator bootstrap identity
# and the exit-code classification — is covered in
# tests/unit/scripts/test_provision_runner.py.


@pytest.mark.asyncio
async def test_ensure_paperclip_for_owner_personal_calls_provision_org():
    """Personal owner → provision_org with owner_id == user_id."""
    fake_provisioning = MagicMock()
    fake_provisioning.provision_org = AsyncMock()
    with (
        patch(
            "core.services.user_provision.build_paperclip_provisioning",
            new=AsyncMock(return_value=fake_provisioning),
        ),
        patch(
            "core.services.user_provision.close_paperclip_provisioning",
            new=AsyncMock(),
        ),
    ):
        await _ensure_paperclip_for_owner(
            owner_id="user_p",
            owner_user_id="user_p",
            owner_email="p@example.com",
            org_id=None,
        )
    fake_provisioning.provision_org.assert_awaited_once_with(
        owner_id="user_p",
        owner_user_id="user_p",
        owner_email="p@example.com",
    )


@pytest.mark.asyncio
async def test_org_admin_with_no_company_creates_it():
    """Org has NO active Paperclip company yet → provision_org (create).
    Decided by company existence, not role."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value=True)
    fake_provisioning.release_org_create_claim = AsyncMock()
    fake_provisioning.provision_org = AsyncMock()
    fake_provisioning.provision_member = AsyncMock()
    # Post-create verification sees the active row for THIS org → claim kept.
    fake_provisioning.get_company_row = AsyncMock(return_value=MagicMock(status="active", org_id="org_xyz"))

    with (
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
    ):
        await _ensure_paperclip_for_org(org_id="org_xyz", user_id="user_admin", email="a@example.com", is_admin=True)
    fake_provisioning.try_acquire_org_create_claim.assert_awaited_once_with("org_xyz", holder="user_admin")
    fake_provisioning.provision_org.assert_awaited_once_with(
        owner_id="org_xyz",
        owner_user_id="user_admin",
        owner_email="a@example.com",
    )
    fake_provisioning.provision_member.assert_not_awaited()
    fake_provisioning.release_org_create_claim.assert_not_awaited()


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
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
    ):
        await _ensure_paperclip_for_org(org_id="org_xyz", user_id="user_admin", email="a@example.com", is_admin=True)
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
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
    ):
        await _ensure_paperclip_for_org(org_id="org_xyz", user_id="user_admin", email="a@example.com", is_admin=True)
    fake_provisioning.release_org_create_claim.assert_awaited_once_with("org_xyz", holder="user_admin")


@pytest.mark.asyncio
async def test_org_admin_create_claim_lost_skips_paperclip_create():
    """Cross-instance race: the caller sees no company but LOSES the create
    claim (another runner/instance is bootstrapping) → must NOT call
    provision_org or provision_member; the org resolves to the join path once
    the winner's company is visible."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value=False)
    fake_provisioning.release_org_create_claim = AsyncMock()
    fake_provisioning.provision_org = AsyncMock()
    fake_provisioning.provision_member = AsyncMock()

    with (
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
    ):
        await _ensure_paperclip_for_org(org_id="org_xyz", user_id="user_admin", email="a@example.com", is_admin=True)
    fake_provisioning.provision_org.assert_not_awaited()
    fake_provisioning.provision_member.assert_not_awaited()
    fake_provisioning.release_org_create_claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_org_admin_create_failure_releases_claim():
    """If provision_org raises after we won the claim, the claim must be
    RELEASED so a retry (this instance or another) can re-acquire it —
    otherwise a crash/error would strand the org's company forever.

    Exercises the helper directly. ensure_user_ready now wraps the seed in a
    best-effort guard (records + swallows so the container still dispatches —
    see test_seed_failure_does_not_block_container_dispatch), so the raise is
    observed at the _ensure_paperclip_for_org boundary, which is where the
    claim-release contract actually lives."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value=True)
    fake_provisioning.release_org_create_claim = AsyncMock()
    boom = RuntimeError("paperclip down")
    fake_provisioning.provision_org = AsyncMock(side_effect=boom)

    with (
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
    ):
        with pytest.raises(RuntimeError):
            await _ensure_paperclip_for_org(
                org_id="org_xyz", user_id="user_admin", email="a@example.com", is_admin=True
            )
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
async def test_ensure_user_ready_dispatches_container_for_owner(_stub_trial_default):
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
        patch("core.services.user_provision._ensure_paperclip_for_member", new=AsyncMock()),
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
    """Non-409 PaperclipApiError (e.g. 500 from sign_up_user) must propagate
    OUT OF THE HELPER — only the duplicate-409 case is treated as idempotent
    success. A blanket swallow would mask transient Paperclip errors from
    operator visibility.

    Exercises the helper directly. ensure_user_ready now catches this at its
    own seed boundary (best-effort — see
    test_seed_failure_does_not_block_container_dispatch); the 409-vs-non-409
    distinction this test guards lives in _ensure_paperclip_for_member."""
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
            await _ensure_paperclip_for_member(
                org_id="org_456", member_user_id="user_456", member_email="m@example.com"
            )
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_ensure_paperclip_for_owner_idempotent_on_duplicate_409():
    """provision_org raising PaperclipApiError(409) must be treated as
    success — a concurrent seed (a superseded runner attempt still in flight,
    or the legacy paperclip-proxy self-provision door) can collide on
    Paperclip's sign_up_user (Better Auth 409 on duplicate email). Without the
    409 swallow that duplicate would fail the whole provision execution.

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
            "core.services.user_provision.build_paperclip_provisioning",
            new=AsyncMock(return_value=fake_provisioning),
        ),
        patch(
            "core.services.user_provision.close_paperclip_provisioning",
            new=AsyncMock(),
        ),
    ):
        # Should NOT raise.
        await _ensure_paperclip_for_owner(
            owner_id="user_123",
            owner_user_id="user_123",
            owner_email="u@example.com",
            org_id=None,
        )
    fake_provisioning.provision_org.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_paperclip_for_owner_reraises_non_409_paperclip_error():
    """Non-409 PaperclipApiError must propagate OUT OF THE HELPER — only the
    duplicate-409 race is treated as idempotent. A blanket swallow would mask
    transient 5xx and credential errors from operator visibility.

    Exercises the helper directly. ensure_user_ready now catches this at its
    own seed boundary and continues to the container dispatch (best-effort —
    see test_personal_seed_failure_does_not_block_container_dispatch); the
    409-vs-non-409 distinction this test guards lives in
    _ensure_paperclip_for_owner."""
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
            "core.services.user_provision.build_paperclip_provisioning",
            new=AsyncMock(return_value=fake_provisioning),
        ),
        patch(
            "core.services.user_provision.close_paperclip_provisioning",
            new=AsyncMock(),
        ),
    ):
        with pytest.raises(PaperclipApiError) as exc:
            await _ensure_paperclip_for_owner(
                owner_id="user_123",
                owner_user_id="user_123",
                owner_email="u@example.com",
                org_id=None,
            )
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_ensure_paperclip_for_owner_records_duplicate_422_without_reraising(_stub_trial_default):
    """Better Auth returns 422 (NOT 409) for a duplicate email. The owner guard
    must classify that as "user already exists" and route it to the
    recorded-FAILURE path — NOT re-raise it (which hard-fails the seed on every
    ~3s /container/status poll through the generic seed-error handler) and NOT
    swallow it as success (which would report Teams ready over a broken/absent
    workspace). provision_org's own _mark_failed persists a durable ``failed``
    marker (put-if-absent when no row exists), which is what surfaces the
    failure — /teams → 503, teams_status="failed" on /container/status; the
    guard emits a duplicate-specific metric and returns without raising."""
    from core.services.paperclip_admin_client import PaperclipApiError

    fake_provisioning = MagicMock()
    fake_provisioning.provision_org = AsyncMock(
        side_effect=PaperclipApiError(
            "POST /api/auth/sign-up/email -> 422",
            status_code=422,
            body='{"message":"User already exists"}',
        ),
    )
    with (
        patch(
            "core.services.user_provision.build_paperclip_provisioning",
            new=AsyncMock(return_value=fake_provisioning),
        ),
        patch(
            "core.services.user_provision.close_paperclip_provisioning",
            new=AsyncMock(),
        ),
        patch("core.services.user_provision.put_metric") as mock_metric,
    ):
        # MUST NOT raise — a duplicate is an expected terminal outcome, not an
        # error to escalate every poll.
        await _ensure_paperclip_for_owner(
            owner_id="user_123",
            owner_user_id="user_123",
            owner_email="u@example.com",
            org_id=None,
        )
    fake_provisioning.provision_org.assert_awaited_once()
    # Recorded as a duplicate FAILURE — distinct from the 409 success no-op
    # (which emits no metric); proves the 422 is not swallowed as success.
    mock_metric.assert_any_call("paperclip.seed.duplicate_email", dimensions={"path": "owner"})


@pytest.mark.asyncio
async def test_ensure_paperclip_for_member_records_duplicate_422_without_reraising():
    """Symmetric to the owner guard (self-review NEW-1): a member-join 422 —
    Better Auth duplicate email — is a recorded failure, NOT re-raised and NOT
    swallowed as success. Only the 409 concurrent-create race is the idempotent
    success no-op; 422 (the account already exists but we cannot re-provision)
    routes to the recorded-failure path."""
    from core.services.paperclip_admin_client import PaperclipApiError

    fake_company = MagicMock(status="active", company_id="co_x", user_id="user_owner")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_company)
    fake_provisioning.provision_member = AsyncMock(
        side_effect=PaperclipApiError(
            "POST /api/auth/sign-up/email -> 422",
            status_code=422,
            body='{"message":"User already exists"}',
        ),
    )
    with (
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
        patch("core.services.user_provision.put_metric") as mock_metric,
    ):
        # MUST NOT raise.
        await _ensure_paperclip_for_member(org_id="org_456", member_user_id="user_456", member_email="m@example.com")
    fake_provisioning.provision_member.assert_awaited_once()
    mock_metric.assert_any_call("paperclip.seed.duplicate_email", dimensions={"path": "member"})


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
async def test_running_container_skips_container_dispatch_for_admin(_container_running):
    """RUNNING-container poll path: the org admin's poll must run paperclip
    provisioning but NOT dispatch the executor. try_claim_provision refuses a
    running row anyway, so the dispatch could only lose its claim — and the old
    caller-passed provision_container=False flag is gone; the decision comes
    from the row."""
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
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    fake_provisioning.provision_member.assert_awaited_once()
    assert captured == [], f"a running container must skip the executor dispatch, got {captured}"


@pytest.mark.asyncio
async def test_running_container_org_admin_with_company_does_not_recreate(_container_running):
    """The seed reconcile on a running org container is CREATE-ONLY: the
    role-blind JOIN already ran on this thread, so an org whose company exists
    must not re-issue the member-add (a Paperclip round trip + Clerk email
    lookup on every ~3s poll of a healthy org)."""
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
        patch("core.services.user_provision._ensure_paperclip_for_org", new=AsyncMock()) as mock_org,
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    # JOIN ran exactly once (the inline member-add), CREATE did not.
    fake_provisioning.provision_member.assert_awaited_once()
    mock_org.assert_not_awaited()


@pytest.mark.asyncio
async def test_running_container_org_admin_without_company_creates_it(_container_running):
    """Legacy org: container already running but the org has NO paperclip
    company. No provision can run (running rows are unclaimable), so the CREATE
    must happen on this thread or the org sits on /teams 202 forever."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.provision_member = AsyncMock()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision.lookup_owner_email", new=AsyncMock(return_value="o@example.com")),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
        patch("core.services.user_provision._ensure_paperclip_for_org", new=AsyncMock()) as mock_org,
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    mock_org.assert_awaited_once_with(
        org_id="org_xyz",
        user_id="user_admin",
        email="a@example.com",
        is_admin=True,
    )


@pytest.mark.asyncio
async def test_metronome_failure_does_not_block_provisioning(_mock_ensure_metronome_customer, _stub_trial_default):
    """A Metronome 5xx (or any error) on the inline customer-provision call is
    best-effort: the container dispatch still runs and nothing raises. The
    short-circuit keys on the persisted metronome_customer_id, so the next
    /container/status poll retries."""
    _mock_ensure_metronome_customer.side_effect = RuntimeError("metronome 503")
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_personal_auth("user_p", "p@example.com"))
    _mock_ensure_metronome_customer.assert_awaited_once_with(owner_id="user_p")
    assert captured == ["background_provision"]


@pytest.mark.asyncio
async def test_seed_failure_does_not_block_container_dispatch():
    """Org-admin path: a Paperclip (Teams) member-JOIN failure is best-effort —
    it must NOT abort provisioning. background_provision is STILL dispatched
    (the box boots and ecs_manager falls back to the legacy `main` agent when
    no company exists) and the failure is recorded via put_metric, never
    swallowed as success. Mirrors test_metronome_failure_does_not_block_provisioning.

    The owner company-CREATE has no equivalent guard here by design: it runs in
    the provisioning runner's step 0, where a genuine failure is classified into
    the exit-code ABI instead of swallowed — see
    tests/unit/scripts/test_provision_runner.py."""
    from core.services.paperclip_admin_client import PaperclipApiError

    seed_error = PaperclipApiError(
        "POST /api/auth/sign-up/email -> 422",
        status_code=422,
        body='{"message":"unprocessable"}',
    )
    dispatched: list = []

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision._ensure_paperclip_for_member",
            new=AsyncMock(side_effect=seed_error),
        ) as mock_seed,
        patch("core.services.user_provision.background_provision", new=MagicMock()) as mock_bg,
        patch(
            "core.services.user_provision.asyncio.create_task",
            side_effect=lambda coro: dispatched.append(coro),
        ),
        patch("core.services.user_provision.put_metric") as mock_metric,
    ):
        # Must NOT raise despite the seed 422 — that is the whole point.
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))

    mock_seed.assert_awaited_once()
    # Container dispatched despite the 422, keyed on the ORG owner_id.
    mock_bg.assert_called_once_with("org_xyz", "user_admin")
    assert len(dispatched) == 1
    # Failure recorded — not silently swallowed as success.
    mock_metric.assert_any_call("paperclip.seed.error")


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
    _fresh_personal_signup,
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


@pytest.fixture
def _fresh_personal_signup(monkeypatch):
    """Brand-new personal user: no billing row yet, no pending org invitation."""
    monkeypatch.setattr(
        "core.services.user_provision.billing_repo.get_by_owner_id",
        AsyncMock(return_value=None),
    )
    invites = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "core.services.user_provision.clerk_admin.list_pending_invitations_for_user",
        invites,
    )
    return invites


@pytest.mark.asyncio
async def test_ensure_personal_trial_default_fires_trial_started_on_create(
    _mock_create_trial_local_if_absent,
    _fresh_personal_signup,
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
    _fresh_personal_signup,
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


# ---------------------------------------------------------------------------
# Tenancy invariant (Gate B): a user with a pending org invitation may not take
# a personal plan — they must accept the invite and become a seat.
# /billing/trial-checkout has always enforced this. The auto-trial default runs
# on every /container/status poll, so without the same check it is simply a way
# around that gate: the invited user need only let the poll run to land a
# personal trial_local row + a personal container.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_org_invitation_refuses_the_personal_trial_default(
    _mock_create_trial_local_if_absent,
    monkeypatch,
):
    monkeypatch.setattr(
        "core.services.user_provision.billing_repo.get_by_owner_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "core.services.user_provision.clerk_admin.list_pending_invitations_for_user",
        AsyncMock(return_value=[{"public_organization_data": {"name": "Acme"}}]),
    )

    with patch("core.services.user_provision.capture_product_event") as mock_capture:
        await ensure_personal_trial_default(_personal_auth("user_p", "p@example.com"), "user_p")

    _mock_create_trial_local_if_absent.assert_not_awaited()
    mock_capture.assert_not_called()


@pytest.mark.asyncio
async def test_an_existing_billing_row_short_circuits_before_the_clerk_call(
    _mock_create_trial_local_if_absent,
    monkeypatch,
):
    """This helper runs on EVERY ~3s /container/status poll. The invitation
    lookup is a Clerk API call and must not fire on each one — an existing
    billing row means there is nothing to create, so we never reach Clerk."""
    monkeypatch.setattr(
        "core.services.user_provision.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_p", "subscription_status": "trial_local"}),
    )
    invites = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "core.services.user_provision.clerk_admin.list_pending_invitations_for_user",
        invites,
    )

    await ensure_personal_trial_default(_personal_auth("user_p", "p@example.com"), "user_p")

    invites.assert_not_awaited()
    _mock_create_trial_local_if_absent.assert_not_awaited()


# ---------------------------------------------------------------------------
# Nightly CEO loop arming (issue #1273)
# ---------------------------------------------------------------------------
#
# The arm step is paid-only (has_always_on_entitlement), idempotent (cache +
# conditional claim + title-skip), and opt-out-durable. These call the REAL
# _maybe_arm_nightly_ceo (imported reference, unaffected by the autouse stub)
# and patch its collaborators on the user_provision module.


def _arm_provisioning(row):
    """A fake PaperclipProvisioning whose company-row lookup returns ``row``.

    Personal owners read via get_company_row(user_id); org owners via
    find_org_owner(owner_id) — stub both so either branch resolves to ``row``.
    """
    fake = MagicMock()
    fake.find_org_owner = AsyncMock(return_value=row)
    fake.get_company_row = AsyncMock(return_value=row)
    fake._admin = MagicMock()  # ._http is just an attribute access
    return fake


def _paid_account(**extra):
    return {"owner_id": "user_p", "subscription_status": "active", **extra}


def _company_row(company_id="co1", status="active", friday="friday1", atlas="atlas1"):
    seeded = [x for x in (friday, atlas) if x is not None] or None
    return MagicMock(company_id=company_id, status=status, seeded_agent_ids=seeded)


@pytest.mark.asyncio
async def test_arm_paid_seeds_nightly_routine_for_friday():
    """Paid + active company + Friday seeded → seed the enabled nightly routine
    with Friday's id, the frozen title, and cron 0 8 * * *."""
    provisioning = _arm_provisioning(_company_row())
    seed = AsyncMock(return_value="rid-nightly")
    with (
        patch("core.services.user_provision.billing_repo.get_by_owner_id", AsyncMock(return_value=_paid_account())),
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.user_provision.get_admin_session_cookie", AsyncMock(return_value="admin-cookie")),
        patch("core.services.user_provision.billing_repo.try_claim_nightly_arm", AsyncMock(return_value=True)),
        patch("core.services.user_provision.create_enabled_schedule_routine", seed),
    ):
        await _maybe_arm_nightly_ceo(_personal_auth("user_p"), "user_p")

    seed.assert_awaited_once()
    kwargs = seed.await_args.kwargs
    assert kwargs["agent_id"] == "friday1"
    assert kwargs["company_id"] == "co1"
    assert kwargs["title"] == "Nightly CEO progress"
    assert kwargs["cron"] == "0 8 * * *"
    assert kwargs["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_arm_trial_never_armed():
    """A trial_local owner returns before ANY Paperclip call."""
    build = AsyncMock()
    with (
        patch(
            "core.services.user_provision.billing_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": "user_p", "subscription_status": "trial_local"}),
        ),
        patch("core.services.user_provision.build_paperclip_provisioning", build),
    ):
        await _maybe_arm_nightly_ceo(_personal_auth("user_p"), "user_p")
    build.assert_not_awaited()
    # Sanity: the entitlement predicate itself excludes trial_local.
    assert has_always_on_entitlement({"subscription_status": "trial_local"}) is False


@pytest.mark.asyncio
async def test_arm_no_account_never_armed():
    """Fail-closed: no billing row → no Paperclip call."""
    build = AsyncMock()
    with (
        patch("core.services.user_provision.billing_repo.get_by_owner_id", AsyncMock(return_value=None)),
        patch("core.services.user_provision.build_paperclip_provisioning", build),
    ):
        await _maybe_arm_nightly_ceo(_personal_auth("user_p"), "user_p")
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_arm_opt_out_honored():
    """nightly_ceo_opted_out=True → no seed even when paid + active."""
    build = AsyncMock()
    with (
        patch(
            "core.services.user_provision.billing_repo.get_by_owner_id",
            AsyncMock(return_value=_paid_account(nightly_ceo_opted_out=True)),
        ),
        patch("core.services.user_provision.build_paperclip_provisioning", build),
    ):
        await _maybe_arm_nightly_ceo(_personal_auth("user_p"), "user_p")
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_arm_cache_hit_skips_claim_and_seed():
    """armed_company already == current company → no claim, no seed."""
    provisioning = _arm_provisioning(_company_row(company_id="co1"))
    claim = AsyncMock(return_value=True)
    seed = AsyncMock()
    with (
        patch(
            "core.services.user_provision.billing_repo.get_by_owner_id",
            AsyncMock(return_value=_paid_account(nightly_ceo_armed_company="co1")),
        ),
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.user_provision.billing_repo.try_claim_nightly_arm", claim),
        patch("core.services.user_provision.create_enabled_schedule_routine", seed),
    ):
        await _maybe_arm_nightly_ceo(_personal_auth("user_p"), "user_p")
    claim.assert_not_awaited()
    seed.assert_not_awaited()


@pytest.mark.asyncio
async def test_arm_friday_not_seeded_yet_skips():
    """Company active but no seeded_agent_ids (Friday not seeded yet) → skip;
    a later poll retries."""
    provisioning = _arm_provisioning(_company_row(friday=None, atlas=None))
    claim = AsyncMock(return_value=True)
    seed = AsyncMock()
    with (
        patch("core.services.user_provision.billing_repo.get_by_owner_id", AsyncMock(return_value=_paid_account())),
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.user_provision.billing_repo.try_claim_nightly_arm", claim),
        patch("core.services.user_provision.create_enabled_schedule_routine", seed),
    ):
        await _maybe_arm_nightly_ceo(_personal_auth("user_p"), "user_p")
    claim.assert_not_awaited()
    seed.assert_not_awaited()


@pytest.mark.asyncio
async def test_arm_company_not_active_skips():
    """A failed/provisioning company row (status != active) → never armed
    (closes the orphan-company cost leak)."""
    provisioning = _arm_provisioning(_company_row(status="failed"))
    seed = AsyncMock()
    with (
        patch("core.services.user_provision.billing_repo.get_by_owner_id", AsyncMock(return_value=_paid_account())),
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.user_provision.create_enabled_schedule_routine", seed),
    ):
        await _maybe_arm_nightly_ceo(_personal_auth("user_p"), "user_p")
    seed.assert_not_awaited()


@pytest.mark.asyncio
async def test_arm_concurrency_only_one_seeds():
    """Two concurrent arms for one owner → try_claim_nightly_arm elects exactly
    one winner; exactly ONE nightly routine is seeded."""
    import asyncio as _asyncio

    provisioning = _arm_provisioning(_company_row())

    claim_results = [True, False]

    async def one_winner(owner_id, company_id):
        return claim_results.pop(0) if claim_results else False

    seed = AsyncMock(return_value="rid-nightly")
    with (
        patch("core.services.user_provision.billing_repo.get_by_owner_id", AsyncMock(return_value=_paid_account())),
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.user_provision.get_admin_session_cookie", AsyncMock(return_value="admin-cookie")),
        patch("core.services.user_provision.billing_repo.try_claim_nightly_arm", side_effect=one_winner),
        patch("core.services.user_provision.create_enabled_schedule_routine", seed),
    ):
        await _asyncio.gather(
            _maybe_arm_nightly_ceo(_personal_auth("user_p"), "user_p"),
            _maybe_arm_nightly_ceo(_personal_auth("user_p"), "user_p"),
        )
    assert seed.await_count == 1


@pytest.mark.asyncio
async def test_arm_seed_failure_releases_claim_and_never_raises():
    """A seed that raises → release_nightly_arm called and _maybe_arm_nightly_ceo
    returns normally (best-effort; ensure_user_ready must keep answering)."""
    provisioning = _arm_provisioning(_company_row())
    release = AsyncMock()
    with (
        patch("core.services.user_provision.billing_repo.get_by_owner_id", AsyncMock(return_value=_paid_account())),
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.user_provision.get_admin_session_cookie", AsyncMock(return_value="admin-cookie")),
        patch("core.services.user_provision.billing_repo.try_claim_nightly_arm", AsyncMock(return_value=True)),
        patch("core.services.user_provision.billing_repo.release_nightly_arm", release),
        patch(
            "core.services.user_provision.create_enabled_schedule_routine",
            AsyncMock(side_effect=RuntimeError("paperclip down")),
        ),
    ):
        # Must NOT raise.
        await _maybe_arm_nightly_ceo(_personal_auth("user_p"), "user_p")
    release.assert_awaited_once_with("user_p", "co1")


@pytest.mark.asyncio
async def test_arm_org_admin_uses_find_org_owner():
    """Org context resolves the company via find_org_owner(owner_id)."""
    provisioning = _arm_provisioning(_company_row(company_id="co_org"))
    seed = AsyncMock(return_value="rid-nightly")
    with (
        patch(
            "core.services.user_provision.billing_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": "org_xyz", "subscription_status": "active"}),
        ),
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.user_provision.get_admin_session_cookie", AsyncMock(return_value="admin-cookie")),
        patch("core.services.user_provision.billing_repo.try_claim_nightly_arm", AsyncMock(return_value=True)),
        patch("core.services.user_provision.create_enabled_schedule_routine", seed),
    ):
        await _maybe_arm_nightly_ceo(_org_admin_auth("user_admin", "org_xyz", "a@example.com"), "org_xyz")
    provisioning.find_org_owner.assert_awaited_once_with("org_xyz")
    seed.assert_awaited_once()
    assert seed.await_args.kwargs["company_id"] == "co_org"


# --- Invocation gating (uses the autouse _stub_arm_nightly mock) -------------


@pytest.mark.asyncio
async def test_ensure_user_ready_invokes_arm_for_personal_owner(_stub_trial_default, _stub_arm_nightly):
    """Owner path → _maybe_arm_nightly_ceo awaited once with (auth, owner_id)."""
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision._ensure_paperclip_for_owner", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_personal_auth("user_p"))
    _stub_arm_nightly.assert_awaited_once()
    assert _stub_arm_nightly.await_args.args[1] == "user_p"


@pytest.mark.asyncio
async def test_ensure_user_ready_invokes_arm_for_org_admin(_stub_arm_nightly):
    """Org admin path → arm awaited with the ORG owner_id."""
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision._ensure_paperclip_for_member", new=AsyncMock()),
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    _stub_arm_nightly.assert_awaited_once()
    assert _stub_arm_nightly.await_args.args[1] == "org_xyz"


@pytest.mark.asyncio
async def test_ensure_user_ready_does_not_invoke_arm_for_org_member(_stub_arm_nightly):
    """Org members ride the org's container and never arm."""
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
    _stub_arm_nightly.assert_not_awaited()


# ---------------------------------------------------------------------------
# Owner seed placement. The OWNER company-create normally runs in the
# executor's runner (scripts.provision_runner step 0) — that is the #1442 fix:
# the duplicate-company source (a load-balanced poll fan-out each running
# provision_org) is gone from the poll thread. The ONE case that stays here is
# the state where no provision will run at all: the container is already
# running (try_claim_provision refuses that row) or is a deliberately parked
# trial box (which a passive read must not wake).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_personal_no_container_skips_inline_owner_seed_only_dispatches(_stub_trial_default):
    """No container row yet (the new-signup path): NO inline owner seed (the
    runner's step 0 does it), NO member call (personal has no join), and the
    container is dispatched exactly once."""
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    inline_owner = AsyncMock()
    inline_org = AsyncMock()
    inline_member = AsyncMock()
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision._ensure_paperclip_for_owner", new=inline_owner),
        patch("core.services.user_provision._ensure_paperclip_for_org", new=inline_org),
        patch("core.services.user_provision._ensure_paperclip_for_member", new=inline_member),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_personal_auth("user_p"))
    inline_owner.assert_not_awaited()
    inline_org.assert_not_awaited()
    inline_member.assert_not_awaited()
    assert captured == ["background_provision"]


@pytest.mark.asyncio
async def test_org_admin_does_member_join_never_inline_create(_stub_trial_default):
    """Org admin, no container row: the poll thread does the role-blind member
    JOIN (member-add STAYS here) and NEVER the inline company create — the
    runner's step 0 owns it. Container still dispatched (admin owns the box)."""
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    inline_org = AsyncMock()
    inline_owner = AsyncMock()
    inline_member = AsyncMock()
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision._ensure_paperclip_for_org", new=inline_org),
        patch("core.services.user_provision._ensure_paperclip_for_owner", new=inline_owner),
        patch("core.services.user_provision._ensure_paperclip_for_member", new=inline_member),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    inline_org.assert_not_awaited()
    inline_owner.assert_not_awaited()
    inline_member.assert_awaited_once_with(
        org_id="org_xyz",
        member_user_id="user_admin",
        member_email="a@example.com",
    )
    assert captured == ["background_provision"]


@pytest.mark.asyncio
async def test_gate_up_org_member_branch_is_unchanged():
    """The gate-up member branch (#1435) is orthogonal to where the owner seed
    runs: with the gate UP, an org member still gets the idempotent
    membership-add and no container is dispatched."""
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=_up_gate())),
        patch("core.services.user_provision._ensure_paperclip_for_member", new=AsyncMock()) as member,
        patch("core.services.user_provision.background_provision", new=AsyncMock()) as container,
    ):
        await ensure_user_ready(_org_member_auth("user_member", "org_456", "m@example.com"))
    member.assert_awaited_once_with(
        org_id="org_456",
        member_user_id="user_member",
        member_email="m@example.com",
    )
    container.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_polls_run_no_inline_owner_seed(_stub_trial_default):
    """The #1442 fix at the poll boundary: 15 concurrent /container/status polls
    for an owner with no container run ZERO inline provision_org — the
    duplicate-company source is gone. Each poll instead dispatches, and the
    executor's try_claim_provision (proven in test_provision_dispatch) collapses
    the 15 dispatches to one execution -> one runner -> one seed."""
    dispatched: list[str] = []

    def capture_task(coro):
        dispatched.append(coro.__qualname__)
        coro.close()

    inline_owner = AsyncMock()
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision._ensure_paperclip_for_owner", new=inline_owner),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await asyncio.gather(*[ensure_user_ready(_personal_auth("user_race")) for _ in range(15)])
    inline_owner.assert_not_awaited()
    assert dispatched == ["background_provision"] * 15


# ---------------------------------------------------------------------------
# Self-heal on a box that is already up: the two regressions this design
# exists to prevent. Both used to be served by an INLINE seed reached through
# ensure_user_ready(provision_container=False); with the seed in the executor
# and the executor refusing a running row, "dispatch and hope" seeds nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_container_personal_owner_seed_is_reconciled(_stub_trial_default, _container_running):
    """REGRESSION (/container/status): a PERSONAL owner with a running container
    and teams_state == "failed" is re-dispatched on every poll. The executor
    cannot help (running rows are unclaimable), so the seed must run HERE or a
    failed personal Teams seed never heals."""
    inline_owner = AsyncMock()
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision._ensure_paperclip_for_owner", new=inline_owner),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_personal_auth("user_p", "p@example.com"))
    inline_owner.assert_awaited_once_with(
        owner_id="user_p",
        owner_user_id="user_p",
        owner_email="p@example.com",
        org_id=None,
    )
    assert captured == [], f"a running container must not be re-provisioned, got {captured}"


@pytest.mark.asyncio
async def test_parked_trial_box_seeds_without_waking_the_container(_stub_trial_default, _no_container_row):
    """REGRESSION (/teams): a personal owner entering Teams on a deliberately
    parked trial box must get the seed WITHOUT a container start — a passive
    read never wakes a parked box (wake_policy is the single predicate), but it
    must not leave the workspace unseeded either (that was the infinite 202)."""
    _no_container_row.return_value = {"status": "stopped", "stop_reason": "trial_idle"}
    inline_owner = AsyncMock()
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision.wake_policy.is_parked_trial", new=AsyncMock(return_value=True)),
        patch("core.services.user_provision.wake_policy.note_wake_suppressed") as note,
        patch("core.services.user_provision._ensure_paperclip_for_owner", new=inline_owner),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_personal_auth("user_p", "p@example.com"))
    inline_owner.assert_awaited_once()
    note.assert_called_once()
    assert captured == [], f"a parked trial box must never be woken by a passive read, got {captured}"


@pytest.mark.asyncio
async def test_stopped_but_not_parked_container_still_dispatches(_stub_trial_default, _no_container_row):
    """A crashed/stopped-but-NOT-parked box keeps its poll-driven self-heal: the
    executor is dispatched (and its step 0 does the seed), so nothing is seeded
    on this thread."""
    _no_container_row.return_value = {"status": "stopped"}
    inline_owner = AsyncMock()
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision._ensure_paperclip_for_owner", new=inline_owner),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_personal_auth("user_p", "p@example.com"))
    inline_owner.assert_not_awaited()
    assert captured == ["background_provision"]


@pytest.mark.asyncio
async def test_running_container_seed_failure_is_swallowed_and_metered(_stub_trial_default, _container_running):
    """The reconcile is best-effort like every other Paperclip step here: a
    failure records paperclip.seed.error and never escapes the poll."""
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision._ensure_paperclip_for_owner",
            new=AsyncMock(side_effect=RuntimeError("paperclip 502")),
        ),
        patch("core.services.user_provision.put_metric") as mock_metric,
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_personal_auth("user_p", "p@example.com"))
    mock_metric.assert_any_call("paperclip.seed.error")
