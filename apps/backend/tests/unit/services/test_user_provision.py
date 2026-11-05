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
    fake_owner_row = MagicMock(status="active", company_id="co_1", provisioner_user_id="user_owner")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_owner_row)
    fake_provisioning.provision_member = AsyncMock()

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
        await ensure_user_ready(_org_member_auth("user_m", "org_o", "m@example.com"))
    fake_provisioning.provision_member.assert_awaited_once_with(
        owner_id="org_o",
        user_id="user_m",
        email="m@example.com",
    )


@pytest.mark.asyncio
async def test_gate_up_org_admin_join_only_never_creates_company():
    """Gate up + org ADMIN + no company yet → NOTHING is created. The gate-up
    branch is join-only (_ensure_paperclip_for_member returns gracefully when
    no active company exists); company CREATE stays behind the gate."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.provision_org = AsyncMock()
    fake_provisioning.provision_member = AsyncMock()
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
    fake_provisioning.provision_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_up_membership_add_409_is_idempotent_no_op():
    """Repeat polls while the gate is up: provision_member's 409 (already
    added / concurrent join) is swallowed — the gate-up membership-add is as
    idempotent as the normal-path one."""
    from core.services.paperclip_admin_client import PaperclipApiError

    fake_owner_row = MagicMock(status="active", company_id="co_1", provisioner_user_id="user_owner")
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

    fake_owner_row = MagicMock(status="active", company_id="co_1", provisioner_user_id="user_owner")
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
    fake_provisioning.get_workspace = AsyncMock(return_value=None)
    fake_provisioning.provision_org = AsyncMock(return_value=MagicMock(status="active", starter_agent_ids=starter_ids))
    ensure_default_room = AsyncMock(return_value="room_general")
    ensure_feed_room = AsyncMock(return_value="room_feed")
    owner_welcome = AsyncMock(return_value="sent")
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
        patch("core.services.messaging.ensure_feed_room", ensure_feed_room),
        patch("core.services.welcome_email.send_owner_welcome", owner_welcome),
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
    # Task 8a: the workspace feed is made beside #general.
    ensure_feed_room.assert_awaited_once_with("org_p", "user_p")
    # T14: the CEO's welcome goes out once #general exists.
    owner_welcome.assert_awaited_once_with("org_p", "user_p")


@pytest.mark.asyncio
async def test_existing_active_starter_workspace_does_not_gain_default_room():
    """A healthy pre-existing workspace is not fresh onboarding evidence."""
    starter_ids = {
        key: f"agent-{key}" for key in ("ceo", "engineering", "finance", "growth", "analytics", "customer_operations")
    }
    existing = MagicMock(status="active", org_id="org_p", starter_agent_ids=starter_ids)
    fake_provisioning = MagicMock()
    fake_provisioning.get_workspace = AsyncMock(return_value=existing)
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
async def test_fresh_workspace_default_room_seeded_once_across_polls():
    """The freshness read is the org's own workspace row: absent on the first
    poll (room seeded), active + complete on the next (no second seed)."""
    starter_ids = {
        key: f"agent-{key}" for key in ("ceo", "engineering", "finance", "growth", "analytics", "customer_operations")
    }
    new_org = MagicMock(status="active", org_id="org_new", starter_agent_ids=starter_ids)
    fake_provisioning = MagicMock()
    fake_provisioning.get_workspace = AsyncMock(side_effect=[None, new_org])
    fake_provisioning.provision_org = AsyncMock(return_value=new_org)
    ensure_default_room = AsyncMock()
    with (
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=fake_provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.messaging.ensure_default_room", ensure_default_room),
        patch("core.services.messaging.ensure_feed_room", AsyncMock()),
    ):
        for _ in range(2):
            await _ensure_paperclip_for_owner(
                owner_id="org_new",
                owner_user_id="user_p",
                owner_email="p@example.com",
            )

    assert [c.args for c in fake_provisioning.get_workspace.await_args_list] == [("org_new",), ("org_new",)]
    ensure_default_room.assert_awaited_once_with("org_new", "user_p")


@pytest.mark.asyncio
async def test_partial_starter_workspace_gets_default_room_after_completion():
    """A partial starter retry becomes eligible only after its map completes."""
    complete = {
        key: f"agent-{key}" for key in ("ceo", "engineering", "finance", "growth", "analytics", "customer_operations")
    }
    partial = MagicMock(status="failed", starter_agent_ids={**complete, "analytics": None})
    fake_provisioning = MagicMock()
    fake_provisioning.get_workspace = AsyncMock(return_value=partial)
    fake_provisioning.provision_org = AsyncMock(return_value=MagicMock(status="active", starter_agent_ids=complete))
    ensure_default_room = AsyncMock()
    with (
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=fake_provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.messaging.ensure_default_room", ensure_default_room),
        patch("core.services.messaging.ensure_feed_room", AsyncMock()),
    ):
        await _ensure_paperclip_for_owner(
            owner_id="org_p",
            owner_user_id="user_p",
            owner_email="p@example.com",
        )

    ensure_default_room.assert_awaited_once_with("org_p", "user_p")


@pytest.mark.asyncio
async def test_retry_after_a_failed_seed_with_no_starters_gets_default_room():
    """A first seed that failed before any starter existed leaves status "failed" and no starter map
    at all; the retry that completes it is still the first provision, so #general is seeded."""
    complete = {
        key: f"agent-{key}" for key in ("ceo", "engineering", "finance", "growth", "analytics", "customer_operations")
    }
    failed = MagicMock(status="failed", starter_agent_ids=None)
    fake_provisioning = MagicMock()
    fake_provisioning.get_workspace = AsyncMock(return_value=failed)
    fake_provisioning.provision_org = AsyncMock(return_value=MagicMock(status="active", starter_agent_ids=complete))
    ensure_default_room = AsyncMock()
    ensure_feed_room = AsyncMock()
    with (
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=fake_provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.messaging.ensure_default_room", ensure_default_room),
        patch("core.services.messaging.ensure_feed_room", ensure_feed_room),
    ):
        await _ensure_paperclip_for_owner(owner_id="org_p", owner_user_id="user_p", owner_email="p@example.com")

    ensure_default_room.assert_awaited_once_with("org_p", "user_p")
    ensure_feed_room.assert_awaited_once_with("org_p", "user_p")


@pytest.mark.asyncio
async def test_org_admin_with_no_company_creates_it():
    """Org has NO active Paperclip company yet → provision_org (create).
    Decided by company existence, not role."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.get_workspace = AsyncMock(return_value=None)
    fake_provisioning.provision_org = AsyncMock()
    fake_provisioning.provision_member = AsyncMock()

    with (
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
    ):
        await _ensure_paperclip_for_org(org_id="org_xyz", user_id="user_admin", email="a@example.com", is_admin=True)
    fake_provisioning.provision_org.assert_awaited_once_with(
        owner_id="org_xyz",
        owner_user_id="user_admin",
        owner_email="a@example.com",
    )
    fake_provisioning.provision_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_org_admin_create_failure_propagates():
    """A provision_org failure on the create branch propagates to the caller
    (the executor classifies it into its retry ABI). There is no claim to
    release: a retry just re-runs provision_org, whose company create replays
    under the per-org Idempotency-Key."""
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=None)
    fake_provisioning.get_workspace = AsyncMock(return_value=None)
    fake_provisioning.provision_org = AsyncMock(side_effect=RuntimeError("paperclip down"))

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


@pytest.mark.asyncio
async def test_org_admin_with_existing_company_joins_it():
    """REGRESSION: org admin + org ALREADY has an active company →
    provision_member (join), NOT provision_org. The multi-admin case that
    previously 409'd on /teams."""
    fake_owner_row = MagicMock(status="active", company_id="co_1", provisioner_user_id="user_first")
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
    ):
        await ensure_user_ready(_org_admin_auth("user_second_admin", "org_xyz", "b@example.com"))
    fake_provisioning.provision_member.assert_awaited_once_with(
        owner_id="org_xyz",
        user_id="user_second_admin",
        email="b@example.com",
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

    with (
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch(
            "core.services.user_provision.build_paperclip_provisioning", new=AsyncMock(return_value=fake_provisioning)
        ),
        patch("core.services.user_provision.close_paperclip_provisioning", new=AsyncMock()),
    ):
        await ensure_user_ready(_org_member_auth())
    fake_provisioning.provision_org.assert_not_awaited()
    fake_provisioning.provision_member.assert_not_awaited()


# ---------------------------------------------------------------------------
# Org-member fast path
# ---------------------------------------------------------------------------
#
# ``_ensure_paperclip_for_member`` reuses
# ``PaperclipProvisioning.find_org_owner`` (the org's workspace row, only
# when ``status="active"``). The "company missing" and "company not yet
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
    fake_owner_row = MagicMock(status="active", company_id="co_123", provisioner_user_id="user_owner_123")
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
    )


@pytest.mark.asyncio
async def test_ensure_paperclip_for_member_never_looks_up_the_owner_email():
    """The bootstrap admin invites + approves a joining member, so the owner's
    email (and sign-in) is no longer needed: no owner-email lookup, and
    provision_member gets no ``owner_email``."""
    fake_company = MagicMock(status="active", company_id="co_123", provisioner_user_id="user_owner_123")
    fake_provisioning = MagicMock()
    fake_provisioning.find_org_owner = AsyncMock(return_value=fake_company)
    fake_provisioning.provision_member = AsyncMock()
    lookup = AsyncMock(return_value="owner@example.com")

    with (
        patch(
            "core.services.user_provision.evaluate_provision_gate",
            new=AsyncMock(return_value=None),
        ),
        patch("core.services.user_provision.lookup_owner_email", new=lookup),
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

    assert not [c for c in lookup.await_args_list if c.kwargs.get("org_id") == "org_o"]
    assert "owner_email" not in fake_provisioning.provision_member.await_args.kwargs


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

    fake_company = MagicMock(status="active", company_id="co_x", provisioner_user_id="user_owner")
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

    fake_company = MagicMock(status="active", company_id="co_x", provisioner_user_id="user_owner")
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
    fake_provisioning.get_workspace = AsyncMock(return_value=None)
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
    fake_provisioning.get_workspace = AsyncMock(return_value=None)
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
    fake_provisioning.get_workspace = AsyncMock(return_value=None)
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

    fake_company = MagicMock(status="active", company_id="co_x", provisioner_user_id="user_owner")
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
    fake_company = MagicMock(status="active", company_id="co_x", provisioner_user_id="user_owner")
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
    ):
        await ensure_user_ready(_org_member_auth())
    fake_provisioning.provision_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_running_container_skips_container_dispatch_for_admin(_container_running):
    """RUNNING-container poll path: the org admin's poll must run paperclip
    provisioning but NOT dispatch the executor — the old caller-passed
    provision_container=False flag is gone; the decision comes from the row."""
    fake_owner_row = MagicMock(status="active", company_id="co_1", provisioner_user_id="user_first")
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
    ):
        await ensure_user_ready(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
    fake_provisioning.provision_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_running_container_org_admin_with_company_does_not_recreate(_container_running):
    """The seed reconcile on a running org container is CREATE-ONLY: the
    role-blind JOIN already ran on this thread, so an org whose company exists
    must not re-issue the member-add (a Paperclip round trip + Clerk email
    lookup on every ~3s poll of a healthy org)."""
    fake_owner_row = MagicMock(status="active", company_id="co_1", provisioner_user_id="user_first")
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
    # The PERSON is the human who hit the door; the workspace rides as a property.
    assert event["distinct_id"] == auth.user_id
    assert event["event"] == "trial_started"
    assert event["properties"] == {"owner_type": "org", "workspace_id": "org_456"}


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
        assert capture.call_args.kwargs["properties"] == {"workspace_id": "org_456", "error_code": "RuntimeError"}
        assert all(c.kwargs["distinct_id"] == _org_admin_auth().user_id for c in capture.call_args_list)
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
    assert first["properties"] == {"owner_type": "org", "workspace_id": "org_456"}
    # Two humans, one workspace: different persons, one deterministic uuid, so
    # PostHog keeps the earlier capture and the trial counts once.
    assert first["distinct_id"] == _org_admin_auth().user_id
    assert second["distinct_id"] == _org_member_auth().user_id
    assert first["event_uuid"] == second["event_uuid"]


@pytest.mark.asyncio
async def test_org_created_webhook_keys_the_trial_on_the_creator(
    _fresh_workspace_signup, _mock_create_trial_local_if_absent
):
    """The organization.created path has no AuthContext; it carries Clerk's
    ``created_by`` so the trial still lands on the creator's timeline."""
    _mock_create_trial_local_if_absent.return_value = True
    with (
        patch.object(user_provision, "capture_product_event") as capture,
        patch.object(user_provision, "evaluate_provision_gate", AsyncMock(return_value=None)),
        patch.object(user_provision, "dispatch_provision", AsyncMock(return_value=True)),
    ):
        await user_provision.provision_new_workspace("org_456", created_by="user_creator")
    event = capture.call_args.kwargs
    assert event["event"] == "trial_started"
    assert event["distinct_id"] == "user_creator"
    assert event["properties"]["workspace_id"] == "org_456"


# ---------------------------------------------------------------------------
# CEO timer-heartbeat arming (replaced the "Nightly CEO progress" routine)
# ---------------------------------------------------------------------------
#
# Gated on should_arm_nightly_ceo (paid OR an unexpired trial) and the durable
# opt-out; the interval follows billing on every call. These call the REAL
# arm_nightly_ceo and patch its collaborators on the user_provision module.

_SPECIALISTS = {"engineering": "eng1", "finance": "fin1"}


def _arm_provisioning(row, extra_agents=()):
    """A fake PaperclipProvisioning whose authoritative owner resolver returns
    ``row`` (``find_org_owner`` too, so a test can prove the arm never reads it)."""
    fake = MagicMock()
    fake.find_org_owner = AsyncMock(return_value=row)
    fake._repo = MagicMock()
    fake._repo.resolve_owner_row = AsyncMock(return_value=row)
    fake._admin = MagicMock()  # ._http is just an attribute access
    fake._admin.set_agent_heartbeat = AsyncMock(return_value={})
    ids = row and (list((row.starter_agent_ids or {}).values()) or row.seeded_agent_ids) or []
    fake._admin.list_agents = AsyncMock(
        return_value={"agents": [{"id": i} for i in dict.fromkeys(ids) if i] + list(extra_agents)}
    )
    fake._admin.list_routines = AsyncMock(return_value=[])
    fake._admin.create_routine = AsyncMock(side_effect=lambda **kw: {"id": f"r-{kw['idempotency_key'][-2:]}"})
    fake._admin.create_routine_trigger = AsyncMock(return_value={})
    # The trigger holder re-reads the routine first (no trigger yet, unless a test says so).
    fake._admin.get_routine = AsyncMock(side_effect=lambda **kw: {"id": kw["routine_id"], "triggers": []})
    # The shared pause/resume seam (paperclip_agent_lifecycle), patched in by _arm.
    fake.pause_automation = AsyncMock(return_value={"agent_ids": [], "routine_ids": []})
    fake.resume_automation = AsyncMock(return_value={"heartbeats_resumed": 0, "routines_resumed": 0})
    fake.disable_timers = AsyncMock(return_value={"agents": [], "routines": []})
    return fake


def _paid_account(**extra):
    return {"owner_id": "org_p", "subscription_status": "active", **extra}


def _company_row(
    company_id="co1", status="active", friday="friday1", starter_ids=None, org_id="org_p", provisioner_user_id=""
):
    """``org_id`` is the workspace the row belongs to."""
    return MagicMock(
        org_id=org_id,
        provisioner_user_id=provisioner_user_id,
        company_id=company_id,
        status=status,
        seeded_agent_ids=[friday] if friday else None,
        starter_agent_ids=starter_ids,
    )


def _heartbeats(provisioning):
    """agent id -> heartbeat, for every set_agent_heartbeat call."""
    return {
        c.kwargs["agent_id"]: c.kwargs["heartbeat"] for c in provisioning._admin.set_agent_heartbeat.await_args_list
    }


async def _arm(provisioning, account, owner_id="org_p"):
    with (
        patch("core.services.user_provision.billing_repo.get_by_owner_id", AsyncMock(return_value=account)),
        patch("core.services.user_provision.build_paperclip_provisioning", AsyncMock(return_value=provisioning)),
        patch("core.services.user_provision.close_paperclip_provisioning", AsyncMock()),
        patch("core.services.user_provision.get_admin_session_cookie", AsyncMock(return_value="admin-cookie")),
        patch("core.services.user_provision.pause_owner_automation", provisioning.pause_automation),
        patch("core.services.user_provision.resume_owner_automation", provisioning.resume_automation),
        patch("core.services.user_provision.disable_timer_automation", provisioning.disable_timers),
        patch("core.services.user_provision.container_repo.try_claim_pass", AsyncMock(return_value=True)),
        patch("core.services.user_provision.container_repo.release_pass", AsyncMock()),
    ):
        return await arm_nightly_ceo(owner_id)


def _ceo_heartbeat(interval):
    return {"enabled": True, "intervalSec": interval, "wakeOnDemand": True, "skipTimerWhenNoActionableWork": False}


_LIVE_TRIAL_END = int(time.time()) + 86400


@pytest.mark.parametrize(
    ("account", "interval"),
    [
        ({"subscription_status": "active", "plan": "base"}, 28800),
        ({"subscription_status": "active", "provider_choice": "openrouter_managed"}, 28800),
        ({"subscription_status": "trialing", "plan": "standard"}, 28800),
        ({"subscription_status": "trial_local", "trial_end": _LIVE_TRIAL_END}, 43200),
        # trial_local resolves to the managed gate whatever it stores.
        ({"subscription_status": "trial_local", "provider_choice": "chatgpt_oauth"}, 43200),
        ({"subscription_status": "active", "provider_choice": "byo_key", "byo_provider": "anthropic"}, 86400),
        ({"subscription_status": "active", "provider_choice": "chatgpt_oauth"}, 86400),
    ],
)
def test_ceo_heartbeat_interval_follows_billing(account, interval):
    assert user_provision.ceo_heartbeat_interval_sec(account) == interval


@pytest.mark.asyncio
async def test_arm_paid_sets_ceo_and_specialist_heartbeats():
    from core.services.paperclip_provisioning import SPECIALIST_HEARTBEAT

    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "starter-ceo", **_SPECIALISTS}))
    with patch("core.services.user_provision.put_metric") as metric:
        assert await _arm(provisioning, _paid_account()) == "armed"
    assert _heartbeats(provisioning) == {
        "starter-ceo": _ceo_heartbeat(28800),
        "eng1": SPECIALIST_HEARTBEAT,
        "fin1": SPECIALIST_HEARTBEAT,
    }
    provisioning._repo.resolve_owner_row.assert_awaited_once_with("org_p")
    provisioning.find_org_owner.assert_not_awaited()
    metric.assert_called_once_with("nightly_ceo.arm", dimensions={"status": "armed"})


@pytest.mark.asyncio
async def test_arm_legacy_roster_arms_the_first_seeded_agent():
    provisioning = _arm_provisioning(_company_row())
    assert await _arm(provisioning, _paid_account()) == "armed"
    assert _heartbeats(provisioning) == {"friday1": _ceo_heartbeat(28800)}


@pytest.mark.asyncio
async def test_arm_legacy_roster_arms_its_other_seeded_agents_as_specialists():
    from core.services.paperclip_provisioning import SPECIALIST_HEARTBEAT

    row = _company_row()
    row.seeded_agent_ids = ["friday1", "forge1", "scout1"]
    provisioning = _arm_provisioning(row)
    assert await _arm(provisioning, _paid_account()) == "armed"
    assert _heartbeats(provisioning) == {
        "friday1": _ceo_heartbeat(28800),
        "forge1": SPECIALIST_HEARTBEAT,
        "scout1": SPECIALIST_HEARTBEAT,
    }


@pytest.mark.asyncio
async def test_arm_arms_hired_agents_and_skips_enabled_and_terminated():
    from core.services.paperclip_provisioning import SPECIALIST_HEARTBEAT

    extra = [
        {"id": "hired1"},
        {"id": "own1", "runtimeConfig": {"heartbeat": {"enabled": True, "intervalSec": 60}}},
        {"id": "off1", "runtimeConfig": {"heartbeat": {"enabled": False}}},
        {"id": "dead1", "status": "terminated"},
    ]
    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "starter-ceo", **_SPECIALISTS}), extra)
    assert await _arm(provisioning, _paid_account()) == "armed"
    assert _heartbeats(provisioning) == {
        "starter-ceo": _ceo_heartbeat(28800),
        "eng1": SPECIALIST_HEARTBEAT,
        "fin1": SPECIALIST_HEARTBEAT,
        "hired1": SPECIALIST_HEARTBEAT,
        "off1": SPECIALIST_HEARTBEAT,
    }


async def test_arm_live_trial_wakes_every_12_hours():
    provisioning = _arm_provisioning(_company_row())
    trial = {"owner_id": "org_p", "subscription_status": "trial_local", "trial_end": _LIVE_TRIAL_END}
    assert await _arm(provisioning, trial) == "armed"
    assert _heartbeats(provisioning) == {"friday1": _ceo_heartbeat(43200)}


@pytest.mark.asyncio
async def test_rearm_follows_the_new_billing_no_cache():
    """A reconcile after a provider switch re-derives the interval: there is no
    "already armed" short-circuit."""
    provisioning = _arm_provisioning(_company_row())
    await _arm(provisioning, _paid_account())
    await _arm(provisioning, _paid_account(provider_choice="byo_key"))
    calls = provisioning._admin.set_agent_heartbeat.await_args_list
    assert [c.kwargs["heartbeat"]["intervalSec"] for c in calls] == [28800, 86400]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account",
    [
        None,
        {"owner_id": "org_p", "subscription_status": "trial_local", "trial_end": int(time.time()) - 86400},
        {"owner_id": "org_p", "subscription_status": "past_due"},
        {"owner_id": "org_p", "subscription_status": "canceled"},
    ],
)
async def test_arm_not_entitled_pauses_through_the_shared_seam(account):
    """Converge, not only enable: an expired trial / past_due / canceled / no-row owner
    stops waking -- routines AND heartbeats, through pause_owner_automation."""
    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "ceo1", **_SPECIALISTS}))
    with patch("core.services.user_provision.put_metric") as metric:
        assert await _arm(provisioning, account) == "not_entitled"
    provisioning.pause_automation.assert_awaited_once_with("org_p")
    provisioning.resume_automation.assert_not_awaited()
    provisioning._admin.set_agent_heartbeat.assert_not_awaited()
    metric.assert_called_once_with("nightly_ceo.arm", dimensions={"status": "not_entitled"})
    assert should_arm_nightly_ceo(account) is False


@pytest.mark.asyncio
async def test_arm_re_entitled_resumes_what_the_pause_took_then_arms():
    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "ceo1", **_SPECIALISTS}))
    assert await _arm(provisioning, _paid_account()) == "armed"
    provisioning.resume_automation.assert_awaited_once_with("org_p")
    provisioning.pause_automation.assert_not_awaited()


@pytest.mark.asyncio
async def test_arm_opt_out_after_arming_turns_the_ceo_off():
    """The owner's durable opt-out disables the CEO's timer; specialists are
    still re-armed (teardown disarms them too)."""
    from core.services.paperclip_provisioning import SPECIALIST_HEARTBEAT

    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "ceo1", **_SPECIALISTS}))
    assert await _arm(provisioning, _paid_account()) == "armed"
    provisioning._admin.set_agent_heartbeat.reset_mock()
    assert await _arm(provisioning, _paid_account(nightly_ceo_opted_out=True)) == "opted_out"
    assert _heartbeats(provisioning) == {
        "ceo1": {"enabled": False},
        "eng1": SPECIALIST_HEARTBEAT,
        "fin1": SPECIALIST_HEARTBEAT,
    }


@pytest.mark.asyncio
async def test_arm_is_a_noop_when_heartbeats_are_disabled():
    """Dev's kill switch: a reconcile must not re-arm timers an operator switched off."""
    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "ceo1", **_SPECIALISTS}))
    with patch("core.services.user_provision.settings.NIGHTLY_CEO_HEARTBEATS_ENABLED", False):
        assert await _arm(provisioning, _paid_account()) == "heartbeats_disabled"
    provisioning._admin.set_agent_heartbeat.assert_not_called()
    provisioning.disable_timers.assert_awaited_once_with("org_p")


@pytest.mark.asyncio
async def test_the_kill_switch_still_resumes_what_our_pause_took():
    """The switch governs arming starter timers, not restoring a re-subscribed owner's own."""
    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "ceo1", **_SPECIALISTS}))
    with patch("core.services.user_provision.settings.NIGHTLY_CEO_HEARTBEATS_ENABLED", False):
        assert await _arm(provisioning, _paid_account()) == "heartbeats_disabled"
    provisioning.resume_automation.assert_awaited_once_with("org_p")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "status"),
    [
        (_company_row(status="failed"), "company_not_active"),
        (_company_row(org_id="org_home"), "company_not_active"),
        (None, "company_not_active"),
        (_company_row(friday=None), "friday_not_seeded"),
    ],
)
async def test_arm_skips_without_an_armable_company(row, status):
    provisioning = _arm_provisioning(row)
    assert await _arm(provisioning, _paid_account()) == status
    provisioning._admin.set_agent_heartbeat.assert_not_awaited()


@pytest.mark.asyncio
async def test_arm_failure_meters_error_and_raises():
    """The executor classifies the failure, reconcile_runtime logs it."""
    provisioning = _arm_provisioning(_company_row())
    provisioning._admin.set_agent_heartbeat.side_effect = RuntimeError("paperclip down")
    with patch("core.services.user_provision.put_metric") as metric:
        with pytest.raises(RuntimeError):
            await _arm(provisioning, _paid_account())
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

    provisioning = MagicMock(find_org_owner=AsyncMock(return_value=None))
    with (
        patch.object(user_provision, "build_paperclip_provisioning", AsyncMock(return_value=provisioning)),
        patch.object(user_provision, "close_paperclip_provisioning", AsyncMock()),
        patch("core.services.user_provision.evaluate_provision_gate", new=AsyncMock(return_value=None)),
        patch("core.services.user_provision._ensure_paperclip_for_org", new=inline_owner),
    ):
        await ensure_user_ready(_org_admin_auth("user_p", "org_p", "p@example.com"))
    inline_owner.assert_awaited_once_with(
        org_id="org_p",
        user_id="user_p",
        email="p@example.com",
        is_admin=True,
    )


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
    ):
        await ensure_user_ready(_org_admin_auth("user_p", "org_p", "p@example.com"))
    mock_metric.assert_any_call("paperclip.seed.error")


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
    await user_provision.provision_new_workspace("org_new", created_by="user_creator")
    trial.assert_awaited_once_with("org_new", actor_user_id="user_creator")
    gate.assert_awaited_once_with(owner_id="org_new", is_admin=True, actor_user_id="user_creator")
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


# ---------------------------------------------------------------------------
# Feed schedule (task 8a): three CEO routines at 08:00, 13:00, 18:00, armed by the same path
# ---------------------------------------------------------------------------


def _feed_routines(provisioning):
    return [c.kwargs for c in provisioning._admin.create_routine.await_args_list]


def _feed_triggers(provisioning):
    return {
        c.kwargs["routine_id"]: c.kwargs["body"] for c in provisioning._admin.create_routine_trigger.await_args_list
    }


@pytest.mark.asyncio
async def test_arm_creates_the_three_feed_routines_and_the_planning_routine_for_the_ceo():
    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "ceo1", **_SPECIALISTS}))
    assert await _arm(provisioning, _paid_account()) == "armed"
    created = _feed_routines(provisioning)
    assert [c["idempotency_key"] for c in created] == [
        "lightbulb-feed-routine:v1:org_p:08",
        "lightbulb-feed-routine:v1:org_p:13",
        "lightbulb-feed-routine:v1:org_p:18",
        "lightbulb-planning-routine:v1:org_p:09",
    ]
    assert all(c["company_id"] == "co1" and c["body"]["assigneeAgentId"] == "ceo1" for c in created)
    assert [c["body"]["title"] for c in created] == [
        *(user_provision.feed_routine_title(h) for h in (8, 13, 18)),
        "Plan the company",
    ]
    feed, planning = created[:3], created[3]
    assert all("--feed" in c["body"]["description"] and "--why" in c["body"]["description"] for c in feed)
    assert planning["body"]["description"] == user_provision.PLANNING_ROUTINE_DESCRIPTION
    assert _feed_triggers(provisioning) == {
        f"r-{h}": {"kind": "schedule", "cronExpression": f"0 {int(h)} * * *", "timezone": "UTC", "enabled": True}
        for h in ("08", "13", "18", "09")
    }


@pytest.mark.asyncio
async def test_a_reconcile_does_not_duplicate_the_ceo_routines():
    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "ceo1", **_SPECIALISTS}))
    provisioning._admin.list_routines.return_value = [
        {
            "id": f"r{hour}",
            "title": title,
            "assigneeAgentId": "ceo1",
            "status": "active",
            "triggers": [{"kind": "schedule"}],
        }
        for _, title, _, hour in user_provision.CEO_ROUTINES
    ]
    assert await _arm(provisioning, _paid_account()) == "armed"
    provisioning._admin.create_routine.assert_not_awaited()
    provisioning._admin.create_routine_trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_feed_routine_left_without_its_trigger_gets_one():
    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "ceo1", **_SPECIALISTS}))
    provisioning._admin.list_routines.return_value = {
        "routines": [
            {"id": "r8", "title": user_provision.feed_routine_title(8), "assigneeAgentId": "ceo1", "triggers": []}
        ]
    }
    await _arm(provisioning, _paid_account())
    assert [c["idempotency_key"][-2:] for c in _feed_routines(provisioning)] == ["13", "18", "09"]
    assert set(_feed_triggers(provisioning)) == {"r8", "r-13", "r-18", "r-09"}


@pytest.mark.asyncio
async def test_a_replayed_feed_routine_the_owner_renamed_gets_no_second_trigger():
    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "ceo1"}))
    provisioning._admin.list_routines.return_value = [
        {"id": f"r-{h:02d}", "title": "Renamed", "assigneeAgentId": "ceo1", "triggers": [{"kind": "schedule"}]}
        for h in (8, 13, 18, 9)
    ]
    await _arm(provisioning, _paid_account())
    assert len(_feed_routines(provisioning)) == 4  # the key replays the same four
    provisioning._admin.create_routine_trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_feed_routine_the_owner_deleted_stays_deleted_without_errors():
    """Paperclip replays a create key only while its routine exists: a deleted one answers
    409 ..._idempotency_target_missing. A trigger on a routine gone since the list is a 404."""
    from core.services.paperclip_admin_client import PaperclipApiError

    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "ceo1"}))
    gone = PaperclipApiError(
        "POST -> 409", status_code=409, body='{"code":"routine_create_idempotency_target_missing"}'
    )
    provisioning._admin.create_routine.side_effect = [gone, {"id": "r-13"}, {"id": "r-18"}, {"id": "r-09"}]
    provisioning._admin.create_routine_trigger.side_effect = [
        PaperclipApiError("POST -> 404", status_code=404, body="{}"),
        {},
        {},
    ]
    with patch("core.services.user_provision.put_metric") as metric:
        assert await _arm(provisioning, _paid_account()) == "armed"
    assert [c.kwargs["routine_id"] for c in provisioning._admin.create_routine_trigger.await_args_list] == [
        "r-13",
        "r-18",
        "r-09",
    ]
    assert all(c.args[0] != "feed.routines" for c in metric.call_args_list)


@pytest.mark.asyncio
async def test_feed_routines_use_the_owners_timezone_when_known():
    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "ceo1"}, provisioner_user_id="user_p"))
    with patch("core.services.user_provision.user_repo.get", AsyncMock(return_value={"timezone": "Europe/Paris"})):
        await _arm(provisioning, _paid_account())
    assert {body["timezone"] for body in _feed_triggers(provisioning).values()} == {"Europe/Paris"}


@pytest.mark.asyncio
async def test_feed_routines_are_not_created_while_automation_is_paused():
    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "ceo1"}))
    assert await _arm(provisioning, {"owner_id": "org_p", "subscription_status": "canceled"}) == "not_entitled"
    provisioning._admin.list_routines.assert_not_awaited()
    provisioning._admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_feed_routine_failure_never_fails_the_arm():
    provisioning = _arm_provisioning(_company_row(starter_ids={"ceo": "ceo1"}))
    provisioning._admin.list_routines.side_effect = RuntimeError("paperclip down")
    assert await _arm(provisioning, _paid_account()) == "armed"


@pytest.mark.asyncio
async def test_overlapping_reconciles_attach_each_feed_trigger_once(monkeypatch):
    """Trigger creates carry no idempotency key upstream: two reconciles that both list a routine
    without its trigger must not both add one. A short per-owner+hour claim serializes the add,
    and the holder re-reads the routine first."""
    import asyncio

    triggers: dict[str, list] = {}
    routines = [
        {"id": f"r{hour}", "title": title, "assigneeAgentId": "ceo1", "status": "active"}
        for _, title, _, hour in user_provision.CEO_ROUTINES
    ]

    async def add_trigger(*, routine_id, body, **_):
        await asyncio.sleep(0)
        triggers.setdefault(routine_id, []).append(body)

    async def get_routine(*, routine_id, **_):
        await asyncio.sleep(0)
        return {"id": routine_id, "triggers": list(triggers.get(routine_id, []))}

    claims: set[str] = set()

    async def claim(name, *, interval_s):
        await asyncio.sleep(0)
        if name in claims:
            return False
        claims.add(name)
        return True

    admin = MagicMock()
    admin.list_routines = AsyncMock(return_value=routines)
    admin.create_routine_trigger = add_trigger
    admin.get_routine = get_routine
    monkeypatch.setattr(user_provision.container_repo, "try_claim_pass", claim)
    monkeypatch.setattr(user_provision.container_repo, "release_pass", AsyncMock())
    monkeypatch.setattr(user_provision, "_feed_timezone", AsyncMock(return_value="UTC"))
    row = _company_row(starter_ids={"ceo": "ceo1"})
    await asyncio.gather(
        user_provision._ensure_feed_routines(admin, "c", "org_p", row, "ceo1"),
        user_provision._ensure_feed_routines(admin, "c", "org_p", row, "ceo1"),
    )
    assert {routine_id: len(added) for routine_id, added in triggers.items()} == {"r8": 1, "r13": 1, "r18": 1, "r9": 1}
