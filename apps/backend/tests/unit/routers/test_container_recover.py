"""Tests for POST /container/recover endpoint."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import trial_cap


class TestContainerRecover:
    """Test POST /api/v1/container/recover."""

    @pytest.fixture
    def mock_container_repo(self):
        """The row IS the state now: an invoke wakes a parked microVM, so there is no task IP to
        discover and no "running but unreachable" shape to detect up front. Tests set
        ``get_by_owner_id`` to the row under test."""
        with patch("routers.container_recover.container_repo") as mock_repo:
            mock_repo.update_error = AsyncMock(return_value=None)
            mock_repo.update_status = AsyncMock(return_value=None)
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)
            yield mock_repo

    @pytest.fixture
    def mock_call_gateway_rpc(self):
        with patch("routers.container_recover._call_gateway_rpc", new_callable=AsyncMock) as mock_rpc:
            # An EXPLICITLY unhealthy gateway. Was a bare ``{}``, which only read as unhealthy
            # because the route used to do ``health.get("ok")`` — a payload carrying neither
            # ``ok`` nor ``ready`` is now "shape we do not recognise" and refuses (502) rather
            # than escalating a workspace it cannot classify (task-15 item 3).
            mock_rpc.return_value = {"ok": False}
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
        self, async_client, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """Stopped container should trigger full re-provision."""
        mock_container_repo.get_by_owner_id = AsyncMock(
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
        self, async_client, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """Error container should trigger full re-provision."""
        mock_container_repo.get_by_owner_id = AsyncMock(
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

    # --- GATEWAY_DOWN: reprovision ---
    #
    # There is no in-place restart step any more. It was an ``update.run`` RPC into the OpenClaw
    # gateway, and the Hermes bridge serves no such method -- so on every migrated workspace it
    # raised and fell straight through to this same escalation. Calling it was one
    # guaranteed-failing invoke (and, against a parked microVM, one cold start) buying a log line.

    @pytest.mark.asyncio
    async def test_recover_gateway_down_escalates_to_reprovision(
        self, async_client, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """If gateway restart fails, escalate to reprovision."""
        container = {
            "owner_id": "user_test_123",
            "gateway_token": "test-token",
            "status": "running",
        }
        mock_container_repo.get_by_owner_id = AsyncMock(return_value=container)
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
        self, async_client, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """The row must be flipped to "error" before the dispatch, or the dispatch's claim (which
        admits only missing/error/stopped/stale rows) stands down against the running status and
        /recover answers "reprovision" while starting nothing."""
        container = {
            "owner_id": "user_test_123",
            "gateway_token": "test-token",
            "status": "running",
        }
        mock_container_repo.get_by_owner_id = AsyncMock(return_value=container)

        response = await async_client.post("/api/v1/container/recover")
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "reprovision"
        mock_container_repo.update_status.assert_called_once_with("org_test_456", "error")
        mock_dispatch_provision.assert_called_once_with("org_test_456")

    # --- HEALTHY: no-op ---

    @pytest.mark.asyncio
    async def test_recover_healthy_returns_none(
        self, async_client, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """Healthy system should return action=none."""
        container = {
            "owner_id": "user_test_123",
            "gateway_token": "test-token",
            "status": "running",
        }
        mock_container_repo.get_by_owner_id = AsyncMock(return_value=container)
        mock_call_gateway_rpc.return_value = {"ok": True}  # health check passes

        response = await async_client.post("/api/v1/container/recover")
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "none"
        assert data["state"] == "HEALTHY"
        mock_dispatch_provision.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_reads_the_hermes_bridge_ready_payload_as_healthy(
        self, async_client, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """Task-14 row 6 / task-15 item 3: the bridge answers ``ready``, not ``ok``. Reading only
        ``ok`` made every HEALTHY AgentCore workspace fall through to ``update.run`` (absent on
        the bridge), get ``status="error"`` written on its row and a full reprovision dispatched
        — while still answering 200."""
        container = {"owner_id": "user_test_123", "gateway_token": "test-token", "status": "running"}
        mock_container_repo.get_by_owner_id = AsyncMock(return_value=container)
        mock_call_gateway_rpc.return_value = {"ready": True, "runtime": "hermes", "uptimeS": 12, "busy": False}

        response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 200
        assert response.json()["action"] == "none"
        mock_dispatch_provision.assert_not_called()
        mock_container_repo.update_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_refuses_an_unrecognised_health_payload(
        self, async_client, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """A runtime that ANSWERED, in a shape carrying neither ``ok`` nor ``ready``, must not be
        assumed broken: "assume the worst" is what turned this door into a landmine. Refuse
        loudly and write nothing."""
        container = {"owner_id": "user_test_123", "gateway_token": "test-token", "status": "running"}
        mock_container_repo.get_by_owner_id = AsyncMock(return_value=container)
        mock_call_gateway_rpc.return_value = {"runtime": "something-new"}

        response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 502
        mock_dispatch_provision.assert_not_called()
        mock_container_repo.update_status.assert_not_called()

    # --- No container ---

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("role", "gate", "status"),
        [("org:admin", None, 200), ("org:admin", "blocked", 402), ("org:member", None, 404)],
    )
    async def test_recover_with_no_row_starts_the_first_provision_for_an_admin(
        self, mock_container_repo, mock_dispatch_provision, role, gate, status
    ):
        """No row at all: its organization.created provision never started and nothing
        re-drives it, so an admin's Retry starts it behind the provision gate. A member
        rides the admin's workspace and gets the 404."""
        from fastapi import HTTPException
        from unittest.mock import MagicMock

        from core.auth import AuthContext
        from routers.container_recover import container_recover

        auth = AuthContext(user_id="user_a", org_id="org_a", org_role=role, email="a@example.com")
        blocked = MagicMock(to_payload=lambda: {"blocked": {"code": "subscription_required"}}) if gate else None
        with (
            patch("routers.container_recover.ensure_workspace_trial_default", new=AsyncMock()) as trial,
            patch("core.services.provision_gate.evaluate_provision_gate", new=AsyncMock(return_value=blocked)),
        ):
            if status == 200:
                result = await container_recover(auth)
                assert result["action"] == "reprovision"
                trial.assert_awaited_once_with(auth, "org_a")
            else:
                with pytest.raises(HTTPException) as exc:
                    await container_recover(auth)
                assert exc.value.status_code == status
        assert mock_dispatch_provision.await_count == (1 if status == 200 else 0)

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
        self, async_client, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """The free-tier cap is what scaled this container to zero. /recover must
        not be the way around it — same 402 limit card as the other two doors."""
        mock_container_repo.get_by_owner_id = AsyncMock(
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
        self, async_client, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """The OTHER half of the trial predicate. This door used to check only the
        daily cap, so an owner whose trial window had ENDED but who was under
        today's $3 fell straight through to the executor — whose runner starts
        with force=True and therefore skips start_user_service's own check —
        and woke a container that was out of runway. Fresh Bedrock spend on a
        dead trial."""
        mock_container_repo.get_by_owner_id = AsyncMock(
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
        self, async_client, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """A capped owner's row still reads "running" -- the cap blocks at the gate rather than
        stopping anything. If its runtime is unhealthy, the escalation below reprovisions, and a
        reprovision RE-MINTS the gate bearer: this endpoint would be the one door a capped owner
        could reprovision their way back through the cap.

        The gate used to be reached via the running/no-IP branch (a live task IP that Cloud Map
        could fail to resolve); there is no task IP to miss now, so the check sits on the
        escalation itself."""
        mock_container_repo.get_by_owner_id = AsyncMock(return_value={"owner_id": "user_test_123", "status": "running"})

        with patch(
            "core.services.trial_cap.trial_block_reason",
            new=AsyncMock(return_value=trial_cap.DAILY_CAP),
        ):
            response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 402
        assert response.json()["detail"]["blocked"]["code"] == "trial_daily_cap"
        mock_dispatch_provision.assert_not_called()

    @pytest.mark.asyncio
    async def test_recover_stands_down_on_a_provisioning_row(
        self, async_client, mock_container_repo, mock_call_gateway_rpc, mock_dispatch_provision
    ):
        """recover must not probe a provisioning row's half-booted gateway or write
        status="error" over it (the 2026-08-10 conversion incident: four recover stomps
        during one provision). Nor does it dispatch before any trial gate: the status poll
        re-drives a wedged row."""
        mock_container_repo.get_by_owner_id = AsyncMock(
            return_value={"owner_id": "user_test_123", "status": "provisioning", "gateway_token": "tok"}
        )

        response = await async_client.post("/api/v1/container/recover")

        assert response.status_code == 200
        assert response.json()["action"] == "already_recovering"
        mock_call_gateway_rpc.assert_not_called()
        mock_dispatch_provision.assert_not_called()
        mock_container_repo.update_status.assert_not_called()
        mock_container_repo.update_error.assert_not_called()
