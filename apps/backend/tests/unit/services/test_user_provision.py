"""Unit tests for the workspace readiness helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.auth import AuthContext
from core.services import user_provision


@pytest.fixture(autouse=True)
def _existing_billing_account():
    with patch.object(
        user_provision.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_create_trial_local_if_absent():
    """Stub the trial_local create so tests never touch real billing DDB. Tests that care
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
        patch.object(user_provision.container_repo, "get_by_owner_id", AsyncMock(return_value=None)),
        patch.object(user_provision, "dispatch_provision", AsyncMock(return_value=True)),
    ):
        await user_provision.provision_new_workspace("org_456", created_by="user_creator")
    event = capture.call_args.kwargs
    assert event["event"] == "trial_started"
    assert event["distinct_id"] == "user_creator"
    assert event["properties"]["workspace_id"] == "org_456"


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


@pytest.fixture
def _no_container(monkeypatch):
    row = AsyncMock(return_value=None)
    monkeypatch.setattr(user_provision.container_repo, "get_by_owner_id", row)
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "billing",
    [
        None,
        {"subscription_status": "trial_local", "trial_end": 1},  # an expired trial
        {"subscription_status": "canceled"},
    ],
)
async def test_provision_new_workspace_dispatches_whatever_the_billing_says(monkeypatch, _no_container, billing):
    """Voice: provisioning is never billing-gated. A workspace on the shared pool costs nothing
    until it runs, and each spending door gates itself, so an expired or canceled row still gets
    its roster, rooms and profiles."""
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(user_provision.billing_repo, "get_by_owner_id", AsyncMock(return_value=billing))
    monkeypatch.setattr(user_provision.billing_repo, "create_trial_local_if_absent", AsyncMock(return_value=True))
    monkeypatch.setattr(user_provision, "capture_product_event", MagicMock())
    monkeypatch.setattr(user_provision, "capture_once_per_owner_per_day", MagicMock())
    monkeypatch.setattr("core.services.workspace_source.adopt_for_workspace", AsyncMock(return_value={}))
    monkeypatch.setattr(user_provision, "dispatch_provision", dispatch)
    await user_provision.provision_new_workspace("org_new", created_by="user_creator")
    dispatch.assert_awaited_once_with("org_new")
    _no_container.assert_awaited_once_with("org_new", consistent=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "dispatches"), [("running", False), ("error", True), ("provisioning", True)])
async def test_a_redelivered_event_retries_a_failed_provision_and_leaves_a_running_one(monkeypatch, status, dispatches):
    """organization.created is not deduped, so a redelivery must be a no-op on a healthy
    workspace and a retry on a failed (or stuck) one."""
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(user_provision, "_ensure_trial_row", AsyncMock())
    monkeypatch.setattr(user_provision.container_repo, "get_by_owner_id", AsyncMock(return_value={"status": status}))
    monkeypatch.setattr(user_provision, "dispatch_provision", dispatch)
    await user_provision.provision_new_workspace("org_new")
    assert dispatch.await_count == (1 if dispatches else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trial", "dispatched"),
    [(AsyncMock(side_effect=RuntimeError("ddb")), None), (AsyncMock(), False)],
)
async def test_provision_new_workspace_raises_so_svix_redelivers(monkeypatch, _no_container, trial, dispatched):
    """The webhook is the ONLY thing that starts a first provision now (no status poll re-drives
    it), so a trial-row failure or a dispatch that did not start RAISES -- and is metered."""
    metrics = []
    monkeypatch.setattr(user_provision, "_ensure_trial_row", trial)
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
