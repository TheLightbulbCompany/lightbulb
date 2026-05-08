"""Tests for container endpoints: GET /status and POST /gateway/restart."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.auth import AuthContext
from routers.container import _resolve_cold_start_phase


def _personal_auth(user_id: str = "user_x", email: str = "u@example.com") -> AuthContext:
    """Personal-context AuthContext (no org)."""
    return AuthContext(
        user_id=user_id,
        org_id=None,
        org_role=None,
        email=email,
    )


def _org_member_auth(
    user_id: str = "user_member",
    org_id: str = "org_1",
    email: str = "m@example.com",
) -> AuthContext:
    """Org-member AuthContext. is_org_admin returns False because role != org:admin."""
    return AuthContext(
        user_id=user_id,
        org_id=org_id,
        org_role="org:member",
        email=email,
    )


def _org_admin_auth(
    user_id: str = "user_admin",
    org_id: str = "org_xyz",
    email: str = "a@example.com",
) -> AuthContext:
    """Org-admin AuthContext. is_org_context + is_org_admin are derived properties."""
    return AuthContext(
        user_id=user_id,
        org_id=org_id,
        org_role="org:admin",
        email=email,
    )


async def _noop_ensure_user_ready(auth):
    """No-op stand-in with the real qualname so capture_task can verify
    we dispatched the right coroutine. Using AsyncMock here would yield
    coro.__qualname__ == 'AsyncMockMixin._execute_mock_call', which
    defeats the contract assertion."""
    return None


class TestColdStartPhaseResolver:
    """Pure unit tests for the (DDB status, pool state) -> phase mapping.

    Drives the frontend stepper. The "starting" phase is the most-visible
    one for users today since it covers the 5+min sidecars.channels wedge.
    """

    @staticmethod
    def _pool(connected: bool):
        return SimpleNamespace(is_user_connected=lambda _uid: connected)

    @pytest.mark.parametrize("status", ["provisioning", "stopped", "error", None, ""])
    def test_anything_not_running_is_provisioning(self, status):
        container = {"status": status} if status is not None else {}
        assert _resolve_cold_start_phase(container, self._pool(False), "u_1") == "provisioning"

    def test_running_without_gateway_handshake_is_starting(self):
        # Covers the gap that takes the longest in practice today —
        # ECS task is up but openclaw is still in plugins.bootstrap or
        # the sidecars.channels wedge.
        container = {"status": "running"}
        assert _resolve_cold_start_phase(container, self._pool(False), "u_1") == "starting"

    def test_running_with_gateway_handshake_is_ready(self):
        container = {"status": "running"}
        assert _resolve_cold_start_phase(container, self._pool(True), "u_1") == "ready"

    def test_none_container_is_provisioning(self):
        # Defensive: status route may briefly hit this if the DDB row is
        # being written concurrently with a poll. Don't crash.
        assert _resolve_cold_start_phase(None, self._pool(False), "u_1") == "provisioning"


class TestContainerStatusPhaseField:
    """Verify the /status response actually carries the phase."""

    @pytest.mark.asyncio
    @patch("routers.container.get_gateway_pool")
    @patch("routers.container.get_ecs_manager")
    async def test_phase_starting_when_running_but_no_pool(self, mock_get_ecs, mock_get_pool, async_client):
        mock_ecs = AsyncMock()
        mock_get_ecs.return_value = mock_ecs
        mock_ecs.resolve_running_container = AsyncMock(
            return_value=(
                {
                    "service_name": "openclaw-abc",
                    "status": "running",
                    "created_at": "2026-01-15T12:00:00+00:00",
                    "updated_at": "2026-01-15T12:00:00+00:00",
                },
                "10.0.1.5",
            )
        )
        mock_get_pool.return_value = SimpleNamespace(is_user_connected=lambda _u: False)

        response = await async_client.get("/api/v1/container/status")
        assert response.status_code == 200
        assert response.json()["phase"] == "starting"

    @pytest.mark.asyncio
    @patch("routers.container.get_gateway_pool")
    @patch("routers.container.get_ecs_manager")
    async def test_phase_ready_when_pool_connected(self, mock_get_ecs, mock_get_pool, async_client):
        mock_ecs = AsyncMock()
        mock_get_ecs.return_value = mock_ecs
        mock_ecs.resolve_running_container = AsyncMock(
            return_value=(
                {
                    "service_name": "openclaw-abc",
                    "status": "running",
                    "created_at": "2026-01-15T12:00:00+00:00",
                    "updated_at": "2026-01-15T12:00:00+00:00",
                },
                "10.0.1.5",
            )
        )
        mock_get_pool.return_value = SimpleNamespace(is_user_connected=lambda _u: True)

        response = await async_client.get("/api/v1/container/status")
        assert response.status_code == 200
        assert response.json()["phase"] == "ready"


class TestContainerStatus:
    """Test GET /api/v1/container/status."""

    @pytest.mark.asyncio
    @patch("routers.container.get_ecs_manager")
    async def test_returns_container_status(self, mock_get_ecs, async_client):
        """Should return container metadata for authenticated user."""
        mock_ecs = AsyncMock()
        mock_get_ecs.return_value = mock_ecs
        mock_ecs.resolve_running_container = AsyncMock(
            return_value=(
                {
                    "owner_id": "user_test_123",
                    "service_name": "openclaw-abc123",
                    "gateway_token": "secret-token-value",
                    "status": "running",
                    "substatus": None,
                    "task_arn": "arn:aws:ecs:us-east-1:123456789:task/test-task",
                    "access_point_id": "fsap-test123",
                    "created_at": "2026-01-15T12:00:00+00:00",
                    "updated_at": "2026-01-15T14:00:00+00:00",
                },
                "10.0.1.5",
            )
        )

        response = await async_client.get("/api/v1/container/status")
        assert response.status_code == 200
        data = response.json()
        assert data["service_name"] == "openclaw-abc123"
        assert data["status"] == "running"
        assert data["substatus"] is None
        assert data["region"] == "us-east-1"
        assert "created_at" in data
        assert "updated_at" in data

    @pytest.mark.asyncio
    @patch("routers.container.get_ecs_manager")
    async def test_excludes_sensitive_fields(self, mock_get_ecs, async_client):
        """Should never expose gateway_token, task_arn, or access_point_id."""
        mock_ecs = AsyncMock()
        mock_get_ecs.return_value = mock_ecs
        mock_ecs.resolve_running_container = AsyncMock(
            return_value=(
                {
                    "owner_id": "user_test_123",
                    "service_name": "openclaw-abc123",
                    "gateway_token": "secret-token-value",
                    "status": "running",
                    "substatus": None,
                    "task_arn": "arn:aws:ecs:us-east-1:123456789:task/test-task",
                    "access_point_id": "fsap-test123",
                    "created_at": "2026-01-15T12:00:00+00:00",
                    "updated_at": "2026-01-15T14:00:00+00:00",
                },
                "10.0.1.5",
            )
        )

        response = await async_client.get("/api/v1/container/status")
        assert response.status_code == 200
        data = response.json()
        assert "gateway_token" not in data
        assert "task_arn" not in data
        assert "access_point_id" not in data
        assert "task_definition_arn" not in data

    @pytest.mark.asyncio
    @patch("routers.container.get_ecs_manager")
    async def test_returns_last_error_fields(self, mock_get_ecs, async_client):
        """Should include last_error and last_error_at when present on the container."""
        mock_ecs = AsyncMock()
        mock_get_ecs.return_value = mock_ecs
        mock_ecs.resolve_running_container = AsyncMock(
            return_value=(
                {
                    "owner_id": "user_test_123",
                    "service_name": "openclaw-abc123",
                    "gateway_token": "secret-token-value",
                    "status": "running",
                    "substatus": None,
                    "task_arn": "arn:aws:ecs:us-east-1:123456789:task/test-task",
                    "access_point_id": "fsap-test123",
                    "created_at": "2026-01-15T12:00:00+00:00",
                    "updated_at": "2026-01-15T14:00:00+00:00",
                    "last_error": "OutOfMemoryError",
                    "last_error_at": "2026-01-15T13:00:00+00:00",
                },
                "10.0.1.5",
            )
        )

        response = await async_client.get("/api/v1/container/status")
        assert response.status_code == 200
        data = response.json()
        assert data["last_error"] == "OutOfMemoryError"
        assert data["last_error_at"] == "2026-01-15T13:00:00+00:00"

    @pytest.mark.asyncio
    @patch("routers.container.get_ecs_manager")
    async def test_returns_null_last_error_when_absent(self, mock_get_ecs, async_client):
        """Should return null for last_error and last_error_at when not set on the container."""
        mock_ecs = AsyncMock()
        mock_get_ecs.return_value = mock_ecs
        mock_ecs.resolve_running_container = AsyncMock(
            return_value=(
                {
                    "owner_id": "user_test_123",
                    "service_name": "openclaw-abc123",
                    "gateway_token": "secret-token-value",
                    "status": "running",
                    "substatus": None,
                    "task_arn": "arn:aws:ecs:us-east-1:123456789:task/test-task",
                    "access_point_id": "fsap-test123",
                    "created_at": "2026-01-15T12:00:00+00:00",
                    "updated_at": "2026-01-15T14:00:00+00:00",
                },
                "10.0.1.5",
            )
        )

        response = await async_client.get("/api/v1/container/status")
        assert response.status_code == 200
        data = response.json()
        assert data["last_error"] is None
        assert data["last_error_at"] is None

    @pytest.mark.asyncio
    @patch("routers.container.asyncio.create_task")
    @patch("routers.container.ensure_user_ready", new_callable=AsyncMock)
    @patch("routers.container.evaluate_provision_gate", new_callable=AsyncMock)
    @patch("routers.container.get_ecs_manager")
    async def test_returns_provisioning_without_container(
        self, mock_get_ecs, mock_gate, mock_ensure, mock_create_task, async_client
    ):
        """No container row + no gate fires (owner) -> dispatch ensure_user_ready
        and return 200 with phase=provisioning. Replaces former 404 contract."""
        mock_ecs = AsyncMock()
        mock_get_ecs.return_value = mock_ecs
        mock_ecs.resolve_running_container = AsyncMock(return_value=(None, None))
        mock_ecs.get_service_status = AsyncMock(return_value=None)
        mock_gate.return_value = None  # no gate fires
        mock_create_task.side_effect = lambda c: c.close() or MagicMock()
        response = await async_client.get("/api/v1/container/status")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "provisioning"
        assert body["phase"] == "provisioning"
        mock_create_task.assert_called_once()


class TestContainerStatusGate:
    """GET /container/status — gate-aware response shape (provision-gate-ui).

    When there's no container row yet, /status should evaluate the same
    provision gate POST /provision uses and return 402 + structured
    `blocked` payload if a gate fires. If no gate fires, /status now
    dispatches ensure_user_ready and returns a synthetic provisioning
    response (Task 8 — was a 404 contract pre-Task-8).
    """

    @pytest.mark.asyncio
    async def test_no_container_with_gate_raises_402_structured(self):
        """No container row + gate fires -> 402 with structured blocked payload."""
        from fastapi import HTTPException

        from core.services.provision_gate import Gate
        from routers.container import container_status

        fake_gate = Gate(
            code="credits_required",
            title="Top up Claude credits to start your container",
            message="Top up some Claude credits to start your Bedrock container.",
            action_label="Top up now",
            action_href="/settings/billing#credits",
            action_admin_only=False,
            owner_role="admin",
        )

        mock_auth = SimpleNamespace(
            user_id="user_x",
            org_id=None,
            is_org_context=False,
            is_org_admin=False,
        )

        fake_ecs = AsyncMock()
        fake_ecs.resolve_running_container = AsyncMock(return_value=(None, None))
        fake_ecs.get_service_status = AsyncMock(return_value=None)

        with (
            patch("routers.container.get_ecs_manager", return_value=fake_ecs),
            patch(
                "routers.container.evaluate_provision_gate",
                new_callable=AsyncMock,
                return_value=fake_gate,
            ),
            patch("routers.container.resolve_owner_id", return_value="user_x"),
        ):
            with pytest.raises(HTTPException) as exc:
                await container_status(auth=mock_auth)

        assert exc.value.status_code == 402
        assert isinstance(exc.value.detail, dict)
        assert exc.value.detail["blocked"]["code"] == "credits_required"
        assert exc.value.detail["blocked"]["action"]["href"] == "/settings/billing#credits"

    @pytest.mark.asyncio
    async def test_no_container_no_gate_dispatches_ensure_user_ready(self):
        """No container row + no gate fires (owner) -> dispatch ensure_user_ready
        and return synthetic provisioning response (was 404 pre-Task-8)."""
        from routers.container import container_status

        captured = []
        fake_ecs = AsyncMock()
        fake_ecs.resolve_running_container = AsyncMock(return_value=(None, None))
        fake_ecs.get_service_status = AsyncMock(return_value=None)

        with (
            patch("routers.container.get_ecs_manager", return_value=fake_ecs),
            patch(
                "routers.container.evaluate_provision_gate",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("routers.container.resolve_owner_id", return_value="user_x"),
            patch("routers.container.ensure_user_ready", new=AsyncMock()),
            patch(
                "routers.container.asyncio.create_task",
                side_effect=lambda c: captured.append(c) or c.close() or c,
            ),
        ):
            result = await container_status(auth=_personal_auth())

        assert result["status"] == "provisioning"
        assert result["phase"] == "provisioning"
        assert result["substatus"] == "ensure_user_ready"
        assert len(captured) == 1

    @pytest.mark.asyncio
    async def test_existing_container_skips_gate(self):
        """Container exists -> gate is NOT consulted; response is unchanged."""
        from routers.container import container_status

        mock_auth = SimpleNamespace(
            user_id="user_x",
            org_id=None,
            is_org_context=False,
            is_org_admin=False,
        )

        fake_ecs = AsyncMock()
        fake_ecs.resolve_running_container = AsyncMock(
            return_value=(
                {
                    "service_name": "openclaw-existing",
                    "status": "running",
                    "substatus": None,
                    "created_at": "2026-01-15T12:00:00+00:00",
                    "updated_at": "2026-01-15T12:00:00+00:00",
                },
                "10.0.1.5",
            )
        )

        with (
            patch("routers.container.get_ecs_manager", return_value=fake_ecs),
            patch(
                "routers.container.evaluate_provision_gate",
                new_callable=AsyncMock,
            ) as mock_gate,
            patch("routers.container.resolve_owner_id", return_value="user_x"),
            patch(
                "routers.container.get_gateway_pool",
                return_value=SimpleNamespace(is_user_connected=lambda _u: True),
            ),
        ):
            result = await container_status(auth=mock_auth)

        # Working container path must never call the gate — billing churn
        # (cancel/resubscribe) shouldn't 402 a user with a live container.
        mock_gate.assert_not_called()
        assert result["service_name"] == "openclaw-existing"
        assert result["status"] == "running"


class TestContainerStatusEnsureUserReadyDispatch:
    """Task 8 — /container/status as the canonical readiness trigger.

    After the Clerk-webhook eager-provisioning paths were stripped (Tasks
    6 + 7), this endpoint is the only path that drives a fresh user from
    "Clerk-authed" to "container provisioning." Owners get a synthetic
    provisioning response; org members get 202 'workspace pending'.
    """

    @pytest.mark.asyncio
    async def test_status_no_container_gate_passes_owner_dispatches_ensure_user_ready(self):
        """Owner role + no container + gate clear -> fire ensure_user_ready,
        return 200 with phase=provisioning (not 404)."""
        from routers.container import container_status

        captured: list[str] = []

        def capture_task(coro):
            # Snapshot qualname BEFORE closing — coro.close() destroys the
            # frame after which __qualname__ inspection is unsafe.
            captured.append(coro.__qualname__)
            coro.close()

        fake_ecs = MagicMock(
            resolve_running_container=AsyncMock(return_value=(None, None)),
            get_service_status=AsyncMock(return_value=None),
        )
        with (
            patch(
                "routers.container.evaluate_provision_gate",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "routers.container.get_ecs_manager",
                return_value=fake_ecs,
            ),
            patch(
                "routers.container.ensure_user_ready",
                new=_noop_ensure_user_ready,
            ),
            patch(
                "routers.container.asyncio.create_task",
                side_effect=capture_task,
            ),
        ):
            result = await container_status(_personal_auth())
        assert result["status"] == "provisioning"
        assert result["phase"] == "provisioning"
        assert captured == ["_noop_ensure_user_ready"], f"expected ensure_user_ready dispatched, got {captured}"

    @pytest.mark.asyncio
    async def test_status_no_container_org_admin_dispatches_ensure_user_ready(self):
        """Org-admin owner role + no container + gate clear → fire ensure_user_ready,
        return 200 with phase=provisioning. Same path as personal-owner; this test
        pins the org-admin variant so a regression in is_org_admin → 202 short-circuit
        is caught."""
        from routers.container import container_status

        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()

        fake_ecs = MagicMock(
            resolve_running_container=AsyncMock(return_value=(None, None)),
            get_service_status=AsyncMock(return_value=None),
        )
        with (
            patch(
                "routers.container.evaluate_provision_gate",
                new=AsyncMock(return_value=None),
            ),
            patch("routers.container.get_ecs_manager", return_value=fake_ecs),
            patch(
                "routers.container.ensure_user_ready",
                new=_noop_ensure_user_ready,
            ),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        ):
            result = await container_status(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
        assert result["status"] == "provisioning"
        assert result["phase"] == "provisioning"
        assert captured == ["_noop_ensure_user_ready"], f"expected ensure_user_ready dispatched, got {captured}"

    @pytest.mark.asyncio
    async def test_status_no_container_org_member_dispatches_ensure_user_ready_returns_202(self):
        """Org member + no owner container + gate clear -> fire ensure_user_ready
        (member role; will add to paperclip if owner's company is active,
        otherwise return gracefully) AND return 202 'team workspace pending'."""
        from fastapi import HTTPException

        from routers.container import container_status

        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()

        fake_ecs = MagicMock(
            resolve_running_container=AsyncMock(return_value=(None, None)),
            get_service_status=AsyncMock(return_value=None),
        )
        with (
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(return_value=None)),
            patch("routers.container.get_ecs_manager", return_value=fake_ecs),
            patch(
                "routers.container.ensure_user_ready",
                new=_noop_ensure_user_ready,
            ),
            patch(
                "routers.container.asyncio.create_task",
                side_effect=capture_task,
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await container_status(_org_member_auth())
        assert exc_info.value.status_code == 202
        assert captured == ["_noop_ensure_user_ready"], f"expected ensure_user_ready dispatched, got {captured}"

    @pytest.mark.asyncio
    async def test_status_no_container_gate_blocks_returns_402_unchanged(self):
        """Existing 402 behavior preserved when gate is up."""
        from fastapi import HTTPException

        from routers.container import container_status

        fake_gate = MagicMock(to_payload=lambda: {"blocked": {"code": "x"}})
        fake_ecs = MagicMock(
            resolve_running_container=AsyncMock(return_value=(None, None)),
            get_service_status=AsyncMock(return_value=None),
        )
        with (
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(return_value=fake_gate)),
            patch("routers.container.get_ecs_manager", return_value=fake_ecs),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await container_status(_personal_auth())
        assert exc_info.value.status_code == 402


class TestContainerStatusOrgMemberDispatchOnExisting:
    """Codex P1 on PR #563.

    For an org member joining an org whose owner already has a container,
    /container/status resolves the owner's existing container row via
    resolve_running_container and skips the missing-container branch
    (which is the canonical paperclip-add trigger). Without an explicit
    dispatch on the existing-container path, the member's
    paperclip-membership-add never fires and /teams 409s indefinitely.
    """

    @pytest.mark.asyncio
    async def test_status_container_exists_org_member_dispatches_ensure_user_ready(self):
        """When the org owner's container exists (200-OK path), an org member
        polling /container/status must STILL dispatch ensure_user_ready so
        their paperclip-membership-add fires."""
        from routers.container import container_status

        fake_container = {
            "service_name": "svc-x",
            "status": "running",
            "substatus": None,
            "created_at": "2026-05-08T00:00:00Z",
            "updated_at": "2026-05-08T00:00:00Z",
            "last_error": None,
            "last_error_at": None,
        }
        fake_ecs = MagicMock(
            resolve_running_container=AsyncMock(return_value=(fake_container, "10.0.0.1")),
        )
        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()

        with (
            patch("routers.container.get_ecs_manager", return_value=fake_ecs),
            patch("routers.container.ensure_user_ready", new=_noop_ensure_user_ready),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
            patch("routers.container._safe_phase", return_value="ready"),
        ):
            result = await container_status(_org_member_auth())

        assert result["status"] == "running"
        assert captured == ["_noop_ensure_user_ready"], (
            f"expected ensure_user_ready dispatched for org member on container-exists path, got {captured}"
        )

    @pytest.mark.asyncio
    async def test_status_container_exists_owner_does_not_dispatch(self):
        """For owners whose container already exists, no dispatch is needed —
        they're already provisioned (paperclip + container created together
        by ensure_user_ready)."""
        from routers.container import container_status

        fake_container = {
            "service_name": "svc-x",
            "status": "running",
            "substatus": None,
            "created_at": "2026-05-08T00:00:00Z",
            "updated_at": "2026-05-08T00:00:00Z",
            "last_error": None,
            "last_error_at": None,
        }
        fake_ecs = MagicMock(
            resolve_running_container=AsyncMock(return_value=(fake_container, "10.0.0.1")),
        )
        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()

        with (
            patch("routers.container.get_ecs_manager", return_value=fake_ecs),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
            patch("routers.container._safe_phase", return_value="ready"),
        ):
            await container_status(_personal_auth())

        assert captured == [], "owner path should not dispatch when container already exists"

    @pytest.mark.asyncio
    async def test_status_container_exists_org_admin_does_not_dispatch(self):
        """Org admin with an existing container must not dispatch either —
        the dispatch is gated on (is_org_context AND NOT is_org_admin).
        Org admins are owners and were already provisioned atomically."""
        from routers.container import container_status

        fake_container = {
            "service_name": "svc-x",
            "status": "running",
            "substatus": None,
            "created_at": "2026-05-08T00:00:00Z",
            "updated_at": "2026-05-08T00:00:00Z",
            "last_error": None,
            "last_error_at": None,
        }
        fake_ecs = MagicMock(
            resolve_running_container=AsyncMock(return_value=(fake_container, "10.0.0.1")),
        )
        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()

        with (
            patch("routers.container.get_ecs_manager", return_value=fake_ecs),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
            patch("routers.container._safe_phase", return_value="ready"),
        ):
            await container_status(_org_admin_auth())

        assert captured == [], "org admin path should not dispatch when container already exists"


class TestGatewayRestart:
    """Test POST /api/v1/container/gateway/restart."""

    @pytest.fixture
    def mock_ecs_manager(self):
        with patch("routers.container_rpc.get_ecs_manager") as mock_getter:
            manager = AsyncMock()
            mock_getter.return_value = manager
            yield manager

    @pytest.fixture
    def mock_call_gateway_rpc(self):
        with patch("routers.container_rpc._call_gateway_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.return_value = {}
            yield mock_rpc

    @pytest.fixture
    def running_container(self, mock_ecs_manager):
        """Set up ECS manager to return a running container with IP."""
        container = {
            "gateway_token": "test-gw-token",
            "owner_id": "user_test_123",
            "service_name": "openclaw-test",
            "status": "running",
        }
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(container, "10.0.1.5"))
        return container

    @pytest.mark.asyncio
    async def test_gateway_restart_success(
        self, async_client, mock_ecs_manager, mock_call_gateway_rpc, running_container
    ):
        """Happy path: should return ok=true and call config.apply RPC."""
        response = await async_client.post("/api/v1/container/gateway/restart")
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        mock_call_gateway_rpc.assert_called_once_with(
            ip="10.0.1.5",
            token="test-gw-token",
            method="config.apply",
            params={},
        )

    @pytest.mark.asyncio
    async def test_gateway_restart_no_container(self, async_client, mock_ecs_manager, mock_call_gateway_rpc):
        """Should return 404 when no container is found."""
        mock_ecs_manager.resolve_running_container = AsyncMock(return_value=(None, None))
        response = await async_client.post("/api/v1/container/gateway/restart")
        assert response.status_code == 404
        assert "No running container" in response.json()["detail"]
        mock_call_gateway_rpc.assert_not_called()

    @pytest.mark.asyncio
    async def test_gateway_restart_gateway_unreachable(
        self, async_client, mock_ecs_manager, mock_call_gateway_rpc, running_container
    ):
        """Should return 502 when gateway refuses connection."""
        mock_call_gateway_rpc.side_effect = ConnectionRefusedError()
        response = await async_client.post("/api/v1/container/gateway/restart")
        assert response.status_code == 502
        assert "Gateway is not responding" in response.json()["detail"]
