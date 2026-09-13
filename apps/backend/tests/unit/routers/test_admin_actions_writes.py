"""Tests for /api/v1/admin/* WRITE endpoints (container/billing/account/config/agent actions).

TDD-style: these tests were written BEFORE the action endpoints existed.

Each of the 15 mutation endpoints under test must:
  1. Be gated by `Depends(require_platform_admin)` (403 for non-admins)
  2. Be decorated with `@audit_admin_action("...")` so a row lands in
     admin_actions DDB synchronously (fail-closed)
  3. Accept an optional `Idempotency-Key` header (CEO D1) — the same key
     within a 60s window returns the cached response and short-circuits
     the underlying side effect

Endpoints (15 total):

Container actions (4):
  POST /api/v1/admin/users/{user_id}/container/reprovision
  POST /api/v1/admin/users/{user_id}/container/stop
  POST /api/v1/admin/users/{user_id}/container/start
  POST /api/v1/admin/users/{user_id}/container/resize         body: {tier}

Billing actions (4):
  POST /api/v1/admin/users/{user_id}/billing/cancel-subscription
  POST /api/v1/admin/users/{user_id}/billing/pause-subscription
  POST /api/v1/admin/users/{user_id}/billing/issue-credit     body: {amount_cents, reason}
  POST /api/v1/admin/users/{user_id}/billing/mark-invoice-resolved   body: {invoice_id}

Account actions (4):
  POST /api/v1/admin/users/{user_id}/account/suspend
  POST /api/v1/admin/users/{user_id}/account/reactivate
  POST /api/v1/admin/users/{user_id}/account/force-signout
  POST /api/v1/admin/users/{user_id}/account/resend-verification

Config + agent actions (3):
  PATCH /api/v1/admin/users/{user_id}/config                  body: {patch}
  POST  /api/v1/admin/users/{user_id}/agents/{agent_id}/delete
  POST  /api/v1/admin/users/{user_id}/agents/{agent_id}/clear-sessions
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.auth import AuthContext, get_current_user

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_env(app):
    """Caller has an @isol8.co email — require_platform_admin admits them.

    The fixture overrides get_current_user in app.dependency_overrides so the
    require_platform_admin check sees an AuthContext with email
    "admin@isol8.co". The teardown pops the override to keep fixtures from
    leaking across tests (the async_client fixture also clears overrides on
    its own teardown, but we pop explicitly here as a safety net in case a
    test uses admin_env without async_client).
    """
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_test_123", email="admin@isol8.co")
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def non_admin_env(app):
    """Caller's email does NOT end with @isol8.co — every admin endpoint 403s."""
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_test_123", email="user@example.com")
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def mock_audit_repo():
    """Patch admin_actions_repo.create — assert per-endpoint that it ran.

    The decorator (`@audit_admin_action`) calls this synchronously after the
    handler returns, so a single successful POST = exactly one repo.create.
    """
    with patch(
        "core.repositories.admin_actions_repo.create",
        new=AsyncMock(return_value={}),
    ) as mock:
        yield mock


# ===========================================================================
# Container actions
# ===========================================================================


class TestContainerReprovision:
    """POST /api/v1/admin/users/{user_id}/container/reprovision."""

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        ecs = MagicMock()
        ecs.reprovision_for_user = AsyncMock(return_value={"status": "started"})
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            res = await async_client.post("/api/v1/admin/users/u_target/container/reprovision")
        assert res.status_code == 200
        ecs.reprovision_for_user.assert_awaited_once_with("u_target")

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/u_target/container/reprovision")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        ecs = MagicMock()
        ecs.reprovision_for_user = AsyncMock(return_value={"status": "started"})
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            res = await async_client.post("/api/v1/admin/users/u_target/container/reprovision")
        assert res.status_code == 200
        mock_audit_repo.assert_awaited_once()
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "container.reprovision"
        assert kwargs["target_user_id"] == "u_target"
        assert kwargs["admin_user_id"] == "user_test_123"

    @pytest.mark.asyncio
    async def test_idempotent_within_window(self, async_client, admin_env):
        """Same Idempotency-Key within 60s => underlying service runs once."""
        ecs = MagicMock()
        ecs.reprovision_for_user = AsyncMock(return_value={"status": "started"})
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            headers = {"Idempotency-Key": "reprov-key-1"}
            r1 = await async_client.post("/api/v1/admin/users/u_target/container/reprovision", headers=headers)
            r2 = await async_client.post("/api/v1/admin/users/u_target/container/reprovision", headers=headers)
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json() == r2.json()
        assert ecs.reprovision_for_user.await_count == 1


class TestContainerStop:
    """POST /api/v1/admin/users/{user_id}/container/stop."""

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        ecs = MagicMock()
        ecs.stop_user_service = AsyncMock(return_value=None)
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            res = await async_client.post("/api/v1/admin/users/u_target/container/stop")
        assert res.status_code == 200
        ecs.stop_user_service.assert_awaited_once_with("u_target", reason="admin")

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/u_target/container/stop")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        ecs = MagicMock()
        ecs.stop_user_service = AsyncMock(return_value=None)
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            res = await async_client.post("/api/v1/admin/users/u_target/container/stop")
        assert res.status_code == 200
        mock_audit_repo.assert_awaited_once()
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "container.stop"
        assert kwargs["target_user_id"] == "u_target"

    @pytest.mark.asyncio
    async def test_idempotent_within_window(self, async_client, admin_env):
        ecs = MagicMock()
        ecs.stop_user_service = AsyncMock(return_value=None)
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            headers = {"Idempotency-Key": "stop-key-1"}
            r1 = await async_client.post("/api/v1/admin/users/u_target/container/stop", headers=headers)
            r2 = await async_client.post("/api/v1/admin/users/u_target/container/stop", headers=headers)
        assert r1.status_code == 200
        assert r1.json() == r2.json()
        assert ecs.stop_user_service.await_count == 1


class TestContainerStart:
    """POST /api/v1/admin/users/{user_id}/container/start."""

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        ecs = MagicMock()
        ecs.start_user_service = AsyncMock(return_value=None)
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            res = await async_client.post("/api/v1/admin/users/u_target/container/start")
        assert res.status_code == 200
        # force=True is the operator break-glass: an admin start is not subject
        # to the free-tier cap or the trial-conversion guard that
        # start_user_service applies to customer-driven starts, and it is the
        # only manual lever for a container with a stranded conversion marker.
        ecs.start_user_service.assert_awaited_once_with("u_target", force=True)

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/u_target/container/start")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        ecs = MagicMock()
        ecs.start_user_service = AsyncMock(return_value=None)
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            res = await async_client.post("/api/v1/admin/users/u_target/container/start")
        assert res.status_code == 200
        mock_audit_repo.assert_awaited_once()
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "container.start"

    @pytest.mark.asyncio
    async def test_idempotent_within_window(self, async_client, admin_env):
        ecs = MagicMock()
        ecs.start_user_service = AsyncMock(return_value=None)
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            headers = {"Idempotency-Key": "start-key-1"}
            r1 = await async_client.post("/api/v1/admin/users/u_target/container/start", headers=headers)
            r2 = await async_client.post("/api/v1/admin/users/u_target/container/start", headers=headers)
        assert r1.json() == r2.json()
        assert ecs.start_user_service.await_count == 1


class TestContainerResize:
    """POST /api/v1/admin/users/{user_id}/container/resize — no body.

    Re-applies the canonical flat-fee task profile. Recovery action for
    containers wedged on a stale task def.
    """

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        ecs = MagicMock()
        ecs.reapply_standard_sizing = AsyncMock(return_value={"status": "resized"})
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            res = await async_client.post("/api/v1/admin/users/u_target/container/resize")
        assert res.status_code == 200
        ecs.reapply_standard_sizing.assert_awaited_once_with("u_target")

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/u_target/container/resize")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        ecs = MagicMock()
        ecs.reapply_standard_sizing = AsyncMock(return_value={"status": "resized"})
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            res = await async_client.post("/api/v1/admin/users/u_target/container/resize")
        assert res.status_code == 200
        mock_audit_repo.assert_awaited_once()
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "container.resize"

    @pytest.mark.asyncio
    async def test_idempotent_within_window(self, async_client, admin_env):
        ecs = MagicMock()
        ecs.reapply_standard_sizing = AsyncMock(return_value={"status": "resized"})
        with patch("routers.admin.get_ecs_manager", return_value=ecs):
            headers = {"Idempotency-Key": "resize-key-1"}
            r1 = await async_client.post("/api/v1/admin/users/u_target/container/resize", headers=headers)
            r2 = await async_client.post("/api/v1/admin/users/u_target/container/resize", headers=headers)
        assert r1.json() == r2.json()
        assert ecs.reapply_standard_sizing.await_count == 1


class TestContainerForceApplyImageUpdate:
    """POST /api/v1/admin/users/{user_id}/container/apply-image-update.

    Admin-initiated force-apply of an image update NOW (no waiting for the
    owner to click the banner). Queues the pending update then applies it via
    the SAME ``apply_update()`` path the owner banner uses — non-destructive:
    reuses the container's existing on-disk openclaw.json (strips removed-schema
    keys first) and rolls the per-user task-def. EFS workspace/state preserved.
    """

    _CREATED = {"owner_id": "u_target", "update_id": "upd_123", "status": "pending"}

    def test_config_lock_refresh_is_a_conflicting_task_definition_roll(self):
        from core.services.update_service import TASK_DEFINITION_REFRESH_CHANGE
        from routers.admin import _rolls_task_def

        assert _rolls_task_def({TASK_DEFINITION_REFRESH_CHANGE: True}) is True

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        with (
            patch("routers.admin.update_repo.get_in_flight", new=AsyncMock(return_value=[])),
            patch("routers.admin.update_repo.create", new=AsyncMock(return_value=self._CREATED)) as mock_create,
            patch("routers.admin.apply_update", new=AsyncMock(return_value=True)) as mock_apply,
        ):
            res = await async_client.post(
                "/api/v1/admin/users/u_target/container/apply-image-update",
                json={"new_image": "ecr/lightbulb/openclaw-extended:2026.7.1-beta.3-isol8.1-e8e1cce"},
            )
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "applied"
        assert body["update_id"] == "upd_123"
        # A pending image_update row was created for the resolved owner...
        create_kwargs = mock_create.await_args.kwargs
        assert create_kwargs["owner_id"] == "u_target"
        assert create_kwargs["update_type"] == "image_update"
        assert create_kwargs["changes"]["new_image"].endswith("e8e1cce")
        # ...then applied via the exact apply_update() path the owner banner uses.
        mock_apply.assert_awaited_once_with("u_target", "upd_123")

    @pytest.mark.asyncio
    async def test_non_container_inflight_does_not_block(self, async_client, admin_env):
        """An in-flight row that does NOT rewrite the task-def (e.g. a
        paperclip_provision retry left 'pending', or a pure config_patch) must not
        block a force image apply — the guard keys on the changes payload, not the
        update_type label."""
        non_conflicting = [
            {"update_id": "prov", "status": "pending", "update_type": "paperclip_provision", "changes": {"op": "seed"}},
        ]
        with (
            patch("routers.admin.update_repo.get_in_flight", new=AsyncMock(return_value=non_conflicting)),
            patch("routers.admin.update_repo.create", new=AsyncMock(return_value=self._CREATED)),
            patch("routers.admin.apply_update", new=AsyncMock(return_value=True)) as mock_apply,
        ):
            res = await async_client.post(
                "/api/v1/admin/users/u_target/container/apply-image-update",
                json={"new_image": "img:tag"},
            )
        assert res.status_code == 200
        mock_apply.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post(
            "/api/v1/admin/users/u_target/container/apply-image-update",
            json={"new_image": "img:tag"},
        )
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_new_image_422(self, async_client, admin_env):
        res = await async_client.post(
            "/api/v1/admin/users/u_target/container/apply-image-update",
            json={},
        )
        assert res.status_code == 422

    @pytest.mark.asyncio
    async def test_409_when_owner_has_inflight_task_def_roll(self, async_client, admin_env):
        """Per-owner mutual exclusion: if another update that rewrites the task-def
        (carries new_image/new_cpu/new_memory) is already pending/scheduled/applying
        for the owner, reject with 409 BEFORE creating or applying a new one — else a
        second concurrent resize_user_container could race and clobber image/size."""
        with (
            patch(
                "routers.admin.update_repo.get_in_flight",
                new=AsyncMock(
                    return_value=[{"update_id": "other", "status": "applying", "changes": {"new_image": "old:tag"}}]
                ),
            ),
            patch("routers.admin.update_repo.create", new=AsyncMock()) as mock_create,
            patch("routers.admin.apply_update", new=AsyncMock()) as mock_apply,
        ):
            res = await async_client.post(
                "/api/v1/admin/users/u_target/container/apply-image-update",
                json={"new_image": "img:tag"},
            )
        assert res.status_code == 409
        # Guard runs first: neither a new row nor an apply is issued.
        mock_create.assert_not_awaited()
        mock_apply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_502_when_apply_raises(self, async_client, admin_env):
        """No in-flight row, so the freshly-created row always acquires; a False
        return therefore means apply_update caught an exception mid-roll and marked
        the row 'failed'. Surface that as 502 (not a queued-row 409)."""
        with (
            patch("routers.admin.update_repo.get_in_flight", new=AsyncMock(return_value=[])),
            patch("routers.admin.update_repo.create", new=AsyncMock(return_value=self._CREATED)),
            patch("routers.admin.apply_update", new=AsyncMock(return_value=False)),
        ):
            res = await async_client.post(
                "/api/v1/admin/users/u_target/container/apply-image-update",
                json={"new_image": "img:tag"},
            )
        assert res.status_code == 502

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        with (
            patch("routers.admin.update_repo.get_in_flight", new=AsyncMock(return_value=[])),
            patch("routers.admin.update_repo.create", new=AsyncMock(return_value=self._CREATED)),
            patch("routers.admin.apply_update", new=AsyncMock(return_value=True)),
        ):
            res = await async_client.post(
                "/api/v1/admin/users/u_target/container/apply-image-update",
                json={"new_image": "img:tag"},
            )
        assert res.status_code == 200
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "container.force_apply_image_update"
        assert kwargs["target_user_id"] == "u_target"

    @pytest.mark.asyncio
    async def test_idempotent_within_window(self, async_client, admin_env):
        """Same Idempotency-Key within 60s => the queue+apply runs exactly once."""
        with (
            patch("routers.admin.update_repo.get_in_flight", new=AsyncMock(return_value=[])),
            patch("routers.admin.update_repo.create", new=AsyncMock(return_value=self._CREATED)),
            patch("routers.admin.apply_update", new=AsyncMock(return_value=True)) as mock_apply,
        ):
            headers = {"Idempotency-Key": "force-apply-key-1"}
            body = {"new_image": "img:tag"}
            r1 = await async_client.post(
                "/api/v1/admin/users/u_target/container/apply-image-update",
                json=body,
                headers=headers,
            )
            r2 = await async_client.post(
                "/api/v1/admin/users/u_target/container/apply-image-update",
                json=body,
                headers=headers,
            )
        assert r1.json() == r2.json()
        assert mock_apply.await_count == 1


class TestContainerReapplyConfig:
    """POST /api/v1/admin/users/{user_id}/container/reapply-config.

    Regenerates the owner's openclaw.json from the CURRENT config generator
    (dashboard twin of scripts/reapply_openclaw_config.py). Config-only —
    no task restart; the route delegates entirely to
    ``update_service.reapply_owner_config``, which handles the regen +
    runtime-subtree restore.
    """

    _RESULT = {
        "owner_id": "u_target",
        "provider_choice": "bedrock_claude",
        "byo_provider": None,
        "restored_subtrees": ["bindings", "channels", "identity"],
    }

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        with patch("routers.admin.reapply_owner_config", new=AsyncMock(return_value=self._RESULT)) as mock_reapply:
            res = await async_client.post("/api/v1/admin/users/u_target/container/reapply-config")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "reapplied"
        assert body["restored_subtrees"] == ["bindings", "channels", "identity"]
        mock_reapply.assert_awaited_once_with("u_target")

    @pytest.mark.asyncio
    async def test_404_when_no_container_row(self, async_client, admin_env):
        with patch(
            "routers.admin.reapply_owner_config",
            new=AsyncMock(side_effect=LookupError("No container row for owner u_target")),
        ):
            res = await async_client.post("/api/v1/admin/users/u_target/container/reapply-config")
        assert res.status_code == 404

    @pytest.mark.asyncio
    async def test_409_for_legacy_row_without_gateway_token(self, async_client, admin_env):
        """Legacy rows without a gateway_token are refused — minting a fresh
        token would diverge from the running task-def and break gateway auth."""
        with patch(
            "routers.admin.reapply_owner_config",
            new=AsyncMock(side_effect=ValueError("no gateway_token")),
        ):
            res = await async_client.post("/api/v1/admin/users/u_target/container/reapply-config")
        assert res.status_code == 409

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/u_target/container/reapply-config")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        with patch("routers.admin.reapply_owner_config", new=AsyncMock(return_value=self._RESULT)):
            res = await async_client.post("/api/v1/admin/users/u_target/container/reapply-config")
        assert res.status_code == 200
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "container.reapply_config"
        assert kwargs["target_user_id"] == "u_target"

    @pytest.mark.asyncio
    async def test_idempotent_within_window(self, async_client, admin_env):
        with patch("routers.admin.reapply_owner_config", new=AsyncMock(return_value=self._RESULT)) as mock_reapply:
            headers = {"Idempotency-Key": "reapply-key-1"}
            r1 = await async_client.post("/api/v1/admin/users/u_target/container/reapply-config", headers=headers)
            r2 = await async_client.post("/api/v1/admin/users/u_target/container/reapply-config", headers=headers)
        assert r1.json() == r2.json()
        assert mock_reapply.await_count == 1


# ===========================================================================
# Provisioning actions (read + retry/reset-bootstrap levers)
# ===========================================================================


class TestProvisionRetry:
    """POST /api/v1/admin/users/{user_id}/provisioning/retry.

    Re-dispatches provisioning through dispatch_provision (never raises).
    Covers the error-row retry AND the stale-claim wedge rescue — unlike
    the owner-facing /container/retry, there is no billing gate here: this
    is an operator break-glass, audited.
    """

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/u_target/provisioning/retry")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_404_when_no_container_row(self, async_client, admin_env):
        with patch("routers.admin.container_repo.get_by_owner_id", new=AsyncMock(return_value=None)):
            res = await async_client.post("/api/v1/admin/users/u_target/provisioning/retry")
        assert res.status_code == 404

    @pytest.mark.asyncio
    async def test_409_when_running(self, async_client, admin_env):
        row = {"status": "running", "substatus": None}
        with patch("routers.admin.container_repo.get_by_owner_id", new=AsyncMock(return_value=row)):
            res = await async_client.post("/api/v1/admin/users/u_target/provisioning/retry")
        assert res.status_code == 409

    @pytest.mark.asyncio
    async def test_409_when_provisioning_with_fresh_claim(self, async_client, admin_env):
        row = {"status": "provisioning", "provision_claim_id": "claim-1"}
        with (
            patch("routers.admin.container_repo.get_by_owner_id", new=AsyncMock(return_value=row)),
            patch("routers.admin.container_repo.is_provision_claim_stale", return_value=False),
        ):
            res = await async_client.post("/api/v1/admin/users/u_target/provisioning/retry")
        assert res.status_code == 409

    @pytest.mark.asyncio
    async def test_200_dispatches_when_error(self, async_client, admin_env):
        before = {"status": "error", "substatus": "boom"}
        after = {"status": "provisioning", "substatus": None, "provision_claim_id": "claim-2"}
        mock_get = AsyncMock(side_effect=[before, after])
        mock_dispatch = AsyncMock(return_value=None)
        with (
            patch("routers.admin.container_repo.get_by_owner_id", new=mock_get),
            patch("routers.admin.dispatch_provision", new=mock_dispatch),
        ):
            res = await async_client.post("/api/v1/admin/users/u_target/provisioning/retry")
        assert res.status_code == 200
        mock_dispatch.assert_awaited_once_with("u_target")
        body = res.json()
        assert body["ok"] is True
        assert body["status"] == "provisioning"
        assert body["provision_claim_id"] == "claim-2"

    @pytest.mark.asyncio
    async def test_200_dispatches_when_provisioning_and_stale(self, async_client, admin_env):
        before = {"status": "provisioning", "provision_claim_id": "claim-old"}
        after = {"status": "provisioning", "substatus": None, "provision_claim_id": "claim-new"}
        mock_get = AsyncMock(side_effect=[before, after])
        mock_dispatch = AsyncMock(return_value=None)
        with (
            patch("routers.admin.container_repo.get_by_owner_id", new=mock_get),
            patch("routers.admin.container_repo.is_provision_claim_stale", return_value=True),
            patch("routers.admin.dispatch_provision", new=mock_dispatch),
        ):
            res = await async_client.post("/api/v1/admin/users/u_target/provisioning/retry")
        assert res.status_code == 200
        mock_dispatch.assert_awaited_once_with("u_target")

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        before = {"status": "error", "substatus": "boom"}
        after = {"status": "provisioning", "substatus": None, "provision_claim_id": "claim-2"}
        mock_get = AsyncMock(side_effect=[before, after])
        with (
            patch("routers.admin.container_repo.get_by_owner_id", new=mock_get),
            patch("routers.admin.dispatch_provision", new=AsyncMock(return_value=None)),
        ):
            res = await async_client.post("/api/v1/admin/users/u_target/provisioning/retry")
        assert res.status_code == 200
        mock_audit_repo.assert_awaited_once()
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "provision.retry"
        assert kwargs["target_user_id"] == "u_target"


class TestBillingCancelSubscription:
    """POST /api/v1/admin/users/{user_id}/billing/cancel-subscription."""

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        billing = MagicMock()
        billing.cancel_subscription_for_owner = AsyncMock(return_value={"status": "canceled"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post("/api/v1/admin/users/u_target/billing/cancel-subscription")
        assert res.status_code == 200
        billing.cancel_subscription_for_owner.assert_awaited_once_with("u_target")

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/u_target/billing/cancel-subscription")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        billing = MagicMock()
        billing.cancel_subscription_for_owner = AsyncMock(return_value={"status": "canceled"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post("/api/v1/admin/users/u_target/billing/cancel-subscription")
        assert res.status_code == 200
        mock_audit_repo.assert_awaited_once()
        assert mock_audit_repo.await_args.kwargs["action"] == "billing.cancel_subscription"

    @pytest.mark.asyncio
    async def test_idempotent_within_window(self, async_client, admin_env):
        billing = MagicMock()
        billing.cancel_subscription_for_owner = AsyncMock(return_value={"status": "canceled"})
        with patch("routers.admin.billing_service", new=billing):
            headers = {"Idempotency-Key": "cancel-key-1"}
            r1 = await async_client.post(
                "/api/v1/admin/users/u_target/billing/cancel-subscription",
                headers=headers,
            )
            r2 = await async_client.post(
                "/api/v1/admin/users/u_target/billing/cancel-subscription",
                headers=headers,
            )
        assert r1.json() == r2.json()
        assert billing.cancel_subscription_for_owner.await_count == 1


class TestBillingPauseSubscription:
    """POST /api/v1/admin/users/{user_id}/billing/pause-subscription."""

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        billing = MagicMock()
        billing.pause_subscription_for_owner = AsyncMock(return_value={"status": "paused"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post("/api/v1/admin/users/u_target/billing/pause-subscription")
        assert res.status_code == 200
        billing.pause_subscription_for_owner.assert_awaited_once_with("u_target")

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/u_target/billing/pause-subscription")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        billing = MagicMock()
        billing.pause_subscription_for_owner = AsyncMock(return_value={"status": "paused"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post("/api/v1/admin/users/u_target/billing/pause-subscription")
        assert res.status_code == 200
        assert mock_audit_repo.await_args.kwargs["action"] == "billing.pause_subscription"

    @pytest.mark.asyncio
    async def test_idempotent_within_window(self, async_client, admin_env):
        billing = MagicMock()
        billing.pause_subscription_for_owner = AsyncMock(return_value={"status": "paused"})
        with patch("routers.admin.billing_service", new=billing):
            headers = {"Idempotency-Key": "pause-key-1"}
            r1 = await async_client.post(
                "/api/v1/admin/users/u_target/billing/pause-subscription",
                headers=headers,
            )
            r2 = await async_client.post(
                "/api/v1/admin/users/u_target/billing/pause-subscription",
                headers=headers,
            )
        assert r1.json() == r2.json()
        assert billing.pause_subscription_for_owner.await_count == 1


class TestBillingIssueCredit:
    """POST /api/v1/admin/users/{user_id}/billing/issue-credit body: {amount_cents, reason}."""

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        billing = MagicMock()
        billing.issue_credit_for_owner = AsyncMock(return_value={"credit_id": "cn_abc"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post(
                "/api/v1/admin/users/u_target/billing/issue-credit",
                json={"amount_cents": 500, "reason": "incident_compensation"},
            )
        assert res.status_code == 200
        billing.issue_credit_for_owner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post(
            "/api/v1/admin/users/u_target/billing/issue-credit",
            json={"amount_cents": 500, "reason": "x"},
        )
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        billing = MagicMock()
        billing.issue_credit_for_owner = AsyncMock(return_value={"credit_id": "cn_abc"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post(
                "/api/v1/admin/users/u_target/billing/issue-credit",
                json={"amount_cents": 500, "reason": "incident_comp"},
            )
        assert res.status_code == 200
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "billing.issue_credit"
        # Both amount + reason are captured in payload (not sensitive).
        payload = kwargs["payload"]
        assert payload.get("amount_cents") == 500
        assert payload.get("reason") == "incident_comp"

    @pytest.mark.asyncio
    async def test_body_validation_requires_amount_and_reason(self, async_client, admin_env):
        billing = MagicMock()
        billing.issue_credit_for_owner = AsyncMock()
        with patch("routers.admin.billing_service", new=billing):
            # Missing reason
            res1 = await async_client.post(
                "/api/v1/admin/users/u_target/billing/issue-credit",
                json={"amount_cents": 500},
            )
            # Missing amount_cents
            res2 = await async_client.post(
                "/api/v1/admin/users/u_target/billing/issue-credit",
                json={"reason": "x"},
            )
            # Empty body
            res3 = await async_client.post(
                "/api/v1/admin/users/u_target/billing/issue-credit",
                json={},
            )
        assert res1.status_code == 422
        assert res2.status_code == 422
        assert res3.status_code == 422
        billing.issue_credit_for_owner.assert_not_called()

    @pytest.mark.asyncio
    async def test_idempotent_within_window(self, async_client, admin_env):
        billing = MagicMock()
        billing.issue_credit_for_owner = AsyncMock(return_value={"credit_id": "cn_abc"})
        with patch("routers.admin.billing_service", new=billing):
            headers = {"Idempotency-Key": "credit-key-1"}
            body = {"amount_cents": 500, "reason": "x"}
            r1 = await async_client.post(
                "/api/v1/admin/users/u_target/billing/issue-credit",
                json=body,
                headers=headers,
            )
            r2 = await async_client.post(
                "/api/v1/admin/users/u_target/billing/issue-credit",
                json=body,
                headers=headers,
            )
        assert r1.json() == r2.json()
        assert billing.issue_credit_for_owner.await_count == 1


class TestBillingMarkInvoiceResolved:
    """POST /api/v1/admin/users/{user_id}/billing/mark-invoice-resolved body: {invoice_id}."""

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        billing = MagicMock()
        billing.mark_invoice_resolved = AsyncMock(return_value={"status": "resolved"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post(
                "/api/v1/admin/users/u_target/billing/mark-invoice-resolved",
                json={"invoice_id": "in_123"},
            )
        assert res.status_code == 200
        billing.mark_invoice_resolved.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post(
            "/api/v1/admin/users/u_target/billing/mark-invoice-resolved",
            json={"invoice_id": "in_123"},
        )
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        billing = MagicMock()
        billing.mark_invoice_resolved = AsyncMock(return_value={"status": "resolved"})
        with patch("routers.admin.billing_service", new=billing):
            res = await async_client.post(
                "/api/v1/admin/users/u_target/billing/mark-invoice-resolved",
                json={"invoice_id": "in_123"},
            )
        assert res.status_code == 200
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "billing.mark_invoice_resolved"
        assert kwargs["payload"].get("invoice_id") == "in_123"

    @pytest.mark.asyncio
    async def test_idempotent_within_window(self, async_client, admin_env):
        billing = MagicMock()
        billing.mark_invoice_resolved = AsyncMock(return_value={"status": "resolved"})
        with patch("routers.admin.billing_service", new=billing):
            headers = {"Idempotency-Key": "inv-key-1"}
            body = {"invoice_id": "in_123"}
            r1 = await async_client.post(
                "/api/v1/admin/users/u_target/billing/mark-invoice-resolved",
                json=body,
                headers=headers,
            )
            r2 = await async_client.post(
                "/api/v1/admin/users/u_target/billing/mark-invoice-resolved",
                json=body,
                headers=headers,
            )
        assert r1.json() == r2.json()
        assert billing.mark_invoice_resolved.await_count == 1


# ===========================================================================
# Account actions
# ===========================================================================


class TestAccountSuspend:
    """POST /api/v1/admin/users/{user_id}/account/suspend → clerk_admin.ban_user."""

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        with patch(
            "routers.admin.clerk_admin.ban_user",
            new=AsyncMock(return_value={"banned": True}),
        ) as mock_ban:
            res = await async_client.post("/api/v1/admin/users/u_target/account/suspend")
        assert res.status_code == 200
        mock_ban.assert_awaited_once_with("u_target")

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/u_target/account/suspend")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        with patch(
            "routers.admin.clerk_admin.ban_user",
            new=AsyncMock(return_value={"banned": True}),
        ):
            res = await async_client.post("/api/v1/admin/users/u_target/account/suspend")
        assert res.status_code == 200
        assert mock_audit_repo.await_args.kwargs["action"] == "account.suspend"


class TestAccountReactivate:
    """POST /api/v1/admin/users/{user_id}/account/reactivate → clerk_admin.unban_user."""

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        with patch(
            "routers.admin.clerk_admin.unban_user",
            new=AsyncMock(return_value={"banned": False}),
        ) as mock_unban:
            res = await async_client.post("/api/v1/admin/users/u_target/account/reactivate")
        assert res.status_code == 200
        mock_unban.assert_awaited_once_with("u_target")

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/u_target/account/reactivate")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        with patch(
            "routers.admin.clerk_admin.unban_user",
            new=AsyncMock(return_value={"banned": False}),
        ):
            res = await async_client.post("/api/v1/admin/users/u_target/account/reactivate")
        assert res.status_code == 200
        assert mock_audit_repo.await_args.kwargs["action"] == "account.reactivate"


class TestAccountForceSignout:
    """POST /api/v1/admin/users/{user_id}/account/force-signout → clerk_admin.revoke_sessions."""

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        with patch(
            "routers.admin.clerk_admin.revoke_sessions",
            new=AsyncMock(return_value={"revoked": 3}),
        ) as mock_revoke:
            res = await async_client.post("/api/v1/admin/users/u_target/account/force-signout")
        assert res.status_code == 200
        mock_revoke.assert_awaited_once_with("u_target")

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/u_target/account/force-signout")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        with patch(
            "routers.admin.clerk_admin.revoke_sessions",
            new=AsyncMock(return_value={"revoked": 3}),
        ):
            res = await async_client.post("/api/v1/admin/users/u_target/account/force-signout")
        assert res.status_code == 200
        assert mock_audit_repo.await_args.kwargs["action"] == "account.force_signout"


class TestAccountResendVerification:
    """POST /api/v1/admin/users/{user_id}/account/resend-verification."""

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        with patch(
            "routers.admin.clerk_admin.resend_verification",
            new=AsyncMock(return_value={"sent": True}),
        ) as mock_resend:
            res = await async_client.post("/api/v1/admin/users/u_target/account/resend-verification")
        assert res.status_code == 200
        mock_resend.assert_awaited_once_with("u_target")

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/u_target/account/resend-verification")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        with patch(
            "routers.admin.clerk_admin.resend_verification",
            new=AsyncMock(return_value={"sent": True}),
        ):
            res = await async_client.post("/api/v1/admin/users/u_target/account/resend-verification")
        assert res.status_code == 200
        assert mock_audit_repo.await_args.kwargs["action"] == "account.resend_verification"


# ===========================================================================
# Config + agent actions
# ===========================================================================


class TestConfigPatch:
    """PATCH /api/v1/admin/users/{user_id}/config body: {patch}.

    Wraps the existing core.services.config_patcher.patch_openclaw_config.
    Patch body field ('patch') must be REDACTED in the audit row — config
    patches frequently contain BYOK/api keys and other secrets.
    """

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        with patch(
            "routers.admin.patch_openclaw_config",
            new=AsyncMock(return_value=None),
        ) as mock_patch:
            res = await async_client.patch(
                "/api/v1/admin/users/u_target/config",
                json={"patch": {"agents": {"defaults": {"model": {"primary": "qwen3-vl-235b"}}}}},
            )
        assert res.status_code == 200
        mock_patch.assert_awaited_once()
        # Owner id forwarded; the patch payload is whatever the body contained.
        call = mock_patch.await_args
        owner_arg = call.args[0] if call.args else call.kwargs.get("owner_id")
        assert owner_arg == "u_target"

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.patch(
            "/api/v1/admin/users/u_target/config",
            json={"patch": {"agents": {}}},
        )
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        with patch(
            "routers.admin.patch_openclaw_config",
            new=AsyncMock(return_value=None),
        ):
            res = await async_client.patch(
                "/api/v1/admin/users/u_target/config",
                json={"patch": {"agents": {"defaults": {}}}},
            )
        assert res.status_code == 200
        assert mock_audit_repo.await_args.kwargs["action"] == "config.patch"

    @pytest.mark.asyncio
    async def test_patch_body_redacted_in_audit(self, async_client, admin_env, mock_audit_repo):
        """The decorator's redact_paths=['patch'] hides the patch contents from
        the audit row — config patches commonly contain BYOK keys / secrets."""
        with patch(
            "routers.admin.patch_openclaw_config",
            new=AsyncMock(return_value=None),
        ):
            res = await async_client.patch(
                "/api/v1/admin/users/u_target/config",
                json={"patch": {"providers": {"openai": {"api_key": "sk-superSecret123"}}}},
            )
        assert res.status_code == 200
        audit_payload = mock_audit_repo.await_args.kwargs["payload"]
        # The whole `patch` field should be redacted — secret string never lands.
        assert audit_payload.get("patch") == "***redacted***"
        # And the literal secret never appears anywhere in the audit row.
        assert "sk-superSecret123" not in str(audit_payload)


class TestAgentDelete:
    """POST /api/v1/admin/users/{user_id}/agents/{agent_id}/delete → gateway agents.delete."""

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        delete_everywhere = AsyncMock(return_value={"deleted": True, "agent_id": "agent_42"})
        with patch(
            "core.services.paperclip_agent_lifecycle.delete_agent_everywhere",
            delete_everywhere,
        ):
            res = await async_client.post("/api/v1/admin/users/u_target/agents/agent_42/delete")
        assert res.status_code == 200
        delete_everywhere.assert_awaited_once_with(owner_id="u_target", agent_id="agent_42")

    @pytest.mark.asyncio
    async def test_final_root_conflict_is_409(self, async_client, admin_env):
        from core.services.paperclip_agent_lifecycle import AgentLifecycleConflict

        with patch(
            "core.services.paperclip_agent_lifecycle.delete_agent_everywhere",
            new=AsyncMock(side_effect=AgentLifecycleConflict("The final root CEO cannot be deleted or demoted.")),
        ):
            res = await async_client.post("/api/v1/admin/users/u_target/agents/root/delete")
        assert res.status_code == 409
        assert res.json()["detail"] == "The final root CEO cannot be deleted or demoted."

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/u_target/agents/agent_42/delete")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        with patch(
            "core.services.paperclip_agent_lifecycle.delete_agent_everywhere",
            new=AsyncMock(return_value={"deleted": True}),
        ):
            res = await async_client.post("/api/v1/admin/users/u_target/agents/agent_42/delete")
        assert res.status_code == 200
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "agent.delete"
        assert kwargs["target_user_id"] == "u_target"


class TestAgentClearSessions:
    """POST /api/v1/admin/users/{user_id}/agents/{agent_id}/clear-sessions → gateway sessions.clear."""

    @pytest.mark.asyncio
    async def test_happy_path(self, async_client, admin_env):
        call = AsyncMock(return_value={"cleared": 7})
        with patch("routers.admin.container_rpc.call", call):
            res = await async_client.post("/api/v1/admin/users/u_target/agents/agent_42/clear-sessions")
        assert res.status_code == 200
        call.assert_awaited_once()
        args = call.await_args.args
        assert args[0] == "u_target"
        assert args[1] == "sessions.clear"
        assert args[2].get("agent_id") == "agent_42"

    @pytest.mark.asyncio
    async def test_403_for_non_admin(self, async_client, non_admin_env):
        res = await async_client.post("/api/v1/admin/users/u_target/agents/agent_42/clear-sessions")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_audit_row_written(self, async_client, admin_env, mock_audit_repo):
        call = AsyncMock(return_value={"cleared": 7})
        with patch("routers.admin.container_rpc.call", call):
            res = await async_client.post("/api/v1/admin/users/u_target/agents/agent_42/clear-sessions")
        assert res.status_code == 200
        kwargs = mock_audit_repo.await_args.kwargs
        assert kwargs["action"] == "agent.clear_sessions"
