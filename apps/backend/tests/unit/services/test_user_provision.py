"""Unit tests for the unified ensure_user_ready helper."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.auth import AuthContext
from core.services import user_provision
from core.services.provision_gate import should_arm_nightly_ceo
from core.services.user_provision import (
    _ensure_paperclip_for_member,
    _ensure_paperclip_for_org,
    _ensure_paperclip_for_owner,
    arm_nightly_ceo,
    ensure_user_ready,
)


@pytest.fixture(autouse=True)
def _existing_billing_account():
    with patch.object(
        user_provision.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})
    ):
        yield


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
def _no_container_row():
    """``ensure_user_ready`` reads the containers row: only a RUNNING workspace gets the
    in-request owner seed reconcile. Default to NO row; the running tests override it."""
    with patch(
        "core.services.user_provision.container_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ) as mock:
        yield mock


@pytest.fixture
def _container_running(_no_container_row):
    """Owner already has a RUNNING container: no executor run is dispatched, so
    the seed must be reconciled on this thread."""
    _no_container_row.return_value = {"status": "running", "service_name": "openclaw-x"}
    return _no_container_row


@pytest.fixture
def _stub_trial_default():
    """Keep billing initialization and member JOIN external to dispatch tests.

    The workspace trial tests below call the real initializer. Membership
    tests override the join stub with their own Paperclip boundary doubles.
    """
    with (
        patch(
            "core.services.user_provision.ensure_workspace_trial_default",
            new=AsyncMock(),
        ) as mock,
        patch.object(user_provision, "_ensure_paperclip_for_member", AsyncMock()),
    ):
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
            "core.services.user_provision.dispatch_provision",
            new=AsyncMock(),
        ) as mock_container,
    ):
        await ensure_user_ready(_org_admin_auth())
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
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value="tok-1")

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
async def test_ensure_paperclip_for_owner_calls_provision_org():
    """Org owner → provision_org with the creator as owner-of-record, then the
    default room for the fresh workspace."""
    starter_ids = {
        key: f"agent-{key}" for key in ("ceo", "engineering", "finance", "growth", "analytics", "customer_operations")
    }
    fake_provisioning = MagicMock()
    fake_provisioning.get_company_row = AsyncMock(return_value=None)
    fake_provisioning.provision_org = AsyncMock(return_value=MagicMock(status="active", starter_agent_ids=starter_ids))
    ensure_default_room = AsyncMock()
    with (
        patch(
            "core.services.user_provision.build_paperclip_provisioning",
            new=AsyncMock(return_value=fake_provisioning),
        ),
        patch(
            "core.services.user_provision.close_paperclip_provisioning",
            new=AsyncMock(),
        ),
        patch("core.services.messaging.ensure_default_room", ensure_default_room),
    ):
        await _ensure_paperclip_for_owner(
            owner_id="org_p",
            owner_user_id="user_p",
            owner_email="p@example.com",
        )
    fake_provisioning.provision_org.assert_awaited_once_with(
        owner_id="org_p",
        owner_user_id="user_p",
        owner_email="p@example.com",
    )
    ensure_default_room.assert_awaited_once_with("org_p", "user_p")


@pytest.mark.asyncio
async def test_existing_active_starter_workspace_does_not_gain_default_room():
    """A healthy pre-existing workspace is not fresh onboarding evidence."""
    starter_ids = {
        key: f"agent-{key}" for key in ("ceo", "engineering", "finance", "growth", "analytics", "customer_operations")
    }
    existing = MagicMock(status="active", org_id="org_p", starter_agent_ids=starter_ids)
    fake_provisioning = MagicMock()
    fake_provisioning.get_company_row = AsyncMock(return_value=existing)
    fake_provisioning.provision_org = AsyncMock(return_value=existing)
    ensure_default_room = AsyncMock()
    with (
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=fake_provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.messaging.ensure_default_room", ensure_default_room),
    ):
        await _ensure_paperclip_for_owner(
            owner_id="org_p",
            owner_user_id="user_p",
            owner_email="p@example.com",
        )

    ensure_default_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_workspace_row_does_not_suppress_new_org_default_room():
    """A row for another workspace is absent for org onboarding freshness."""
    starter_ids = {
        key: f"agent-{key}" for key in ("ceo", "engineering", "finance", "growth", "analytics", "customer_operations")
    }
    other = MagicMock(status="active", org_id="org_old", starter_agent_ids=starter_ids)
    new_org = MagicMock(status="active", org_id="org_new", starter_agent_ids=starter_ids)
    fake_provisioning = MagicMock()
    fake_provisioning.get_company_row = AsyncMock(side_effect=[other, new_org])
    fake_provisioning.provision_org = AsyncMock(return_value=new_org)
    ensure_default_room = AsyncMock()
    with (
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=fake_provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.messaging.ensure_default_room", ensure_default_room),
    ):
        for _ in range(2):
            await _ensure_paperclip_for_owner(
                owner_id="org_new",
                owner_user_id="user_p",
                owner_email="p@example.com",
            )

    ensure_default_room.assert_awaited_once_with("org_new", "user_p")


@pytest.mark.asyncio
async def test_partial_starter_workspace_gets_default_room_after_completion():
    """A partial starter retry becomes eligible only after its map completes."""
    complete = {
        key: f"agent-{key}" for key in ("ceo", "engineering", "finance", "growth", "analytics", "customer_operations")
    }
    partial = MagicMock(status="failed", starter_agent_ids={**complete, "analytics": None})
    fake_provisioning = MagicMock()
    fake_provisioning.get_company_row = AsyncMock(return_value=partial)
    fake_provisioning.provision_org = AsyncMock(return_value=MagicMock(status="active", starter_agent_ids=complete))
    ensure_default_room = AsyncMock()
    with (
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=fake_provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.messaging.ensure_default_room", ensure_default_room),
    ):
        await _ensure_paperclip_for_owner(
            owner_id="org_p",
            owner_user_id="user_p",
            owner_email="p@example.com",
        )

    ensure_default_room.assert_awaited_once_with("org_p", "user_p")


@pytest.mark.asyncio
async def test_org_admin_with_no_company_creates_it():
    """Org has NO active Paperclip company yet → provision_org (create).
    Decided by company existence, not role."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value="tok-1")
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
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value="tok-1")
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
    fake_provisioning.release_org_create_claim.assert_awaited_once_with("org_xyz", token="tok-1")


@pytest.mark.asyncio
async def test_org_admin_create_other_workspace_row_mismatch_releases_claim():
    """If provision_org short-circuits on the admin's pre-existing row for
    ANOTHER workspace (rows are keyed per-user), the strongly-consistent read
    returns an active row whose org_id is NOT this org. That must NOT count as a
    successful org create — release the claim so it isn't held with no
    org-indexed company."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value="tok-1")
    fake_provisioning.release_org_create_claim = AsyncMock()
    fake_provisioning.provision_org = AsyncMock()
    # Active row exists, but it belongs to another workspace.
    fake_provisioning.get_company_row = AsyncMock(
        return_value=MagicMock(status="active", org_id="org_other"),
    )
    fake_provisioning.provision_member = AsyncMock()

    with (
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
    ):
        await _ensure_paperclip_for_org(org_id="org_xyz", user_id="user_admin", email="a@example.com", is_admin=True)
    fake_provisioning.release_org_create_claim.assert_awaited_once_with("org_xyz", token="tok-1")


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
    fake_provisioning.try_acquire_org_create_claim = AsyncMock(return_value="tok-1")
    fake_provisioning.release_org_create_claim = AsyncMock()
    fake_provisioning.get_company_row = AsyncMock(return_value=None)
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
    fake_provisioning.release_org_create_claim.assert_awaited_once_with("org_xyz", token="tok-1")


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
# The container dispatch_provision is dispatched as an asyncio task
# only on the owner path. Org members ride the owner's container, so
# ensure_user_ready must NOT dispatch a container task for them.


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
    fake_provisioning.get_company_row = AsyncMock(return_value=None)
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
            owner_id="org_123",
            owner_user_id="user_123",
            owner_email="u@example.com",
        )
    fake_provisioning.provision_org.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_paperclip_for_owner_reraises_non_409_paperclip_error():
    """Non-409 PaperclipApiError must propagate OUT OF THE HELPER — only the
    duplicate-409 race is treated as idempotent. A blanket swallow would mask
    transient 5xx and credential errors from operator visibility.

    Exercises the helper directly. ensure_user_ready now catches this at its
    own seed boundary and continues to the container dispatch (best-effort —
    see test_seed_failure_does_not_block_container_dispatch); the
    409-vs-non-409 distinction this test guards lives in
    _ensure_paperclip_for_owner."""
    from core.services.paperclip_admin_client import PaperclipApiError

    fake_provisioning = MagicMock()
    fake_provisioning.get_company_row = AsyncMock(return_value=None)
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
                owner_id="org_123",
                owner_user_id="user_123",
                owner_email="u@example.com",
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
    fake_provisioning.get_company_row = AsyncMock(return_value=None)
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
            owner_id="org_123",
            owner_user_id="user_123",
            owner_email="u@example.com",
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
    provisioning but NOT dispatch the executor — the old caller-passed
    provision_container=False flag is gone; the decision comes from the row."""
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


# Workspace trial initialization.


@pytest.fixture
def _fresh_workspace_signup(monkeypatch):
    monkeypatch.setattr(user_provision.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", [_org_admin_auth(), _org_member_auth()])
async def test_workspace_trial_is_unarmed_and_shared_by_all_members(
    auth, _fresh_workspace_signup, _mock_create_trial_local_if_absent
):
    _mock_create_trial_local_if_absent.return_value = True
    with patch.object(user_provision, "capture_product_event") as capture:
        await user_provision.ensure_workspace_trial_default(auth, "org_456")
    _mock_create_trial_local_if_absent.assert_awaited_once_with(owner_id="org_456", owner_type="org", trial_end=None)
    event = capture.call_args.kwargs
    assert event["distinct_id"] == "org_456"
    assert event["event"] == "trial_started"
    assert event["properties"] == {"owner_type": "org"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth,workspace_id,status",
    [
        (_personal_auth(), "user_123", 403),
        # A personal workspace id is refused even from an org-context caller:
        # this helper only ever CREATES, and no new personal workspace exists.
        (_org_admin_auth(), "user_123", 403),
        (_org_admin_auth(), "org_other", 403),
    ],
)
async def test_workspace_trial_rejects_missing_or_mismatched_org(
    auth, workspace_id, status, _mock_create_trial_local_if_absent
):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await user_provision.ensure_workspace_trial_default(auth, workspace_id)
    assert exc.value.status_code == status
    _mock_create_trial_local_if_absent.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_org_returns_before_the_gate_and_creates_nothing(_stub_trial_default):
    """The workspace-first fence: a personal (no-org) caller leaves
    ``ensure_user_ready`` as its FIRST statement — before the JOIN, the gate and
    the seed — so no personal world can be built for anyone who signs up alone."""
    gate = AsyncMock(return_value=_up_gate())
    with (
        patch.object(user_provision, "evaluate_provision_gate", gate),
        patch.object(user_provision, "_reconcile_owner_seed", AsyncMock()) as seed,
    ):
        assert await ensure_user_ready(_personal_auth()) is None
    gate.assert_not_awaited()
    seed.assert_not_awaited()
    user_provision._ensure_paperclip_for_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_trial_existing_row_and_lost_create_do_not_emit_started(
    _fresh_workspace_signup, _mock_create_trial_local_if_absent
):
    _mock_create_trial_local_if_absent.return_value = False
    with patch.object(user_provision, "capture_product_event") as capture:
        await user_provision.ensure_workspace_trial_default(_org_admin_auth(), "org_456")
        with patch.object(
            user_provision.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})
        ):
            await user_provision.ensure_workspace_trial_default(_org_admin_auth(), "org_456")
    _mock_create_trial_local_if_absent.assert_awaited_once()
    capture.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_trial_write_failure_is_reported_once_per_day(
    _fresh_workspace_signup, _mock_create_trial_local_if_absent
):
    from core.observability import product_analytics

    _mock_create_trial_local_if_absent.side_effect = RuntimeError("ddb down")
    product_analytics._daily_dedup_seen.clear()
    try:
        with patch.object(product_analytics, "capture_product_event") as capture:
            for _ in range(3):
                with pytest.raises(RuntimeError, match="ddb down"):
                    await user_provision.ensure_workspace_trial_default(_org_admin_auth(), "org_456")
        assert [c.kwargs["event"] for c in capture.call_args_list] == ["trial_start_requested", "trial_start_failed"]
        assert capture.call_args.kwargs["properties"] == {"error_code": "RuntimeError"}
        assert all(c.kwargs["distinct_id"] == "org_456" for c in capture.call_args_list)
    finally:
        product_analytics._daily_dedup_seen.clear()


@pytest.mark.asyncio
async def test_workspace_trial_started_event_dedups_across_members(
    _fresh_workspace_signup, _mock_create_trial_local_if_absent
):
    _mock_create_trial_local_if_absent.return_value = True
    with patch.object(user_provision, "capture_product_event") as capture:
        await user_provision.ensure_workspace_trial_default(_org_admin_auth(), "org_456")
        await user_provision.ensure_workspace_trial_default(_org_member_auth(), "org_456")
    first, second = [c.kwargs for c in capture.call_args_list]
    assert first["properties"] == {"owner_type": "org"}
    assert first["event_uuid"] == second["event_uuid"]


# ---------------------------------------------------------------------------
# Nightly CEO loop arming (issue #1273)
# ---------------------------------------------------------------------------
#
# The arm step is gated on should_arm_nightly_ceo (paid OR an unexpired trial),
# idempotent (cache + conditional claim + title-skip), and opt-out-durable.
# These call the REAL
# arm_nightly_ceo and patch its collaborators on the user_provision module.


def _arm_provisioning(row):
    """A fake PaperclipProvisioning whose authoritative owner resolver returns
    ``row`` (``get_company_row`` / ``find_org_owner`` too, so a test can prove
    the arm never reads them)."""
    fake = MagicMock()
    fake.find_org_owner = AsyncMock(return_value=row)
    fake.get_company_row = AsyncMock(return_value=row)
    fake._repo = MagicMock()
    fake._repo.resolve_owner_row = AsyncMock(return_value=row)
    fake._admin = MagicMock()  # ._http is just an attribute access
    return fake


def _paid_account(**extra):
    return {"owner_id": "org_p", "subscription_status": "active", **extra}


def _company_row(company_id="co1", status="active", friday="friday1", atlas="atlas1", starter_ids=None, org_id="org_p"):
    """``org_id`` is the workspace the row belongs to."""
    seeded = [x for x in (friday, atlas) if x is not None] or None
    return MagicMock(
        org_id=org_id,
        company_id=company_id,
        status=status,
        seeded_agent_ids=seeded,
        starter_agent_ids=starter_ids,
    )


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
        await arm_nightly_ceo("org_p")

    seed.assert_awaited_once()
    kwargs = seed.await_args.kwargs
    assert kwargs["agent_id"] == "friday1"
    assert kwargs["company_id"] == "co1"
    assert kwargs["title"] == "Nightly CEO progress"
    assert kwargs["cron"] == "0 8 * * *"
    assert kwargs["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_arm_paid_prefers_starter_ceo_for_nightly_routine():
    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "starter-ceo"}))
    seed = AsyncMock(return_value="rid-nightly")
    with (
        patch("core.services.user_provision.billing_repo.get_by_owner_id", AsyncMock(return_value=_paid_account())),
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.user_provision.get_admin_session_cookie", AsyncMock(return_value="admin-cookie")),
        patch("core.services.user_provision.billing_repo.try_claim_nightly_arm", AsyncMock(return_value=True)),
        patch("core.services.user_provision.create_enabled_schedule_routine", seed),
    ):
        await arm_nightly_ceo("org_p")

    assert seed.await_args.kwargs["agent_id"] == "starter-ceo"


@pytest.mark.asyncio
async def test_arm_active_trial_now_seeds():
    """An UNEXPIRED trial_local is now armed (the retention change) — trials get
    the nightly CEO through the same Paperclip path as paid, with reconcile on."""
    provisioning = _arm_provisioning(_company_row())
    seed = AsyncMock(return_value="rid-nightly")
    trial = {"owner_id": "org_p", "subscription_status": "trial_local", "trial_end": int(time.time()) + 86400}
    with (
        patch("core.services.user_provision.billing_repo.get_by_owner_id", AsyncMock(return_value=trial)),
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.user_provision.get_admin_session_cookie", AsyncMock(return_value="admin-cookie")),
        patch("core.services.user_provision.billing_repo.try_claim_nightly_arm", AsyncMock(return_value=True)),
        patch("core.services.user_provision.create_enabled_schedule_routine", seed),
    ):
        await arm_nightly_ceo("org_p")
    seed.assert_awaited_once()
    assert seed.await_args.kwargs["reconcile_paused"] is True


@pytest.mark.asyncio
async def test_arm_expired_trial_not_armed():
    """An EXPIRED trial_local (trial_end in the past) is NOT armed — returns
    before any Paperclip call."""
    build = AsyncMock()
    expired = {"owner_id": "org_p", "subscription_status": "trial_local", "trial_end": int(time.time()) - 86400}
    with (
        patch("core.services.user_provision.billing_repo.get_by_owner_id", AsyncMock(return_value=expired)),
        patch("core.services.user_provision.build_paperclip_provisioning", build),
    ):
        await arm_nightly_ceo("org_p")
    build.assert_not_awaited()
    assert should_arm_nightly_ceo(expired) is False


@pytest.mark.asyncio
async def test_arm_no_account_never_armed():
    """Fail-closed: no billing row → no Paperclip call."""
    build = AsyncMock()
    with (
        patch("core.services.user_provision.billing_repo.get_by_owner_id", AsyncMock(return_value=None)),
        patch("core.services.user_provision.build_paperclip_provisioning", build),
    ):
        await arm_nightly_ceo("org_p")
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
        await arm_nightly_ceo("org_p")
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
        await arm_nightly_ceo("org_p")
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
        await arm_nightly_ceo("org_p")
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
        await arm_nightly_ceo("org_p")
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
            arm_nightly_ceo("org_p"),
            arm_nightly_ceo("org_p"),
        )
    assert seed.await_count == 1


@pytest.mark.asyncio
async def test_arm_seed_failure_releases_claim_and_raises():
    """A seed that raises → release_nightly_arm called and the error propagates (the
    caller decides: the executor classifies it, reconcile_runtime logs it)."""
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
        with pytest.raises(RuntimeError):
            await arm_nightly_ceo("org_p")
    release.assert_awaited_once_with("org_p", "co1")


@pytest.mark.asyncio
async def test_arm_org_admin_uses_authoritative_owner_resolver():
    """Org context resolves the company via the authoritative owner row."""
    provisioning = _arm_provisioning(_company_row(org_id="org_xyz", company_id="co_org"))
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
        await arm_nightly_ceo("org_xyz")
    provisioning._repo.resolve_owner_row.assert_awaited_once_with("org_xyz")
    provisioning.find_org_owner.assert_not_awaited()
    seed.assert_awaited_once()
    assert seed.await_args.kwargs["company_id"] == "co_org"


# --- arm_nightly_ceo: the auth-free door the provision executor calls --------


def _arm_patches(provisioning, **overrides):
    """The full collaborator set for a paid workspace owner, as (mocks, patchers)."""
    mocks = {
        "billing_repo.get_by_owner_id": AsyncMock(return_value=_paid_account()),
        "build_paperclip_provisioning": AsyncMock(return_value=provisioning),
        "close_paperclip_provisioning": AsyncMock(),
        "get_admin_session_cookie": AsyncMock(return_value="admin-cookie"),
        "billing_repo.try_claim_nightly_arm": AsyncMock(return_value=True),
        "billing_repo.release_nightly_arm": AsyncMock(),
        "create_enabled_schedule_routine": AsyncMock(return_value="rid-nightly"),
        **overrides,
    }
    return mocks, [patch(f"core.services.user_provision.{k}", v) for k, v in mocks.items()]


@pytest.mark.asyncio
async def test_arm_nightly_ceo_returns_armed_and_seeds_for_workspace_owner():
    provisioning = _arm_provisioning(_company_row())
    mocks, patches = _arm_patches(provisioning)
    with patch("core.services.user_provision.put_metric") as metric:
        for p in patches:
            p.start()
        try:
            assert await arm_nightly_ceo("org_p") == "armed"
        finally:
            for p in patches:
                p.stop()
    provisioning._repo.resolve_owner_row.assert_awaited_once_with("org_p")
    provisioning.get_company_row.assert_not_awaited()
    provisioning.find_org_owner.assert_not_awaited()
    assert mocks["create_enabled_schedule_routine"].await_args.kwargs["agent_id"] == "friday1"
    metric.assert_called_once_with("nightly_ceo.arm", dimensions={"status": "armed"})


@pytest.mark.asyncio
async def test_arm_nightly_ceo_expired_trial_is_not_entitled():
    """An EXPIRED trial_local is not armed → no Paperclip client is even built.
    (An unexpired trial IS armed — see test_arm_active_trial_now_seeds.)"""
    build = AsyncMock()
    with (
        patch(
            "core.services.user_provision.billing_repo.get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "org_p",
                    "subscription_status": "trial_local",
                    "trial_end": int(time.time()) - 86400,
                }
            ),
        ),
        patch("core.services.user_provision.build_paperclip_provisioning", build),
        patch("core.services.user_provision.put_metric") as metric,
    ):
        assert await arm_nightly_ceo("org_p") == "not_entitled"
    build.assert_not_awaited()
    metric.assert_called_once_with("nightly_ceo.arm", dimensions={"status": "not_entitled"})


@pytest.mark.asyncio
async def test_arm_nightly_ceo_org_owner_uses_authoritative_owner_resolver():
    """org_ prefix resolves the authoritative row, never a member row."""
    provisioning = _arm_provisioning(_company_row(org_id="org_xyz", company_id="co_org"))
    org_account = AsyncMock(return_value={"owner_id": "org_xyz", "subscription_status": "active"})
    _, patches = _arm_patches(provisioning, **{"billing_repo.get_by_owner_id": org_account})
    for p in patches:
        p.start()
    try:
        assert await arm_nightly_ceo("org_xyz") == "armed"
    finally:
        for p in patches:
            p.stop()
    provisioning._repo.resolve_owner_row.assert_awaited_once_with("org_xyz")
    provisioning.find_org_owner.assert_not_awaited()
    provisioning.get_company_row.assert_not_awaited()


@pytest.mark.asyncio
async def test_arm_nightly_ceo_org_prefers_newer_authoritative_starter_owner():
    """An older active member row must not hide the owner's starter CEO."""
    older_member = _company_row(org_id="org_xyz", company_id="co_org", friday="legacy-member-friday")
    starter_owner = _company_row(
        org_id="org_xyz",
        company_id="co_org",
        friday="legacy-owner-friday",
        starter_ids={"ceo": "starter-owner-ceo"},
    )
    provisioning = _arm_provisioning(starter_owner)
    provisioning.find_org_owner.return_value = older_member
    org_account = AsyncMock(return_value={"owner_id": "org_xyz", "subscription_status": "active"})
    mocks, patches = _arm_patches(provisioning, **{"billing_repo.get_by_owner_id": org_account})
    for p in patches:
        p.start()
    try:
        assert await arm_nightly_ceo("org_xyz") == "armed"
    finally:
        for p in patches:
            p.stop()

    provisioning._repo.resolve_owner_row.assert_awaited_once_with("org_xyz")
    provisioning.find_org_owner.assert_not_awaited()
    assert mocks["create_enabled_schedule_routine"].await_args.kwargs["agent_id"] == "starter-owner-ceo"


@pytest.mark.asyncio
async def test_arm_nightly_ceo_executor_ignores_claim_cache():
    """trust_cache=False with the cache already naming this company: no
    re-claim, straight to the title-idempotent create → "armed". This is the
    crash window between the claim write and the create."""
    provisioning = _arm_provisioning(_company_row())
    cached = AsyncMock(return_value=_paid_account(nightly_ceo_armed_company="co1"))
    mocks, patches = _arm_patches(provisioning, **{"billing_repo.get_by_owner_id": cached})
    for p in patches:
        p.start()
    try:
        assert await arm_nightly_ceo("org_p", trust_cache=False) == "armed"
    finally:
        for p in patches:
            p.stop()
    mocks["billing_repo.try_claim_nightly_arm"].assert_not_awaited()
    mocks["create_enabled_schedule_routine"].assert_awaited_once()


@pytest.mark.asyncio
async def test_arm_nightly_ceo_trusts_claim_cache_by_default():
    """Same cache, default trust_cache=True → "already_armed", no claim, no
    Paperclip call."""
    provisioning = _arm_provisioning(_company_row())
    cached = AsyncMock(return_value=_paid_account(nightly_ceo_armed_company="co1"))
    mocks, patches = _arm_patches(provisioning, **{"billing_repo.get_by_owner_id": cached})
    for p in patches:
        p.start()
    try:
        assert await arm_nightly_ceo("org_p") == "already_armed"
    finally:
        for p in patches:
            p.stop()
    mocks["billing_repo.try_claim_nightly_arm"].assert_not_awaited()
    mocks["get_admin_session_cookie"].assert_not_awaited()
    mocks["create_enabled_schedule_routine"].assert_not_awaited()


@pytest.mark.asyncio
async def test_arm_nightly_ceo_seed_failure_releases_claim_and_raises():
    """After winning the claim, a seed failure releases it, meters status=error,
    and PROPAGATES — the executor classifies it, not this function."""
    provisioning = _arm_provisioning(_company_row())
    mocks, patches = _arm_patches(
        provisioning,
        create_enabled_schedule_routine=AsyncMock(side_effect=RuntimeError("paperclip down")),
    )
    with patch("core.services.user_provision.put_metric") as metric:
        for p in patches:
            p.start()
        try:
            with pytest.raises(RuntimeError):
                await arm_nightly_ceo("org_p")
        finally:
            for p in patches:
                p.stop()
    mocks["billing_repo.release_nightly_arm"].assert_awaited_once_with("org_p", "co1")
    metric.assert_called_once_with("nightly_ceo.arm", dimensions={"status": "error"})


# ---------------------------------------------------------------------------
# Owner seed placement. The OWNER company-create normally runs in the
# executor's runner (scripts.provision_runner step 0) — that is the #1442 fix.
# The ONE case that stays here is a workspace that is already running, so no
# provision will run at all. ensure_user_ready never dispatches a provision.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, {"status": "error"}, {"status": "stopped"}, {"status": "provisioning"}])
async def test_ensure_user_ready_never_dispatches_and_leaves_a_non_running_seed_to_the_executor(
    _stub_trial_default, _no_container_row, row
):
    """The Teams/proxy doors are a lazy JOIN, never a provision: whatever the row says,
    nothing is dispatched, the member JOIN runs, and a workspace that is not running is
    the executor's to seed (a retryable error waits for the user's Retry)."""
    _no_container_row.return_value = row
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision.dispatch_provision", new=AsyncMock()) as dispatch,
        patch("core.services.user_provision._ensure_paperclip_for_org", new=AsyncMock()) as inline_org,
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    dispatch.assert_not_awaited()
    inline_org.assert_not_awaited()
    user_provision._ensure_paperclip_for_member.assert_awaited_once_with(
        org_id="org_xyz", member_user_id="user_admin", member_email="a@example.com"
    )


@pytest.mark.asyncio
async def test_gate_up_org_member_branch_is_unchanged():
    """The gate-up member branch (#1435) is orthogonal to where the owner seed
    runs: with the gate UP, an org member still gets the idempotent
    membership-add and no container is dispatched."""
    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=_up_gate())),
        patch("core.services.user_provision._ensure_paperclip_for_member", new=AsyncMock()) as member,
        patch("core.services.user_provision.dispatch_provision", new=AsyncMock()) as container,
    ):
        await ensure_user_ready(_org_member_auth("user_member", "org_456", "m@example.com"))
    member.assert_awaited_once_with(
        org_id="org_456",
        member_user_id="user_member",
        member_email="m@example.com",
    )
    container.assert_not_awaited()


# ---------------------------------------------------------------------------
# Self-heal on a box that is already up: the two regressions this design
# exists to prevent. Both used to be served by an INLINE seed reached through
# ensure_user_ready(provision_container=False); with the seed in the executor
# and the executor refusing a running row, "dispatch and hope" seeds nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_container_workspace_owner_seed_is_reconciled(_stub_trial_default, _container_running):
    """A running workspace with no active company seeds on the poll thread;
    the executor cannot help because running rows are unclaimable."""
    inline_owner = AsyncMock()
    captured: list[str] = []

    def capture_task(coro):
        captured.append(coro.__qualname__)
        coro.close()

    provisioning = MagicMock(find_org_owner=AsyncMock(return_value=None))
    with (
        patch.object(user_provision, "build_paperclip_provisioning", AsyncMock(return_value=provisioning)),
        patch.object(user_provision, "close_paperclip_provisioning", AsyncMock()),
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision._ensure_paperclip_for_org", new=inline_owner),
        patch("core.services.user_provision.asyncio.create_task", side_effect=capture_task),
    ):
        await ensure_user_ready(_org_admin_auth("user_p", "org_p", "p@example.com"))
    inline_owner.assert_awaited_once_with(
        org_id="org_p",
        user_id="user_p",
        email="p@example.com",
        is_admin=True,
    )
    assert captured == [], f"a running container must not be re-provisioned, got {captured}"


@pytest.mark.asyncio
async def test_running_container_seed_failure_is_swallowed_and_metered(_stub_trial_default, _container_running):
    """The reconcile is best-effort like every other Paperclip step here: a
    failure records paperclip.seed.error and never escapes the poll."""
    provisioning = MagicMock(find_org_owner=AsyncMock(return_value=None))
    with (
        patch.object(user_provision, "build_paperclip_provisioning", AsyncMock(return_value=provisioning)),
        patch.object(user_provision, "close_paperclip_provisioning", AsyncMock()),
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision._ensure_paperclip_for_org",
            new=AsyncMock(side_effect=RuntimeError("paperclip 502")),
        ),
        patch("core.services.user_provision.put_metric") as mock_metric,
        patch("core.services.user_provision.asyncio.create_task", side_effect=lambda c: c.close()),
    ):
        await ensure_user_ready(_org_admin_auth("user_p", "org_p", "p@example.com"))
    mock_metric.assert_any_call("paperclip.seed.error")


@pytest.mark.asyncio
async def test_arm_skips_when_row_belongs_to_another_owner():
    """A company row that belongs to a different workspace is never armed
    under this workspace's billing account."""
    provisioning = _arm_provisioning(_company_row(org_id="org_home"))
    claim = AsyncMock(return_value=True)
    seed = AsyncMock()
    with (
        patch("core.services.user_provision.billing_repo.get_by_owner_id", AsyncMock(return_value=_paid_account())),
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.user_provision.billing_repo.try_claim_nightly_arm", claim),
        patch("core.services.user_provision.create_enabled_schedule_routine", seed),
    ):
        assert await arm_nightly_ceo("org_p", trust_cache=False) == "company_not_active"
    claim.assert_not_awaited()
    seed.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_trial_default_offers_an_armed_trial_row_to_the_30_day_heal():
    """The 2026-09 prod cohort expired under the 3-day window; every trial-default door
    offers the row to the 30-day heal."""
    row = {"subscription_status": "trial_local", "trial_end": 123, "created_at": "2026-09-12T01:26:00+00:00"}
    with (
        patch.object(user_provision.billing_repo, "get_by_owner_id", AsyncMock(return_value=row)),
        patch.object(user_provision.billing_repo, "extend_short_trial", AsyncMock()) as extend,
    ):
        await user_provision.ensure_workspace_trial_default(_org_admin_auth(), "org_456")
    extend.assert_awaited_once_with("org_456", row)

    with (
        patch.object(
            user_provision.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})
        ),
        patch.object(user_provision.billing_repo, "extend_short_trial", AsyncMock()) as extend,
    ):
        await user_provision.ensure_workspace_trial_default(_org_admin_auth(), "org_456")
    extend.assert_not_awaited()


# ----------------------------------------------------------------------
# provision_new_workspace (Clerk organization.created)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_new_workspace_creates_the_trial_row_and_dispatches(monkeypatch):
    trial = AsyncMock()
    gate = AsyncMock(return_value=None)
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(user_provision, "_ensure_trial_row", trial)
    monkeypatch.setattr(user_provision, "evaluate_provision_gate", gate)
    monkeypatch.setattr(user_provision, "dispatch_provision", dispatch)
    await user_provision.provision_new_workspace("org_new")
    trial.assert_awaited_once_with("org_new")
    gate.assert_awaited_once_with(owner_id="org_new", is_admin=True)
    dispatch.assert_awaited_once_with("org_new")


@pytest.mark.asyncio
async def test_provision_new_workspace_stops_at_a_gate(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(user_provision, "_ensure_trial_row", AsyncMock())
    monkeypatch.setattr(user_provision, "evaluate_provision_gate", AsyncMock(return_value=_up_gate()))
    monkeypatch.setattr(user_provision, "dispatch_provision", dispatch)
    await user_provision.provision_new_workspace("org_new")
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trial", "dispatched"),
    [(AsyncMock(side_effect=RuntimeError("ddb")), None), (AsyncMock(), False)],
)
async def test_provision_new_workspace_raises_so_svix_redelivers(monkeypatch, trial, dispatched):
    """The webhook is the ONLY thing that starts a first provision now (no status poll re-drives
    it), so a trial-row failure or a dispatch that did not start RAISES -- and is metered."""
    metrics = []
    monkeypatch.setattr(user_provision, "_ensure_trial_row", trial)
    monkeypatch.setattr(user_provision, "evaluate_provision_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(user_provision, "dispatch_provision", AsyncMock(return_value=dispatched))
    monkeypatch.setattr(user_provision, "put_metric", lambda name, **kw: metrics.append(name))
    with pytest.raises(RuntimeError):
        await user_provision.provision_new_workspace("org_new")
    assert metrics == ["provision.org_created.error"]


@pytest.mark.asyncio
async def test_provision_new_workspace_ignores_a_non_org_id(monkeypatch):
    trial = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(user_provision, "_ensure_trial_row", trial)
    monkeypatch.setattr(user_provision, "dispatch_provision", dispatch)
    await user_provision.provision_new_workspace("user_alone")
    trial.assert_not_awaited()
    dispatch.assert_not_awaited()
