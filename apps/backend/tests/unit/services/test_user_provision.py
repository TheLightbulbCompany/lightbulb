"""Unit tests for the workspace readiness helpers."""

from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.services import user_provision


@pytest.fixture(autouse=True)
def _existing_billing_account(monkeypatch):
    # monkeypatch, as the tests' own overrides use: a `with patch` here exits before the earlier-created
    # monkeypatch undoes those overrides, which would put this mock back on billing_repo for later modules.
    monkeypatch.setattr(
        user_provision.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})
    )


@pytest.fixture(autouse=True)
def _mock_create_trial_local_if_absent(monkeypatch):
    """Stub the trial_local create so tests never touch real billing DDB. Tests that care
    about this call inspect/override the mock directly."""
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(user_provision.billing_repo, "create_trial_local_if_absent", mock)
    return mock


def _personal_auth(user_id: str = "user_123", email: str = "u@example.com", human_session=False) -> AuthContext:
    """Personal-context AuthContext (no org)."""
    return AuthContext(user_id=user_id, email=email, human_session=human_session)


def _org_admin_auth(
    user_id: str = "user_123",
    org_id: str = "org_456",
    email: str = "u@example.com",
) -> AuthContext:
    """Org-admin AuthContext. is_org_context + is_org_admin are derived properties."""
    return AuthContext(user_id=org_id, email=email, human_session=True)


def _org_member_auth(
    user_id: str = "user_456",
    org_id: str = "org_456",
    email: str = "m@example.com",
) -> AuthContext:
    """Org-member AuthContext. is_org_admin returns False because role != org:admin."""
    return AuthContext(user_id=org_id, email=email, human_session=True)


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
    _mock_create_trial_local_if_absent.assert_awaited_once_with(owner_id="org_456", trial_end=None)
    event = capture.call_args.kwargs
    # The PERSON is the human who hit the door; the workspace rides as a property.
    assert event["distinct_id"] == auth.user_id
    assert event["event"] == "trial_started"
    assert event["properties"] == {"workspace_id": "org_456"}


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
    assert first["properties"] == {"workspace_id": "org_456"}
    # Two humans, one workspace: different persons, one deterministic uuid, so
    # PostHog keeps the earlier capture and the trial counts once.
    assert first["distinct_id"] == _org_admin_auth().user_id
    assert second["distinct_id"] == _org_member_auth().user_id
    assert first["event_uuid"] == second["event_uuid"]


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
