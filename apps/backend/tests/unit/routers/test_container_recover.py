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
            yield mock_repo

    @pytest.fixture
    def mock_call_gateway_rpc(self):
        with patch("routers.container_recover._call_gateway_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.return_value = {}
            yield mock_rpc

    @pytest.fixture(autouse=True)
    def mock_billing_repo(self):
        """Recovery reads provider_choice from billing_repo (keyed on
        owner_id) before reprovisioning. Mock the lookup so tests don't
        hit DDB.

        Patches the lookup at its source — `resolve_provider_choice` now
        lives in `core.services.container_provision` (the local copy in
        `routers/container_recover` was deleted), so this is where
        `billing_repo` is actually resolved."""
        with patch("core.services.container_provision.billing_repo") as mock_repo:
            # No row / no choice persisted -> falls through to bedrock_claude default.
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)
            yield mock_repo

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
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc
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
        mock_ecs_manager.provision_user_container = AsyncMock(return_value="openclaw-new")

        response = await async_client.post("/api/v1/container/recover")
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "reprovision"
        assert data["state"] == "CONTAINER_DOWN"
        mock_ecs_manager.provision_user_container.assert_called_once()

    @pytest.mark.asyncio
    async def test_recover_error_container_reprovisions(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc
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
        mock_ecs_manager.provision_user_container = AsyncMock(return_value="openclaw-new")

        response = await async_client.post("/api/v1/container/recover")
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "reprovision"
        assert data["state"] == "CONTAINER_DOWN"

    # --- GATEWAY_DOWN: restart gateway ---

    @pytest.mark.asyncio
    async def test_recover_gateway_down_restarts(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc
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

    @pytest.mark.asyncio
    async def test_recover_gateway_down_escalates_to_reprovision(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc
    ):
        """If gateway restart fails, escalate to reprovision."""
        container = {
            "owner_id": "user_test_123",
            "gateway_token": "test-token",
            "status": "running",
        }
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(container, "10.0.1.5"))
        mock_ecs_manager.provision_user_container = AsyncMock(return_value="openclaw-new")
        # Both health check and restart fail
        mock_call_gateway_rpc.side_effect = ConnectionRefusedError()

        response = await async_client.post("/api/v1/container/recover")
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "reprovision"

    # --- HEALTHY: no-op ---

    @pytest.mark.asyncio
    async def test_recover_healthy_returns_none(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc
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

    # --- No container ---

    @pytest.mark.asyncio
    async def test_recover_no_container_returns_404(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc
    ):
        """No container at all should return 404."""
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(None, None))
        mock_ecs_manager.get_service_status = AsyncMock(return_value=None)

        response = await async_client.post("/api/v1/container/recover")
        assert response.status_code == 404

    # --- The start guards (Task 13) ---
    #
    # /recover is the THIRD door onto a stopped container (after
    # GET /container/status's auto-retry and POST /container/provision). It wakes
    # the container through provision_user_container — whose own start is
    # force=True, because that call IS a provision — so it cannot inherit the
    # guards start_user_service applies. It checks them itself.

    @pytest.mark.asyncio
    async def test_recover_refuses_to_wake_an_over_cap_trial_owner(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc
    ):
        """The free-tier cap is what scaled this container to zero. /recover must
        not be the way around it — same 402 limit card as the other two doors."""
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(None, None))
        mock_ecs_manager.get_service_status = AsyncMock(
            return_value={"owner_id": "user_test_123", "status": "stopped", "substatus": None}
        )
        mock_ecs_manager.provision_user_container = AsyncMock(return_value="openclaw-new")

        with patch(
            "core.services.trial_cap.trial_block_reason",
            new=AsyncMock(return_value=trial_cap.DAILY_CAP),
        ):
            response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 402
        assert response.json()["detail"]["blocked"]["code"] == "trial_daily_cap"
        mock_ecs_manager.provision_user_container.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_refuses_to_wake_an_expired_trial_owner_under_the_cap(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc
    ):
        """The OTHER half of the trial predicate. This door used to check only the
        daily cap, so an owner whose trial window had ENDED but who was under
        today's $3 fell straight through to provision_user_container — whose start
        is force=True and therefore skips start_user_service's own check — and woke
        a container that was out of runway. Fresh Bedrock spend on a dead trial."""
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(None, None))
        mock_ecs_manager.get_service_status = AsyncMock(
            return_value={"owner_id": "user_test_123", "status": "stopped", "substatus": None}
        )
        mock_ecs_manager.provision_user_container = AsyncMock(return_value="openclaw-new")

        with patch(
            "core.services.trial_cap.trial_block_reason",
            new=AsyncMock(return_value=trial_cap.TRIAL_EXPIRED),
        ):
            response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 402
        assert response.json()["detail"]["blocked"]["code"] == "trial_expired"
        mock_ecs_manager.provision_user_container.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_stands_down_while_a_conversion_is_draining(
        self, async_client, mock_ecs_manager, mock_container_repo, mock_call_gateway_rpc
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
        mock_ecs_manager.provision_user_container = AsyncMock(return_value="openclaw-new")

        with patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=None)):
            response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 200
        assert response.json()["action"] == "already_recovering"
        mock_ecs_manager.provision_user_container.assert_not_called()
