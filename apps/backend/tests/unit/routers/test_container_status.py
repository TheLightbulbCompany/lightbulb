"""Tests for container endpoints: GET /status and POST /gateway/restart."""

import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from core.services.stripe_webhook_handlers import CONVERSION_GUARD_SECONDS
from routers.container import _resolve_cold_start_phase


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale_iso() -> str:
    """A conversion stamp older than the guard window — i.e. a dead dispatch."""
    return (datetime.now(timezone.utc) - timedelta(seconds=CONVERSION_GUARD_SECONDS + 60)).isoformat()


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


async def _noop_ensure_user_ready(auth, *, provision_container=True):
    """No-op stand-in with the real qualname so capture_task can verify
    we dispatched the right coroutine. Using AsyncMock here would yield
    coro.__qualname__ == 'AsyncMockMixin._execute_mock_call', which
    defeats the contract assertion. Accepts ``provision_container`` to
    mirror the real ensure_user_ready signature (the running-container
    branch passes provision_container=False)."""
    return None


@pytest.fixture(autouse=True)
def _stub_personal_trial_default():
    """The no-container /status path now auto-defaults a personal owner into a
    trial_local row BEFORE the gate (create-before-gate fix). Stub it by default
    so the gate→response-mapping tests below don't hit real DynamoDB; the
    dedicated regression test patches it explicitly to assert the call and its
    ordering relative to the gate."""
    with patch("routers.container.ensure_personal_trial_default", new=AsyncMock()) as stub:
        yield stub


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
    response.
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

    @pytest.mark.asyncio
    async def test_admin_signup_required_returns_404_for_picker(self):
        """Router mapping: a signup_required gate for an admin owner → 404, so
        the frontend renders the ProviderPicker (the checkout entry point)
        rather than a Subscribe button that loops /onboarding → /chat. A fresh
        personal signup no longer produces signup_required (it gets a
        trial_local default first — see
        test_personal_no_billing_creates_trial_before_gate); this mapping
        governs the code wherever the gate still emits it."""
        from fastapi import HTTPException

        from core.services.provision_gate import Gate
        from routers.container import container_status

        fake_gate = Gate(
            code="signup_required",
            title="Subscribe to start your container",
            message="An active subscription is required to provision a container.",
            action_label="Subscribe",
            action_href="/onboarding",
            action_admin_only=True,
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

        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_personal_no_billing_creates_trial_before_gate(self):
        """Regression (the core Phase-A flow): the no-container path MUST create
        the personal trial_local default BEFORE evaluate_provision_gate. Order
        matters — if the gate runs first it sees no billing row, returns
        signup_required, and /status 404s before the trial is ever created,
        trapping a brand-new personal signup on the ProviderPicker instead of
        provisioning the trial container. With the trial created first, the gate clears and
        /status returns 200 provisioning."""
        from routers.container import container_status

        order: list[str] = []

        async def rec_trial(auth, owner_id):
            order.append("trial")

        async def rec_gate(**kwargs):
            order.append("gate")
            return None  # trial row now exists → gate clears

        fake_ecs = MagicMock(
            resolve_running_container=AsyncMock(return_value=(None, None)),
            get_service_status=AsyncMock(return_value=None),
        )
        with (
            patch("routers.container.ensure_personal_trial_default", new=AsyncMock(side_effect=rec_trial)),
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(side_effect=rec_gate)),
            patch("routers.container.get_ecs_manager", return_value=fake_ecs),
            patch("routers.container.ensure_user_ready", new=_noop_ensure_user_ready),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close()),
        ):
            result = await container_status(_personal_auth())

        assert order == ["trial", "gate"], f"trial must be created before the gate, got {order}"
        assert result["status"] == "provisioning"
        assert result["phase"] == "provisioning"

    @pytest.mark.asyncio
    async def test_org_admin_no_billing_returns_team_billing_required_402(self):
        """Org admin with no billing row: no trial is created (personal-only),
        the gate returns the distinct team_billing_required code, and /status
        402s with the structured payload the Teams-billing paywall keys off —
        NOT the personal signup_required 404."""
        from fastapi import HTTPException

        from core.services.provision_gate import Gate
        from routers.container import container_status

        fake_gate = Gate(
            code="team_billing_required",
            title="Set up Teams billing",
            message="Your team needs an active per-seat subscription to start.",
            action_label="Set up billing",
            action_href="/settings/billing",
            action_admin_only=True,
            owner_role="admin",
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
            patch("routers.container.resolve_owner_id", return_value="org_xyz"),
            # Org owner_id also fires the fire-and-forget seat reconcile
            # (Task 8) before the gate check; stub it so this unit test
            # doesn't spawn a real background task hitting live DDB/Stripe
            # code.
            patch("core.services.billing_service.sync_team_seat_quantity", new_callable=AsyncMock),
        ):
            with pytest.raises(HTTPException) as exc:
                await container_status(auth=_org_admin_auth())

        assert exc.value.status_code == 402
        assert exc.value.detail["blocked"]["code"] == "team_billing_required"

    @pytest.mark.asyncio
    async def test_admin_subscription_required_still_returns_402(self):
        """Admin with an *inactive* existing billing row
        (canceled/unpaid/paused) hits subscription_required, NOT
        signup_required. /status must still 402 with the structured
        action so the frontend routes them to /settings/billing — not
        the picker, which would 409 already_subscribed on click."""
        from fastapi import HTTPException

        from core.services.provision_gate import Gate
        from routers.container import container_status

        fake_gate = Gate(
            code="subscription_required",
            title="Subscription not active",
            message="Reactivate your subscription to start your container.",
            action_label="Manage subscription",
            action_href="/settings/billing",
            action_admin_only=True,
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
        assert exc.value.detail["blocked"]["code"] == "subscription_required"
        assert exc.value.detail["blocked"]["action"]["href"] == "/settings/billing"

    @pytest.mark.asyncio
    async def test_member_signup_required_still_returns_402(self):
        """Non-admin org member with no billing row on the org: gate fires
        signup_required, but /status keeps the 402 so the member sees
        'ask your org admin' instead of a ProviderPicker that would 403
        on click."""
        from fastapi import HTTPException

        from core.services.provision_gate import Gate
        from routers.container import container_status

        fake_gate = Gate(
            code="signup_required",
            title="Subscribe to start your container",
            message="An active subscription is required to provision a container.",
            action_label="Subscribe",
            action_href="/onboarding",
            action_admin_only=True,
            owner_role="member",
        )

        mock_auth = SimpleNamespace(
            user_id="user_x",
            org_id="org_x",
            is_org_context=True,
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
            patch("routers.container.resolve_owner_id", return_value="org_x"),
            # Org owner_id also fires the fire-and-forget seat reconcile
            # (Task 8) before the gate check; stub it so this unit test
            # doesn't spawn a real background task hitting live DDB/Stripe
            # code.
            patch("core.services.billing_service.sync_team_seat_quantity", new_callable=AsyncMock),
        ):
            with pytest.raises(HTTPException) as exc:
                await container_status(auth=mock_auth)

        assert exc.value.status_code == 402
        assert exc.value.detail["blocked"]["code"] == "signup_required"


class TestContainerStatusEnsureUserReadyDispatch:
    """/container/status as the canonical readiness trigger.

    With Clerk-webhook eager-provisioning removed, this endpoint is the
    only path that drives a fresh user from "Clerk-authed" to "container
    provisioning." Owners get a synthetic provisioning response; org
    members get 202 'workspace pending'.
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
        # Org-context owner_id (org_xyz) also triggers the fire-and-forget
        # seat reconcile (Task 8), dispatched before ensure_user_ready.
        assert captured == ["sync_team_seat_quantity", "_noop_ensure_user_ready"], (
            f"expected seat sync + ensure_user_ready dispatched, got {captured}"
        )

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
        # Org-context owner_id also triggers the fire-and-forget seat
        # reconcile (Task 8), dispatched before ensure_user_ready.
        assert captured == ["sync_team_seat_quantity", "_noop_ensure_user_ready"], (
            f"expected seat sync + ensure_user_ready dispatched, got {captured}"
        )

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
    """For an org member joining an org whose owner already has a container,
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
        # Org-context owner_id also triggers the fire-and-forget seat
        # reconcile (Task 8), dispatched before ensure_user_ready.
        assert captured == ["sync_team_seat_quantity", "_noop_ensure_user_ready"], (
            f"expected seat sync + ensure_user_ready dispatched for org member on container-exists path, got {captured}"
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
    async def test_status_container_exists_org_admin_dispatches_ensure_user_ready(self):
        """Org admin with an existing container MUST dispatch ensure_user_ready
        too. The dispatch is now role-blind (is_org_context), not gated on
        NOT is_org_admin. Excluding admins here was the /teams "team workspace
        not provisioned" bug for multi-admin orgs: a co-admin who joins an
        already-provisioned org never got their paperclip-membership-add."""
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
            await container_status(_org_admin_auth())

        # Org-context owner_id also triggers the fire-and-forget seat
        # reconcile (Task 8), dispatched before ensure_user_ready.
        assert captured == ["sync_team_seat_quantity", "_noop_ensure_user_ready"], (
            f"org admin on container-exists path must dispatch seat sync + ensure_user_ready, got {captured}"
        )


class TestGatewayRestart:
    """Test POST /api/v1/container/gateway/restart."""

    @pytest.fixture
    def mock_ecs_manager(self):
        with patch("routers.container_recover.get_ecs_manager") as mock_getter:
            manager = AsyncMock()
            mock_getter.return_value = manager
            yield manager

    @pytest.fixture
    def mock_call_gateway_rpc(self):
        # gateway_restart now lives in container_recover and reaches the gateway
        # through the shared signed one-shot helper.
        with patch("routers.container_recover.gateway_rpc_oneshot", new_callable=AsyncMock) as mock_rpc:
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
        """Happy path: should return ok=true and call config.apply via the
        signed one-shot (config.apply is operator.admin-reserved)."""
        response = await async_client.post("/api/v1/container/gateway/restart")
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        mock_call_gateway_rpc.assert_awaited_once()
        call = mock_call_gateway_rpc.await_args
        assert call.args[0] == "10.0.1.5"  # ip (positional)
        assert call.kwargs["token"] == "test-gw-token"
        assert call.kwargs["method"] == "config.apply"
        assert call.kwargs["params"] == {}
        assert call.kwargs["owner_id"]  # signed handshake is scoped to the owner

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


class TestContainerStatusTrialConversion:
    """GET /container/status vs. the trial→paid conversion (Task 12).

    (a) The conversion's `reprovision_for_user` scales the trial container to
        zero and then BLOCKS in a ~90s drain wait — a single EFS-WAL SQLite
        writer is required, and two live tasks corrupt it. The just-paid owner
        is polling /status every ~3s; without a guard, that poll sees
        status="stopped", passes the gate (they're paid now), and dispatches
        its OWN background_provision with no drain wait. Two provisions race.

    (b) The conversion is one fire-and-forget task. If it dies, Stripe never
        re-fires it (its retry sees old_status != "trial_local"), and the
        paying customer keeps the trial container forever. The status path is
        the only thing that runs again — so it self-heals, rate-limited.
    """

    @staticmethod
    def _ecs(row):
        """ECS manager stub: a stopped/error row resolves through
        get_service_status, a running one through resolve_running_container."""
        running = row.get("status") == "running"
        return MagicMock(
            resolve_running_container=AsyncMock(return_value=((row, "10.0.0.1") if running else (None, None))),
            get_service_status=AsyncMock(return_value=row),
            has_live_service=AsyncMock(return_value=False),
        )

    @staticmethod
    def _patches(row, *, subscription_status="active", captured=None, repo=None):
        """Common wiring: gate clear, not over the cap, phase stubbed, and the
        conversion helpers' repos stubbed. ``repo`` is the fake container_repo
        whose update_fields mutates ``row`` in place, so a re-stamped
        rate-limit anchor is visible to the NEXT poll."""
        from core.services import stripe_webhook_handlers as handlers

        def capture_task(coro):
            if captured is not None:
                captured.append(coro.__qualname__)
            coro.close()
            return MagicMock()

        # The stale-lease recovery branch calls this PURE predicate on the
        # container dict; a truthy MagicMock here would make every row look
        # like a dead provision. Use the real function.
        from core.repositories.container_repo import is_provision_claim_stale as _real_stale

        if repo is not None:
            repo.is_provision_claim_stale = _real_stale

        return (
            patch("routers.container.get_ecs_manager", return_value=TestContainerStatusTrialConversion._ecs(row)),
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(return_value=None)),
            patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=None)),
            patch("routers.container._safe_phase", return_value="provisioning"),
            patch("routers.container.container_repo", new=repo),
            patch.object(handlers, "container_repo", new=repo),
            patch.object(
                handlers,
                "billing_repo",
                new=MagicMock(
                    get_by_owner_id=AsyncMock(return_value={"subscription_status": subscription_status}),
                ),
            ),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        )

    @staticmethod
    def _repo(row):
        """container_repo double whose writes land on ``row``."""

        async def update_fields(owner_id, fields):
            row.update(fields)
            return row

        async def update_status(owner_id, status, substatus=None):
            row.update({"status": status, "substatus": substatus})
            return row

        return MagicMock(
            update_fields=AsyncMock(side_effect=update_fields),
            update_status=AsyncMock(side_effect=update_status),
        )

    @pytest.mark.asyncio
    async def test_converting_row_is_not_auto_retried(self):
        """A row the conversion owns (substatus=converting) must NOT be
        auto-retried — that second background_provision has no drain wait and
        would run a task alongside the one still draining."""
        from routers.container import container_status

        row = {
            "service_name": "svc",
            "status": "stopped",
            "substatus": "converting",
            "conversion_pending": True,
            "conversion_started_at": _now_iso(),
        }
        repo = self._repo(row)
        captured: list[str] = []
        with contextlib.ExitStack() as stack:
            for p in self._patches(row, captured=captured, repo=repo):
                stack.enter_context(p)
            result = await container_status(_personal_auth())

        repo.update_status.assert_not_awaited()
        assert captured == [], f"a converting row must not dispatch a provision, got {captured}"
        assert result["substatus"] == "converting"

    @pytest.mark.asyncio
    async def test_draining_row_with_clobbered_substatus_is_not_auto_retried(self):
        """The actual race. ecs_manager.stop_user_service writes
        update_status(owner, "stopped") — substatus defaults to None — so the
        converting substatus is GONE the moment the drain starts. Only the
        conversion_started_at anchor survives, and it alone must hold the
        auto-retry off for the whole drain."""
        from routers.container import container_status

        row = {
            "service_name": "svc",
            "status": "stopped",
            "substatus": None,  # clobbered by stop_user_service
            "conversion_pending": True,
            "conversion_started_at": _now_iso(),
        }
        repo = self._repo(row)
        captured: list[str] = []
        with contextlib.ExitStack() as stack:
            for p in self._patches(row, captured=captured, repo=repo):
                stack.enter_context(p)
            await container_status(_personal_auth())

        repo.update_status.assert_not_awaited()
        assert captured == [], f"a draining conversion must not dispatch a provision, got {captured}"

    @pytest.mark.asyncio
    async def test_plain_stopped_row_still_auto_retries(self):
        """Regression: an ordinary stopped row (no conversion marker) keeps the
        pre-existing auto-retry behaviour."""
        from routers.container import container_status

        row = {"service_name": "svc", "status": "stopped", "substatus": None}
        repo = self._repo(row)
        captured: list[str] = []
        with contextlib.ExitStack() as stack:
            for p in self._patches(row, captured=captured, repo=repo):
                stack.enter_context(p)
            result = await container_status(_personal_auth())

        # The racy status pre-write is deliberately gone — background_provision's
        # atomic lease claim IS the transition to "provisioning" now. The poll
        # only dispatches and shapes its own response.
        repo.update_status.assert_not_awaited()
        assert captured == ["background_provision"], f"expected the ordinary auto-retry, got {captured}"
        assert result["status"] == "provisioning"
        assert result["substatus"] == "auto_retry"

    @pytest.mark.asyncio
    async def test_stuck_conversion_redispatches_at_most_once_per_guard_window(self):
        """Paid billing + a container still carrying conversion_pending long
        past the guard window == the conversion task died. The status path
        re-dispatches it — ONCE. The poll runs every ~3s; three polls in a row
        must produce exactly one reprovision, or the self-heal is a storm."""
        from routers.container import container_status

        row = {
            "service_name": "svc",
            "status": "running",  # still the trial box: never got stopped
            "substatus": None,
            "conversion_pending": True,
            "conversion_started_at": _stale_iso(),
        }
        repo = self._repo(row)
        captured: list[str] = []
        with contextlib.ExitStack() as stack:
            for p in self._patches(row, captured=captured, repo=repo):
                stack.enter_context(p)
            for _ in range(3):  # ~9s of polling
                await container_status(_personal_auth())

        assert captured == ["run_conversion_reprovision"], (
            f"a stuck conversion must re-dispatch exactly once per guard window, got {captured}"
        )
        assert row["substatus"] == "converting"

    @pytest.mark.asyncio
    async def test_no_self_heal_when_billing_is_not_paid(self):
        """The self-heal fires only when billing SAYS PAID. A canceled owner's
        stale marker is dropped and the row falls back to the ordinary
        auto-retry + gate path."""
        from routers.container import container_status

        row = {
            "service_name": "svc",
            "status": "running",
            "substatus": None,
            "conversion_pending": True,
            "conversion_started_at": _stale_iso(),
        }
        repo = self._repo(row)
        captured: list[str] = []
        with contextlib.ExitStack() as stack:
            for p in self._patches(row, subscription_status="canceled", captured=captured, repo=repo):
                stack.enter_context(p)
            await container_status(_personal_auth())

        assert captured == []
        assert row["conversion_pending"] is False


class TestContainerStatusTrialBedrockRestore:
    """GET /container/status vs. the daily Bedrock restore trigger (Task 7).

    The meter Lambda's IAM revoke does not expire on its own. This is the
    ONLY place a trial owner's stopped-by-cap container gets its Bedrock
    grant back — the owner polling /status IS the trigger, so it must fire
    on the same request that then proceeds to the ordinary auto-retry logic.
    """

    @staticmethod
    def _ecs(row):
        running = row.get("status") == "running"
        return MagicMock(
            resolve_running_container=AsyncMock(return_value=((row, "10.0.0.1") if running else (None, None))),
            get_service_status=AsyncMock(return_value=row),
            has_live_service=AsyncMock(return_value=False),
        )

    @staticmethod
    def _repo(row):
        async def update_status(owner_id, status, substatus=None):
            row.update({"status": status, "substatus": substatus})
            return row

        return MagicMock(update_status=AsyncMock(side_effect=update_status))

    @pytest.mark.asyncio
    async def test_revoked_stopped_row_restores_bedrock_then_auto_retries(self):
        """Revoked + new day (under cap, not expired): restore_owner_bedrock
        fires, bedrock_revoked clears on the row, AND the ordinary auto-retry
        still proceeds in the SAME poll — 'let the normal provisioning path
        bring the container back'."""
        from routers.container import container_status

        row = {"service_name": "svc", "status": "stopped", "substatus": None, "bedrock_revoked": True}
        repo = self._repo(row)
        restore = AsyncMock()
        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()
            return MagicMock()

        with (
            patch("routers.container.get_ecs_manager", return_value=self._ecs(row)),
            patch("routers.container.container_repo", new=repo),
            patch(
                "core.services.provision_gate.billing_repo.get_by_owner_id",
                new=AsyncMock(return_value={"subscription_status": "trial_local", "trial_end": None}),
            ),
            patch("core.services.trial_cap.billing_repo.get_trial_daily_spend", new=AsyncMock(return_value=0)),
            patch("core.billing.bedrock_aip.restore_owner_bedrock", new=restore),
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(return_value=None)),
            patch("routers.container._safe_phase", return_value="provisioning"),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        ):
            result = await container_status(_personal_auth())

        restore.assert_awaited_once_with("user_x")
        assert row["bedrock_revoked"] is False
        assert captured == ["background_provision"], f"expected auto-retry to still fire, got {captured}"
        assert result["status"] == "provisioning"

    @pytest.mark.asyncio
    async def test_not_revoked_row_makes_no_billing_call_for_restore(self):
        """The overwhelmingly common poll: no bedrock_revoked flag at all —
        the restore trigger must not even read the billing row."""
        from routers.container import container_status

        row = {"service_name": "svc", "status": "running", "substatus": None}
        billing_read = AsyncMock()

        with (
            patch("routers.container.get_ecs_manager", return_value=self._ecs(row)),
            patch("core.services.provision_gate.billing_repo.get_by_owner_id", new=billing_read),
            patch("core.billing.bedrock_aip.restore_owner_bedrock", new=AsyncMock()) as restore,
            patch("routers.container._safe_phase", return_value="ready"),
        ):
            result = await container_status(_personal_auth())

        billing_read.assert_not_called()
        restore.assert_not_awaited()
        assert result["status"] == "running"

    @pytest.mark.asyncio
    async def test_restore_failure_does_not_break_the_status_poll(self):
        """A restore that raises must be logged and swallowed — /status still
        answers normally (not a 500), and the ordinary auto-retry proceeds
        regardless (bedrock_revoked simply stays set for the next poll)."""
        from routers.container import container_status

        row = {"service_name": "svc", "status": "stopped", "substatus": None, "bedrock_revoked": True}
        repo = self._repo(row)
        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()
            return MagicMock()

        with (
            patch("routers.container.get_ecs_manager", return_value=self._ecs(row)),
            patch("routers.container.container_repo", new=repo),
            patch(
                "core.services.provision_gate.billing_repo.get_by_owner_id",
                new=AsyncMock(return_value={"subscription_status": "trial_local", "trial_end": None}),
            ),
            patch("core.services.trial_cap.billing_repo.get_trial_daily_spend", new=AsyncMock(return_value=0)),
            patch(
                "core.billing.bedrock_aip.restore_owner_bedrock",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(return_value=None)),
            patch("routers.container._safe_phase", return_value="provisioning"),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        ):
            result = await container_status(_personal_auth())  # must not raise

        assert row["bedrock_revoked"] is True  # restore never landed
        assert result["status"] == "provisioning"  # the poll still answers normally


class TestContainerProvisionStartGuards:
    """POST /container/provision vs. the start chokepoint (Task 13).

    /provision restarts a `stopped` container directly — the branch deliberately
    skips the payment gate ("billing churns; don't 402 someone asking about a
    container they already have"), which meant it ALSO skipped the free-tier cap
    and the conversion guard that GET /container/status applies. Two doors, one
    guarded. Both checks now live in ``ecs_manager.start_user_service``; these
    tests pin how this router surfaces each refusal.
    """

    @staticmethod
    def _ecs(*, refusal: Exception | None = None):
        return MagicMock(
            start_user_service=AsyncMock(side_effect=refusal),
        )

    @staticmethod
    def _stopped_row():
        return {"status": "stopped", "service_name": "openclaw-foo"}

    @pytest.mark.asyncio
    async def test_over_cap_owner_gets_the_same_402_limit_card_as_the_status_path(self):
        """The whole point: a capped-out trial owner who calls /provision
        directly gets the identical limit card GET /status returns — not a
        woken container."""
        from core.containers.ecs_manager import START_REFUSED_CAP, ContainerStartRefused
        from core.services.provision_gate import trial_daily_cap_gate
        from routers.container import container_provision

        ecs = self._ecs(refusal=ContainerStartRefused(START_REFUSED_CAP, "user_x", "over cap"))
        with (
            patch(
                "routers.container.container_repo.get_by_owner_id",
                new=AsyncMock(return_value=self._stopped_row()),
            ),
            patch("routers.container.get_ecs_manager", return_value=ecs),
        ):
            with pytest.raises(HTTPException) as exc:
                await container_provision(_personal_auth())

        assert exc.value.status_code == 402
        assert exc.value.detail == trial_daily_cap_gate("admin").to_payload()
        assert exc.value.detail["blocked"]["code"] == "trial_daily_cap"

    @pytest.mark.asyncio
    async def test_conversion_in_flight_returns_a_pollable_status_and_starts_nothing(self):
        """A conversion reprovision is mid-drain. /provision must not dispatch a
        second one (the EFS-WAL double-writer), and must not hand the caller an
        error either — the container IS coming back. Return the pollable
        provisioning shape the caller already understands."""
        from core.containers.ecs_manager import START_REFUSED_CONVERTING, ContainerStartRefused
        from routers.container import container_provision

        ecs = self._ecs(refusal=ContainerStartRefused(START_REFUSED_CONVERTING, "user_x", "converting"))
        with (
            patch(
                "routers.container.container_repo.get_by_owner_id",
                new=AsyncMock(return_value=self._stopped_row()),
            ),
            patch("routers.container.get_ecs_manager", return_value=ecs),
        ):
            result = await container_provision(_personal_auth())

        assert result["status"] == "provisioning"
        assert result["substatus"] == "converting"
        assert result["already_existed"] is True

    @pytest.mark.asyncio
    async def test_unrefused_start_still_restarts_the_container(self):
        """Regression: the ordinary cold start (paid owner, under-cap trial,
        no conversion) still wakes the container."""
        from routers.container import container_provision

        ecs = self._ecs()
        with (
            patch(
                "routers.container.container_repo.get_by_owner_id",
                new=AsyncMock(return_value=self._stopped_row()),
            ),
            patch("routers.container.get_ecs_manager", return_value=ecs),
        ):
            result = await container_provision(_personal_auth())

        ecs.start_user_service.assert_awaited_once_with("user_x")
        assert result["status"] == "provisioning"
