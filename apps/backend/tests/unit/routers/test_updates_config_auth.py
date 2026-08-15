"""Authorization + audit + confirmation wiring for the container-config patch
routes in routers/updates.py.

Regression for the personal-context auth-bypass: both ``PATCH /container/config``
(fleet) and ``PATCH /container/config/{owner_id}`` (per-owner, owner from the
path) used ``Depends(get_current_user)`` plus an ``if auth.is_org_context:
require_org_admin(auth)`` branch — which passes through unconditionally for
personal (non-org) callers. That left every authenticated personal user able to
deep-merge config into another tenant's (or every tenant's) openclaw.json.

The fix gates both routes with ``require_platform_admin`` (the @isol8.co gate)
and removes the org-admin branch. These tests assert that wiring directly off the
FastAPI route definitions so the bypass can't silently return.

Issue #743 (a) added the two remaining fences on the same routes:
  * ``@audit_admin_action`` on BOTH — the fleet patch was the single most
    destructive endpoint in the product and left no trace in admin_actions.
  * A typed confirmation header on the FLEET route only —
    ``X-Confirm-Fleet-Patch: <ENVIRONMENT>``, 428 when absent or wrong.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.dependencies.utils import get_flat_dependant

from core.auth import AuthContext, get_current_user, require_platform_admin
from routers.updates import router


def _patch_config_routes():
    routes = {}
    for r in router.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", set()) or set()
        if path in ("/config/{owner_id}", "/config") and "PATCH" in methods:
            routes[path] = r
    return routes


def test_both_patch_config_routes_exist():
    routes = _patch_config_routes()
    assert set(routes) == {"/config/{owner_id}", "/config"}


def test_config_routes_are_guarded_by_platform_admin():
    """Each PATCH /config* route must depend (directly) on require_platform_admin."""
    routes = _patch_config_routes()
    assert routes, "expected the PATCH /config routes to exist"
    for path, route in routes.items():
        direct_calls = [d.call for d in route.dependant.dependencies]
        assert require_platform_admin in direct_calls, f"{path} is not guarded by require_platform_admin"


def test_config_routes_do_not_use_get_current_user_directly():
    """The old idiom injected get_current_user directly so the handler could run
    the (bypassed) is_org_context branch. After the fix, get_current_user only
    appears nested *under* require_platform_admin, never as a direct route dep.
    """
    routes = _patch_config_routes()
    for path, route in routes.items():
        direct_calls = [d.call for d in route.dependant.dependencies]
        assert get_current_user not in direct_calls, f"{path} still injects get_current_user directly (bypass idiom)"
        # get_current_user must still be reachable transitively (platform-admin
        # depends on it) — confirm the auth chain is intact, just gated.
        flat_calls = [d.call for d in get_flat_dependant(route.dependant).dependencies]
        assert get_current_user in flat_calls
        assert require_platform_admin in flat_calls


# ---------------------------------------------------------------------------
# Audit trail + fleet confirmation header (issue #743 item (a))
# ---------------------------------------------------------------------------

FLEET_PATH = "/api/v1/container/config"
SINGLE_PATH = "/api/v1/container/config/u_target"
# settings.ENVIRONMENT is unset under pytest, so the handler's expected
# confirmation value is its "local" fallback.
CONFIRM = {"X-Confirm-Fleet-Patch": "local"}


@pytest.fixture
def admin_env(app):
    """Platform admin (@isol8.co) for require_platform_admin."""
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_test_123", email="admin@isol8.co")
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def mock_audit_repo():
    """Patch the audit repo — the decorator writes synchronously on every call."""
    with patch("core.repositories.admin_actions_repo.create", new=AsyncMock(return_value={})) as mock:
        yield mock


@pytest.fixture
def mock_patcher():
    with patch("routers.updates.patch_openclaw_config", new=AsyncMock(return_value=None)) as mock:
        yield mock


@pytest.fixture
def one_owner_fleet():
    """billing-accounts scan yields exactly one owner."""
    with (
        patch("core.dynamodb.get_table"),
        patch("core.dynamodb.run_in_thread", new=AsyncMock(return_value={"Items": [{"owner_id": "o1"}]})),
    ):
        yield


class TestFleetPatchConfirmationHeader:
    """PATCH /container/config refuses to touch the fleet without a typed confirmation."""

    @pytest.mark.asyncio
    async def test_428_without_header(self, async_client, admin_env, mock_patcher, one_owner_fleet):
        res = await async_client.patch(FLEET_PATH, json={"patch": {"agents": {"x": 1}}})
        assert res.status_code == 428
        assert "X-Confirm-Fleet-Patch" in res.json()["detail"]
        # Nothing was written — the refusal happens before the owner scan.
        mock_patcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_428_with_wrong_environment(self, async_client, admin_env, mock_patcher, one_owner_fleet):
        res = await async_client.patch(
            FLEET_PATH,
            json={"patch": {"agents": {"x": 1}}},
            headers={"X-Confirm-Fleet-Patch": "prod"},
        )
        assert res.status_code == 428
        mock_patcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_proceeds_with_correct_header(self, async_client, admin_env, mock_patcher, one_owner_fleet):
        res = await async_client.patch(
            FLEET_PATH,
            json={"patch": {"agents": {"x": 1}}},
            headers=CONFIRM,
        )
        assert res.status_code == 200
        body = res.json()
        assert body["patched"] == 1
        assert body["failed"] == 0
        mock_patcher.assert_awaited_once_with("o1", {"agents": {"x": 1}})

    @pytest.mark.asyncio
    async def test_header_does_not_substitute_for_admin(self, async_client, mock_patcher, one_owner_fleet):
        """A correct confirmation header from a non-admin is still a 403 —
        the header is an intent check, never an authorization one."""
        res = await async_client.patch(FLEET_PATH, json={"patch": {}}, headers=CONFIRM)
        assert res.status_code == 403
        mock_patcher.assert_not_awaited()


class TestConfigPatchAuditTrail:
    """Both patch routes append to admin_actions."""

    @pytest.mark.asyncio
    async def test_fleet_patch_writes_audit_row(
        self, async_client, admin_env, mock_audit_repo, mock_patcher, one_owner_fleet
    ):
        res = await async_client.patch(FLEET_PATH, json={"patch": {"agents": {"x": 1}}}, headers=CONFIRM)
        assert res.status_code == 200
        mock_audit_repo.assert_awaited_once()
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "config.patch_fleet"
        assert kwargs["target_user_id"] == "__fleet__"
        assert kwargs["admin_user_id"] == "user_test_123"
        assert kwargs["result"] == "success"
        # Patch contents may carry secrets — redacted like /admin/users/{id}/config.
        assert kwargs["payload"] == {"patch": "***redacted***"}

    @pytest.mark.asyncio
    async def test_refused_fleet_patch_is_also_audited(
        self, async_client, admin_env, mock_audit_repo, mock_patcher, one_owner_fleet
    ):
        """A rejected attempt at the fleet is exactly what the trail is for."""
        res = await async_client.patch(FLEET_PATH, json={"patch": {}})
        assert res.status_code == 428
        mock_audit_repo.assert_awaited_once()
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "config.patch_fleet"
        assert kwargs["result"] == "error"
        assert kwargs["http_status"] == 428

    @pytest.mark.asyncio
    async def test_single_patch_writes_audit_row(self, async_client, admin_env, mock_audit_repo, mock_patcher):
        res = await async_client.patch(SINGLE_PATH, json={"patch": {"agents": {"x": 1}}})
        assert res.status_code == 200
        mock_audit_repo.assert_awaited_once()
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "config.patch"
        # Target is the owner from the PATH, not the calling admin.
        assert kwargs["target_user_id"] == "u_target"
        assert kwargs["admin_user_id"] == "user_test_123"
        assert kwargs["payload"] == {"patch": "***redacted***"}
