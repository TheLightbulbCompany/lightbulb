"""Tests for POST /container/recover endpoint."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.services import trial_cap


class TestContainerRecover:
    """Test POST /api/v1/container/recover."""

    @pytest.fixture
    def mock_ecs_manager(self):
        with patch("routers.container_recover.get_ecs_manager") as mock_getter:
            manager = AsyncMock()
            mock_getter.return_value = manager
            yield manager

    @pytest.fixture
    def mock_container_repo(self):
        with patch("routers.container_recover.container_repo") as mock_repo:
            mock_repo.update_error = AsyncMock(return_value=None)
            mock_repo.update_status = AsyncMock(return_value=None)
            yield mock_repo

    @pytest.fixture
    def mock_call_gateway_rpc(self):
        with patch("routers.container_recover._call_gateway_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.return_value = {}
            yield mock_rpc

    @pytest.fixture
    def mock_dispatch_provision(self):
        """Every reprovision sub-branch routes through the PROVISION executor
        (spec §7) — dispatch_provision replaces the old direct
        ecs_manager.provision_user_container call."""
        with patch("routers.container_recover.dispatch_provision", new_callable=AsyncMock) as mock_dispatch:
            yield mock_dispatch

    @pytest.fixture(autouse=True)
    def mock_trial_cap(self):
        """Recovering a stopped container is a WAKE, so it now checks the
        free-tier daily cap (which reads billing_repo). Default it to "under the
        cap" for the pre-existing tests; the guard tests below override it."""
        with patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=None)) as m:
            yield m

    # --- CONTAINER_DOWN: full re-provision ---

    @pytest.mark.asyncio
    async def test_recover_stopped_container_reprovisions(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """Stopped container should trigger full re-provision."""
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(None, None))
        mock_ecs_manager.get_service_status = AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "status": "stopped",
                "substatus": None,
            }
        )

        response = await async_client.post("/api/v1/container/recover")
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "reprovision"
        assert data["state"] == "CONTAINER_DOWN"
        mock_dispatch_provision.assert_called_once_with("org_test_456")

    @pytest.mark.asyncio
    async def test_recover_error_container_reprovisions(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """Error container should trigger full re-provision."""
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(None, None))
        mock_ecs_manager.get_service_status = AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "status": "error",
                "substatus": None,
                "last_error": "OutOfMemoryError",
            }
        )

        response = await async_client.post("/api/v1/container/recover")
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "reprovision"
        assert data["state"] == "CONTAINER_DOWN"
        mock_dispatch_provision.assert_called_once_with("org_test_456")

    # --- GATEWAY_DOWN: restart gateway ---

    @pytest.mark.asyncio
    async def test_recover_gateway_down_restarts(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """Running container with unresponsive gateway should restart gateway."""
        container = {
            "owner_id": "user_test_123",
            "gateway_token": "test-token",
            "status": "running",
        }
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(container, "10.0.1.5"))
        # Gateway health check fails, but update.run succeeds
        mock_call_gateway_rpc.side_effect = [
            ConnectionRefusedError(),  # health check fails
            {},  # update.run succeeds
        ]

        response = await async_client.post("/api/v1/container/recover")
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "gateway_restart"
        assert data["state"] == "GATEWAY_DOWN_DEAD"
        mock_dispatch_provision.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_gateway_down_escalates_to_reprovision(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """If gateway restart fails, escalate to reprovision."""
        container = {
            "owner_id": "user_test_123",
            "gateway_token": "test-token",
            "status": "running",
        }
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(container, "10.0.1.5"))
        # Both health check and restart fail
        mock_call_gateway_rpc.side_effect = ConnectionRefusedError()

        response = await async_client.post("/api/v1/container/recover")
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "reprovision"
        # The row must be flipped to "error" BEFORE the dispatch — the
        # dispatch's claim admits only missing/error/stopped/stale rows, so
        # dispatching against the still-"running" row would silently lose
        # the claim and start nothing.
        mock_container_repo.update_status.assert_called_once_with("org_test_456", "error")
        mock_dispatch_provision.assert_called_once_with("org_test_456")

    @pytest.mark.asyncio
    async def test_recover_running_but_no_ip_flips_to_error_then_reprovisions(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """A "running" row whose task is gone (no IP) must be flipped to
        "error" before the dispatch, or the dispatch's claim stands down
        against the running status and /recover answers "reprovision" while
        starting nothing."""
        container = {
            "owner_id": "user_test_123",
            "gateway_token": "test-token",
            "status": "running",
        }
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(container, None))

        response = await async_client.post("/api/v1/container/recover")
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "reprovision"
        mock_container_repo.update_status.assert_called_once_with("org_test_456", "error")
        mock_dispatch_provision.assert_called_once_with("org_test_456")

    # --- HEALTHY: no-op ---

    @pytest.mark.asyncio
    async def test_recover_healthy_returns_none(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """Healthy system should return action=none."""
        container = {
            "owner_id": "user_test_123",
            "gateway_token": "test-token",
            "status": "running",
        }
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(container, "10.0.1.5"))
        mock_call_gateway_rpc.return_value = {"ok": True}  # health check passes

        response = await async_client.post("/api/v1/container/recover")
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "none"
        assert data["state"] == "HEALTHY"
        mock_dispatch_provision.assert_not_called()

    # --- No container ---

    @pytest.mark.asyncio
    async def test_recover_no_container_returns_404(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """No container at all should return 404."""
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(None, None))
        mock_ecs_manager.get_service_status = AsyncMock(return_value=None)

        response = await async_client.post("/api/v1/container/recover")
        assert response.status_code == 404
        mock_dispatch_provision.assert_not_called()

    # --- The start guards (Task 13) ---
    #
    # /recover is the THIRD door onto a stopped container (after
    # GET /container/status's auto-retry and POST /container/provision). It
    # wakes the container through the PROVISION executor (dispatch_provision)
    # — whose runner starts with force=True, because that call IS a provision
    # — so it cannot inherit the guards start_user_service applies. It checks
    # them itself.

    @pytest.mark.asyncio
    async def test_recover_refuses_to_wake_an_over_cap_trial_owner(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """The free-tier cap is what scaled this container to zero. /recover must
        not be the way around it — same 402 limit card as the other two doors."""
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(None, None))
        mock_ecs_manager.get_service_status = AsyncMock(
            return_value={"owner_id": "user_test_123", "status": "stopped", "substatus": None}
        )

        with patch(
            "core.services.trial_cap.trial_block_reason",
            new=AsyncMock(return_value=trial_cap.DAILY_CAP),
        ):
            response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 402
        assert response.json()["detail"]["blocked"]["code"] == "trial_daily_cap"
        mock_dispatch_provision.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_refuses_to_wake_an_expired_trial_owner_under_the_cap(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """The OTHER half of the trial predicate. This door used to check only the
        daily cap, so an owner whose trial window had ENDED but who was under
        today's $3 fell straight through to the executor — whose runner starts
        with force=True and therefore skips start_user_service's own check —
        and woke a container that was out of runway. Fresh Bedrock spend on a
        dead trial."""
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(None, None))
        mock_ecs_manager.get_service_status = AsyncMock(
            return_value={"owner_id": "user_test_123", "status": "stopped", "substatus": None}
        )

        with patch(
            "core.services.trial_cap.trial_block_reason",
            new=AsyncMock(return_value=trial_cap.TRIAL_EXPIRED),
        ):
            response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 402
        assert response.json()["detail"]["blocked"]["code"] == "trial_expired"
        mock_dispatch_provision.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_refuses_a_capped_owners_zombie_running_row(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """The cap kill's ECS stop transiently leaves a "running" row whose
        task is gone — exactly the running/no-IP shape. That branch
        reprovisions (a force=True start), so without its own gate it was the
        one door a capped trial owner could wake the box through."""
        mock_ecs_manager.resolve_running_container = AsyncMock(
            return_value=(
                {"owner_id": "user_test_123", "status": "running", "bedrock_revoked": True},
                None,
            )
        )

        with patch(
            "core.services.trial_cap.trial_block_reason",
            new=AsyncMock(return_value=trial_cap.DAILY_CAP),
        ):
            response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 402
        assert response.json()["detail"]["blocked"]["code"] == "trial_daily_cap"
        mock_dispatch_provision.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_stands_down_while_a_conversion_is_draining(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """A trial->paid conversion has scaled the container to zero and is
        blocked in its drain wait. A reprovision dispatched from here would land a
        second task on the EFS-WAL SQLite. Note substatus is None: the conversion's
        own stop_user_service already clobbered the "converting" label, so only the
        conversion_started_at timestamp holds this off."""
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(None, None))
        mock_ecs_manager.get_service_status = AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "status": "stopped",
                "substatus": None,
                "conversion_pending": True,
                "conversion_started_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        with patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=None)):
            response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 200
        assert response.json()["action"] == "already_recovering"
        mock_dispatch_provision.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_stands_down_on_a_running_row_mid_conversion(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """The conversion drain is not instantaneous: reprovision_for_user scales
        the service to zero while the container ROW still reads "running", so a
        poll landing in that window resolves a running container whose gateway is
        already gone.

        The guard used to sit inside the stopped/error branch, so this shape fell
        through to the gateway-restart branch, failed its update.run RPC, and wrote
        status="error" + "Gateway restart failed — reprovisioning" over a row the
        conversion owned. That write rendered the terminal "Setup failed" card to a
        customer who had JUST paid, and dispatched a provision competing with the
        conversion's own. Reproduces the prod incident of 2026-08-06.
        """
        mock_ecs_manager.resolve_running_container = AsyncMock(
            return_value=(
                {
                    "owner_id": "user_test_123",
                    "status": "running",
                    "substatus": None,
                    "gateway_token": "tok",
                    "conversion_pending": True,
                    "conversion_started_at": datetime.now(timezone.utc).isoformat(),
                },
                "10.0.0.5",
            )
        )
        # Gateway is down (it was drained) and the restart RPC fails — exactly the
        # path that used to write the error.
        mock_call_gateway_rpc.side_effect = RuntimeError("connection refused")

        response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 200
        assert response.json()["action"] == "already_recovering"
        mock_dispatch_provision.assert_not_called()
        # The load-bearing assertion: nothing may mark the row errored. That write
        # is what surfaced "Setup failed" post-payment.
        mock_container_repo.update_status.assert_not_called()
        mock_container_repo.update_error.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_stands_down_on_a_running_row_with_no_ip_mid_conversion(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """Same window, one step earlier: the task is already gone so Cloud Map
        resolves no IP. The no-IP branch also wrote status="error" over the
        conversion's row before the guard was hoisted."""
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(None, None))
        mock_ecs_manager.get_service_status = AsyncMock(
            return_value={
                "owner_id": "user_test_123",
                "status": "running",
                "substatus": None,
                "conversion_pending": True,
                "conversion_started_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 200
        assert response.json()["action"] == "already_recovering"
        mock_dispatch_provision.assert_not_called()
        mock_container_repo.update_status.assert_not_called()
        mock_container_repo.update_error.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_stands_down_on_an_active_provision_lease(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """A provisioning row whose lease heartbeat is fresh is OWNED by a live
        SFN run — recover must not probe its half-booted gateway, write
        status="error" over it, or dispatch a competitor (the 2026-08-10
        conversion incident: four recover stomps during one provision)."""
        mock_container_repo.is_provision_claim_stale = lambda c: False
        mock_ecs_manager.resolve_running_container = AsyncMock(
            return_value=(
                {
                    "owner_id": "user_test_123",
                    "status": "provisioning",
                    "substatus": "task_running",
                    "provision_claim_id": "claim-1",
                    "gateway_token": "tok",
                },
                "10.0.1.5",
            )
        )

        response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 200
        assert response.json()["action"] == "already_recovering"
        mock_call_gateway_rpc.assert_not_called()
        mock_dispatch_provision.assert_not_called()
        mock_container_repo.update_status.assert_not_called()
        mock_container_repo.update_error.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_proceeds_on_a_stale_provision_lease(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """A stale lease means the runner died — recover stays a recovery
        path for wedged provisions and falls through to its normal branches."""
        mock_container_repo.is_provision_claim_stale = lambda c: True
        mock_ecs_manager.resolve_running_container = AsyncMock(
            return_value=(
                {
                    "owner_id": "user_test_123",
                    "status": "provisioning",
                    "substatus": "claimed",
                    "provision_claim_id": "claim-1",
                    "gateway_token": "tok",
                },
                "10.0.1.5",
            )
        )

        response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 200
        assert response.json()["action"] != "already_recovering"
