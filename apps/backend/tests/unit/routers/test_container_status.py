"""Tests for container endpoints: GET /status."""

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from core.auth import AuthContext


@pytest.fixture
def mock_auth_context(mock_org_admin_context):
    """Container requests default to an active organization workspace."""
    return mock_org_admin_context


@pytest.fixture(autouse=True)
def _close_request_background_tasks(request):
    create_task = asyncio.create_task

    def dispatch(coro):
        if "async_client" in request.fixturenames:
            coro.close()
            return MagicMock()
        return create_task(coro)

    with patch("routers.container.asyncio.create_task", side_effect=dispatch):
        yield


def _workspace_auth(user_id: str = "user_x", email: str = "u@example.com") -> AuthContext:
    """Organization-admin AuthContext for supported workspace requests."""
    return AuthContext(
        user_id=user_id,
        org_id="org_x",
        org_role="org:admin",
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


@pytest.fixture(autouse=True)
def _stub_workspace_trial_default():
    """The no-container /status path now auto-defaults a workspace into a
    trial_local row BEFORE the gate (create-before-gate fix). Stub it by default
    so the gate→response-mapping tests below don't hit real DynamoDB; the
    dedicated regression test patches it explicitly to assert the call and its
    ordering relative to the gate."""
    with patch("routers.container.ensure_workspace_trial_default", new=AsyncMock()) as stub:
        yield stub


def _onboarding_state_stub(status: str | None = None) -> dict:
    """The all-defaults `get_onboarding_state` return shape, with `teams_state`
    overridable -- mirrors readiness_state.compute_status's ONE paperclip-
    companies read (core.repositories.workspace_repo.WorkspaceRepo.
    get_onboarding_state). Every WorkspaceRepo test double in this file must
    configure BOTH `.get` (legacy callers) and `.get_onboarding_state`
    (compute_status's own accessor) -- patching the whole class replaces the
    instance with a bare MagicMock, so `.get_onboarding_state` is never the
    real method unless a test explicitly stubs it here."""
    return {
        "onboarding_generation": None,
        "funnel_state": "not_started",
        "funnel_submitted_at": None,
        "funnel_completed_at": None,
        "onboarding_goal_id": None,
        "onboarding_project_id": None,
        "onboarding_brief_text": None,
        "onboarding_submitted_by": None,
        "bootstrap_state": "not_started",
        "bootstrap_dispatch_id": None,
        "bootstrap_dispatched_at": None,
        "bootstrap_delivered_at": None,
        "bootstrap_attempts": 0,
        "bootstrap_last_error": None,
        "teams_state": status,
        "company_id": None,
        "paperclip_user_id": None,
        "user_id": None,
    }


def _paperclip_repo_stub(status: str | None = None) -> MagicMock:
    """A WorkspaceRepo test double with BOTH `.get` (legacy direct readers)
    and `.get_onboarding_state` (compute_status) configured consistently
    off the same `status`."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=SimpleNamespace(status=status) if status is not None else None)
    repo.get_onboarding_state = AsyncMock(return_value=_onboarding_state_stub(status))
    # resolve_owner_row is the canonical owner-row resolver compute_status
    # (via get_onboarding_state) delegates to. Kept on the stub so any test
    # that patches WorkspaceRepo directly gets a coherent double even though
    # the router itself no longer calls it (it goes through get_onboarding_state).
    repo.resolve_owner_row = AsyncMock(
        return_value=SimpleNamespace(status=status, funnel_state=None) if status is not None else None
    )
    return repo


@pytest.fixture(autouse=True)
def _stub_teams_status_repo():
    """The status path now reads the polling user's paperclip-companies seed
    state (via readiness_state.compute_status) to surface a NON-BLOCKING
    teams_status AND the company-scoped `onboarded` fact (funnel_state ==
    "complete"), both composed inside compute_status/get_onboarding_state.
    Stub WorkspaceRepo to an empty read by default so this file's tests
    exercise the real composition without hitting DynamoDB; the tests that
    care re-patch WorkspaceRepo inline (an active/failed row, a
    funnel_state=="complete" row, or a raising read)."""
    repo = _paperclip_repo_stub(None)
    with patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=repo):
        yield repo


@pytest.mark.asyncio
async def test_ready_paid_workspace_does_not_try_to_arm_a_trial():
    import boto3
    from moto import mock_aws
    from core.repositories import billing_repo
    from routers.container import container_status

    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="paid-workspace",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.put_item(
            Item={
                "owner_id": "org_x",
                "owner_type": "org",
                "subscription_status": "active",
                "stripe_subscription_id": "sub_paid",
                "provider_choice": "chatgpt_oauth",
            }
        )
        row = {"owner_id": "org_x", "status": "running"}
        _row = row
        with (
            patch.object(billing_repo, "_get_table", return_value=table),
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch("core.services.container_rpc.call", AsyncMock(return_value={"ok": True})),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close()),
        ):
            result = await container_status(_workspace_auth())

        assert result["runtime_phase"] == "ready"
        assert result["provider"]["subscription_status"] == "active"


class TestContainerStatusPhaseField:
    """Verify the /status response actually carries the phase.

    NOTE: `test_phase_starting_when_running_but_no_pool` and
    `test_phase_ready_when_pool_connected` were deleted. Both drove a running container row
    through the gateway health-probe branch of `resolve_runtime_phase`/`compute_status` --
    unreachable now that `runtime_transport.host_for` is pinned to "agentcore" fleet-wide:
    a running row is reported "ready" unconditionally, with `container_rpc.call` never
    awaited at all (see the "Never probe a running AgentCore row" comment in
    `core/services/readiness_state.py`). That composed behaviour has its own direct unit
    coverage in tests/unit/services/test_readiness_state.py and
    test_readiness_state_agentcore_parked.py; this router-level duplicate would just assert
    dead code.
    """


class TestContainerStatus:
    """Test GET /api/v1/container/status."""

    @pytest.mark.asyncio
    @patch("routers.container.container_repo.get_by_owner_id")
    async def test_returns_container_status(self, mock_get_row, async_client):
        """Should return container metadata for authenticated user."""
        mock_get_row.return_value = {
            "owner_id": "user_test_123",
            "gateway_token": "secret-token-value",
            "status": "running",
            "substatus": None,
            "task_arn": "arn:aws:ecs:us-east-1:123456789:task/test-task",
            "access_point_id": "fsap-test123",
            "created_at": "2026-01-15T12:00:00+00:00",
            "updated_at": "2026-01-15T14:00:00+00:00",
        }

        response = await async_client.get("/api/v1/container/status")
        assert response.status_code == 200
        data = response.json()
        assert data["container_status"] == "running"
        assert data["container_substatus"] is None
        assert data["region"] == "us-east-1"
        assert "created_at" in data
        assert "updated_at" in data

    @pytest.mark.asyncio
    @patch("routers.container.container_repo.get_by_owner_id")
    async def test_excludes_sensitive_fields(self, mock_get_row, async_client):
        """Should never expose gateway_token, task_arn, or access_point_id."""
        mock_get_row.return_value = {
            "owner_id": "user_test_123",
            "gateway_token": "secret-token-value",
            "status": "running",
            "substatus": None,
            "task_arn": "arn:aws:ecs:us-east-1:123456789:task/test-task",
            "access_point_id": "fsap-test123",
            "created_at": "2026-01-15T12:00:00+00:00",
            "updated_at": "2026-01-15T14:00:00+00:00",
        }

        response = await async_client.get("/api/v1/container/status")
        assert response.status_code == 200
        data = response.json()
        assert "gateway_token" not in data
        assert "task_arn" not in data
        assert "access_point_id" not in data
        assert "task_definition_arn" not in data

    @pytest.mark.asyncio
    @patch("routers.container.container_repo.get_by_owner_id")
    async def test_a_running_row_hides_a_leftover_last_error(self, mock_get_row, async_client):
        """A stale last_error from an earlier failed attempt must not show on a live workspace."""
        mock_get_row.return_value = {
            "owner_id": "user_test_123",
            "status": "running",
            "substatus": None,
            "last_error": "AgentCore runtime failed to start",
            "last_error_at": "2026-01-15T13:00:00+00:00",
        }

        response = await async_client.get("/api/v1/container/status")
        assert response.status_code == 200
        data = response.json()
        assert data["last_error"] is None
        assert data["last_error_at"] is None

    @pytest.mark.asyncio
    @patch("routers.container.container_repo.get_by_owner_id")
    async def test_returns_null_last_error_when_absent(self, mock_get_row, async_client):
        """Should return null for last_error and last_error_at when not set on the container."""
        mock_get_row.return_value = {
            "owner_id": "user_test_123",
            "gateway_token": "secret-token-value",
            "status": "running",
            "substatus": None,
            "task_arn": "arn:aws:ecs:us-east-1:123456789:task/test-task",
            "access_point_id": "fsap-test123",
            "created_at": "2026-01-15T12:00:00+00:00",
            "updated_at": "2026-01-15T14:00:00+00:00",
        }

        response = await async_client.get("/api/v1/container/status")
        assert response.status_code == 200
        data = response.json()
        assert data["last_error"] is None
        assert data["last_error_at"] is None

    @pytest.mark.asyncio
    @patch("routers.container.asyncio.create_task")
    @patch("routers.container.evaluate_provision_gate", new_callable=AsyncMock)
    @patch("routers.container.container_repo.get_by_owner_id")
    async def test_returns_provisioning_without_container(
        self, mock_get_row, mock_gate, mock_create_task, async_client
    ):
        """No container row + no gate fires (owner) -> 200 with phase=provisioning, and
        no background dispatch (the status read triggers nothing)."""
        mock_get_row.return_value = None
        mock_gate.return_value = None  # no gate fires
        response = await async_client.get("/api/v1/container/status")
        assert response.status_code == 200
        body = response.json()
        assert body["container_status"] is None
        assert body["runtime_phase"] == "provisioning"
        mock_create_task.assert_not_called()


class TestContainerStatusGate:
    """GET /container/status — gate-aware response shape (provision-gate-ui).

    When there's no container row yet, /status should evaluate the same
    provision gate POST /provision uses and return 402 + structured
    `blocked` payload if a gate fires. If no gate fires, it answers the
    composed contract for "no row" -- and dispatches nothing.
    """

    @pytest.mark.asyncio
    async def test_no_container_no_gate_is_a_cold_read(self):
        """No row + gate clear: the composed contract (runtime_phase provisioning, no
        container status) -- no dispatch, no background task. The organization.created
        webhook owns the first provision."""
        from routers.container import container_status

        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=None)),
            patch("routers.container.evaluate_provision_gate", new_callable=AsyncMock, return_value=None),
            patch("routers.container.dispatch_provision", new=AsyncMock()) as dispatch,
            patch("routers.container.asyncio.create_task") as create_task,
        ):
            result = await container_status(auth=_workspace_auth())

        assert result["runtime_phase"] == "provisioning"
        assert result["container_status"] is None
        assert result["container_substatus"] is None
        dispatch.assert_not_awaited()
        create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_first_admin_read_that_beats_the_webhook_sees_the_trial_not_the_paywall(self):
        """The GET can arrive before organization.created has written the trial row. It creates
        it (create-if-absent) BEFORE the gate reads billing, so the gate is clear and the admin
        gets the no-row contract instead of team_billing_required."""
        from core.repositories import billing_repo
        from core.services import provision_gate
        from routers.container import container_status

        rows: dict = {}

        async def create(*, owner_id, owner_type, trial_end):
            rows[owner_id] = {"owner_id": owner_id, "owner_type": owner_type, "subscription_status": "trial_local"}
            return True

        async def gate(**kwargs):
            return await provision_gate.evaluate_provision_gate(**kwargs)

        from core.services.user_provision import ensure_workspace_trial_default as real_default

        with (
            patch("routers.container.ensure_workspace_trial_default", new=real_default),
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=None)),
            patch.object(billing_repo, "get_by_owner_id", AsyncMock(side_effect=lambda o, **k: rows.get(o))),
            patch.object(billing_repo, "create_trial_local_if_absent", AsyncMock(side_effect=create)),
            patch("routers.container.evaluate_provision_gate", new=gate),
            patch("routers.container.dispatch_provision", new=AsyncMock()) as dispatch,
        ):
            result = await container_status(auth=_org_admin_auth())

        assert rows["org_xyz"]["subscription_status"] == "trial_local"
        assert result["runtime_phase"] == "provisioning"
        assert result["container_status"] is None
        dispatch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_container_org_member_gets_202_and_dispatches_nothing(self):
        from routers.container import container_status

        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=None)),
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(return_value=None)),
            patch("routers.container.asyncio.create_task") as create_task,
            pytest.raises(HTTPException) as exc,
        ):
            await container_status(_org_member_auth())
        assert exc.value.status_code == 202
        create_task.assert_not_called()

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

        mock_auth = _workspace_auth()

        _row = None

        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "routers.container.evaluate_provision_gate",
                new_callable=AsyncMock,
                return_value=fake_gate,
            ),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close()),
        ):
            with pytest.raises(HTTPException) as exc:
                await container_status(auth=mock_auth)

        assert exc.value.status_code == 402
        assert isinstance(exc.value.detail, dict)
        assert exc.value.detail["blocked"]["code"] == "credits_required"
        assert exc.value.detail["blocked"]["action"]["href"] == "/settings/billing#credits"

    @pytest.mark.asyncio
    async def test_no_container_preserves_onboarded_during_recreation(self):
        """Container-only recreation (no container row yet, but the company +
        G2 onboarding preserved) must NOT re-show a completed funnel: the
        synthetic provisioning response resolves onboarded from the preserved
        onboarding state via compute_status, not a hardcoded False. Pins the
        #1416 P2 fix folded into COMPOSER."""
        from routers.container import container_status

        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        repo.get_onboarding_state = AsyncMock(
            return_value=_onboarding_state_with(funnel_state="complete", onboarding_generation="onb_g")
        )
        _row = None

        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch("routers.container.evaluate_provision_gate", new_callable=AsyncMock, return_value=None),
            patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=repo),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close() or c),
        ):
            result = await container_status(auth=_workspace_auth())

        assert result["container_status"] is None
        assert result["onboarded"] is True  # NOT re-funneled

    @pytest.mark.asyncio
    async def test_existing_container_skips_gate(self):
        """Container exists -> gate is NOT consulted; response is unchanged."""
        from routers.container import container_status

        mock_auth = _workspace_auth()

        _row = {
            "status": "running",
            "substatus": None,
            "created_at": "2026-01-15T12:00:00+00:00",
            "updated_at": "2026-01-15T12:00:00+00:00",
        }

        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "routers.container.evaluate_provision_gate",
                new_callable=AsyncMock,
            ) as mock_gate,
            patch(
                "core.services.container_rpc.call",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close()),
        ):
            result = await container_status(auth=mock_auth)

        # Working container path must never call the gate — billing churn
        # (cancel/resubscribe) shouldn't 402 a user with a live container.
        mock_gate.assert_not_called()
        assert result["container_status"] == "running"

    @pytest.mark.asyncio
    async def test_personal_context_requires_workspace_before_provisioning(self):
        from routers.container import container_status

        with pytest.raises(HTTPException) as exc_info:
            await container_status(AuthContext(user_id="user_x"))

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["code"] == "workspace_required"

    @pytest.mark.asyncio
    async def test_org_admin_no_billing_returns_team_billing_required_402(self):
        """Org admin with no billing row: the gate returns the distinct
        team_billing_required code, and /status 402s with the structured
        payload the workspace-billing paywall keys off."""
        from fastapi import HTTPException

        from core.services.provision_gate import Gate
        from routers.container import container_status

        fake_gate = Gate(
            code="team_billing_required",
            title="Set up workspace billing",
            message="Your workspace needs an active subscription to start.",
            action_label="Set up billing",
            action_href="/settings/billing",
            action_admin_only=True,
            owner_role="admin",
        )
        _row = None
        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "routers.container.evaluate_provision_gate",
                new_callable=AsyncMock,
                return_value=fake_gate,
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await container_status(auth=_org_admin_auth())

        assert exc.value.status_code == 402
        assert exc.value.detail["blocked"]["code"] == "team_billing_required"

    @pytest.mark.asyncio
    async def test_admin_subscription_required_still_returns_402(self):
        """Admin with an *inactive* existing billing row
        (canceled/unpaid/paused) hits subscription_required. /status must
        402 with the structured
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

        mock_auth = _workspace_auth()

        _row = None

        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "routers.container.evaluate_provision_gate",
                new_callable=AsyncMock,
                return_value=fake_gate,
            ),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close()),
        ):
            with pytest.raises(HTTPException) as exc:
                await container_status(auth=mock_auth)

        assert exc.value.status_code == 402
        assert exc.value.detail["blocked"]["code"] == "subscription_required"
        assert exc.value.detail["blocked"]["action"]["href"] == "/settings/billing"


class TestContainerStatusColdRead:
    """GET /container/status is a cold read: it reports a failed or stopped row as it is and
    re-drives nothing (no restore, no restart). Its one write is an admin's missing trial row."""

    @staticmethod
    def _patches(row):
        return (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=row)),
            patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=None)),
            patch("routers.container.dispatch_provision", new=AsyncMock()),
            patch("routers.container.asyncio.create_task"),
            patch("routers.container.ensure_workspace_trial_default", new=AsyncMock()),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "row",
        [
            {"status": "error", "substatus": "provision_failed", "last_error": "boom", "retryable": True},
            {"status": "error", "substatus": "provision_failed", "last_error": "boom", "retryable": False},
            {"status": "error", "substatus": None, "last_error": None},
            {"status": "stopped", "substatus": None},
        ],
    )
    async def test_a_failed_or_stopped_row_is_reported_as_is_and_never_redispatched(self, row):
        from routers.container import container_status

        with contextlib.ExitStack() as stack:
            dispatch, create_task, trial_default = [stack.enter_context(p) for p in self._patches(row)][2:]
            result = await container_status(_workspace_auth())

        assert result["container_status"] == row["status"]
        assert result["container_substatus"] == row["substatus"]
        assert result["last_error"] == (row.get("last_error") if row["status"] == "error" else None)
        dispatch.assert_not_awaited()
        create_task.assert_not_called()
        trial_default.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_capped_trial_row_still_gets_the_limit_card(self):
        from core.services import trial_cap
        from routers.container import container_status

        row = {"status": "stopped", "substatus": "trial_daily_cap"}
        with contextlib.ExitStack() as stack:
            for p in self._patches(row):
                stack.enter_context(p)
            stack.enter_context(
                patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=trial_cap.DAILY_CAP))
            )
            with pytest.raises(HTTPException) as exc:
                await container_status(_workspace_auth())
        assert exc.value.status_code == 402
        assert exc.value.detail["blocked"]["code"] == "trial_daily_cap"

    @pytest.mark.asyncio
    async def test_a_ready_trial_read_never_arms_the_trial_clock(self):
        """Arming moved to the provision executor (MarkRunning); a status read writes nothing."""
        from core.repositories import billing_repo
        from routers.container import container_status

        with (
            patch(
                "routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value={"status": "running"})
            ),
            patch.object(
                billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "trial_local"})
            ),
            patch.object(billing_repo, "arm_trial_if_unset", AsyncMock()) as arm,
        ):
            result = await container_status(_workspace_auth())
        assert result["runtime_phase"] == "ready"
        arm.assert_not_awaited()


class TestContainerStatusTeamsStatus:
    """GET /container/status surfaces a failed Paperclip seed as a NON-BLOCKING
    teams_status (Part C). A running container with a failed company seed still
    serves chat — phase stays "ready" — while teams_status="failed" tells the
    client to show a "Teams needs attention / retry" affordance. The paperclip
    read is best-effort: any failure omits the field, never breaks /status.

    The pure "does a Paperclip row's status become the composed contract's
    teams_state" logic moved to readiness_state.compute_status (spec PR-5,
    COMPOSER) — the direct _resolve_teams_status unit tests below were
    ported to test_readiness_state.py's TestTeamsState. This class keeps
    only the end-to-end /container/status wire-contract assertions.
    """

    @pytest.mark.asyncio
    async def test_running_container_with_failed_seed_is_ready_plus_teams_failed(self):
        """The core Part C case: a running container whose company seed failed
        reports phase=ready (chat works) AND teams_status=failed (degraded)."""
        from routers.container import container_status

        _row = {"status": "running", "substatus": None}
        failed_repo = _paperclip_repo_stub("failed")
        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "core.services.container_rpc.call",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=failed_repo),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close()),
        ):
            result = await container_status(_workspace_auth())

        assert result["runtime_phase"] == "ready", "a failed seed must NOT block chat"
        assert result["teams_state"] == "failed"

    @pytest.mark.asyncio
    async def test_running_container_with_healthy_seed_reports_teams_state_active(self):
        """teams_state carries the raw underlying value always -- no more
        conditional field presence now that the legacy additive teams_status
        flat duplicate is gone (spec PR-9, CLIENT-HOOK)."""
        from routers.container import container_status

        _row = {"status": "running", "substatus": None}
        active_repo = _paperclip_repo_stub("active")
        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "core.services.container_rpc.call",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=active_repo),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close()),
        ):
            result = await container_status(_workspace_auth())

        assert result["runtime_phase"] == "ready"
        assert result["teams_state"] == "active"

    @pytest.mark.asyncio
    async def test_ready_poll_reads_paperclip_companies_exactly_once(self):
        """The hot ready-poll path reads paperclip-companies only once."""
        from routers.container import container_status

        _row = {"status": "running", "substatus": None}
        onboarding_state = _onboarding_state_stub("active")
        onboarding_state["onboarding_generation"] = "01G"
        onboarding_state["funnel_state"] = "submitted"
        onboarding_state["user_id"] = "creator_1"
        onboarding_state["onboarding_submitted_by"] = "member_1"
        repo = MagicMock()
        repo.get = AsyncMock(return_value=SimpleNamespace(status="active"))
        repo.get_onboarding_state = AsyncMock(return_value=onboarding_state)
        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "core.services.container_rpc.call",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=repo),
        ):
            result = await container_status(_workspace_auth())

        # CLIENT-HOOK removed the flat `phase`; the contract's runtime_phase
        # carries it now.
        assert result["runtime_phase"] == "ready"
        repo.get_onboarding_state.assert_awaited_once_with("org_x")


def _onboarding_state_with(
    *, funnel_state: str, onboarding_generation: str | None, teams_state: str | None = None
) -> dict:
    """Return an onboarding row with the requested funnel fields."""
    state = _onboarding_state_stub(teams_state)
    state["funnel_state"] = funnel_state
    state["onboarding_generation"] = onboarding_generation
    return state


_RUNNING_ROW = {"status": "running", "substatus": None}


class TestContainerStatusOnboarded:
    """GET /container/status surfaces `onboarded` from the paperclip-companies
    OWNER row's funnel_state (spec PR-4 / ONBOARD-TABLE — supersedes the old
    containers.onboarded_at stamp, S2). The fact is composed INSIDE
    readiness_state.compute_status via get_onboarding_state -> resolve_owner_row
    (company-scoped: an org shares one owner row), NOT the calling user's own
    row and NOT the containers row. The router no longer resolves it itself
    (INV-1: compute_status is the sole composer)."""

    async def _status_with_onboarding(self, onboarding_state: dict) -> dict:
        from routers.container import container_status

        repo = MagicMock()
        repo.get = AsyncMock(return_value=SimpleNamespace(status="active"))
        repo.get_onboarding_state = AsyncMock(return_value=onboarding_state)
        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_RUNNING_ROW)),
            patch("core.services.container_rpc.call", new=AsyncMock(return_value={"ok": True})),
            patch("core.repositories.workspace_repo.WorkspaceRepo", return_value=repo),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close()),
        ):
            return await container_status(_workspace_auth())

    @pytest.mark.asyncio
    async def test_running_container_with_complete_funnel_reports_onboarded_true(self):
        result = await self._status_with_onboarding(
            _onboarding_state_with(funnel_state="complete", onboarding_generation="onb_gen_x")
        )
        assert result["onboarded"] is True

    @pytest.mark.asyncio
    async def test_submitted_but_not_complete_reports_onboarded_false(self):
        """Submitted-but-not-yet-delivered must NOT read as onboarded --
        only "complete" stops the funnel from being offered again."""
        result = await self._status_with_onboarding(
            _onboarding_state_with(funnel_state="submitted", onboarding_generation="onb_gen_x")
        )
        assert result["onboarded"] is False

    @pytest.mark.asyncio
    async def test_running_container_with_no_owner_row_reports_onboarded_false(self):
        """Additive, safe default: no row yet (fresh signup) reports
        onboarded=False, not a missing/omitted field -- unlike teams_status,
        `onboarded` is part of the base response shape every consumer reads.
        With no onboarding_generation the composer falls back to the legacy
        containers.onboarded_at stamp (absent here -> False)."""
        result = await self._status_with_onboarding(
            _onboarding_state_with(funnel_state="not_started", onboarding_generation=None)
        )
        assert result["onboarded"] is False


class TestProvisionTaskObservability:
    """Part A: the detached ensure_user_ready dispatches (Paperclip-proxy handshake) carry a done-callback
    so a raised exception is surfaced (metric + log) instead of vanishing as a
    silent "Task exception was never retrieved"."""

    @pytest.mark.asyncio
    async def test_watch_provision_task_emits_metric_on_failure(self):
        from routers.container import _watch_provision_task

        async def boom():
            raise ValueError("boom")

        task = asyncio.get_running_loop().create_task(boom())
        await asyncio.gather(task, return_exceptions=True)
        with patch("routers.container.put_metric") as mock_metric:
            _watch_provision_task(task)
        mock_metric.assert_called_once_with("container.provision", dimensions={"status": "error"})

    @pytest.mark.asyncio
    async def test_watch_provision_task_no_metric_on_success(self):
        from routers.container import _watch_provision_task

        async def ok():
            return None

        task = asyncio.get_running_loop().create_task(ok())
        await asyncio.gather(task, return_exceptions=True)
        with patch("routers.container.put_metric") as mock_metric:
            _watch_provision_task(task)
        mock_metric.assert_not_called()

    @pytest.mark.asyncio
    async def test_watch_provision_task_ignores_cancelled(self):
        from routers.container import _watch_provision_task

        async def sleeper():
            await asyncio.sleep(10)

        task = asyncio.get_running_loop().create_task(sleeper())
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with patch("routers.container.put_metric") as mock_metric:
            _watch_provision_task(task)  # must not raise, must not emit
        mock_metric.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_watched_surfaces_raising_task(self):
        """End-to-end wiring: a coroutine dispatched through _dispatch_watched
        that raises fires the provision-error metric via the attached
        done-callback (proves the site attaches it to a real Task)."""
        from routers.container import _dispatch_watched

        holder: dict = {}

        def cap(coro):
            task = asyncio.get_running_loop().create_task(coro)
            holder["task"] = task
            return task

        async def boom():
            raise ValueError("boom")

        with (
            patch("routers.container.asyncio.create_task", side_effect=cap),
            patch("routers.container.put_metric") as mock_metric,
        ):
            _dispatch_watched(boom())
            await asyncio.gather(holder["task"], return_exceptions=True)
            await asyncio.sleep(0)  # let the done-callback fire

        mock_metric.assert_called_once_with("container.provision", dimensions={"status": "error"})


class TestStatusResponseRuntimeHost:
    def test_the_wire_body_still_carries_runtime_host_for_shipped_mobile_bundles(self):
        """Older mobile bundles hold every runtime RPC until `runtime_host` is truthy."""
        from routers.container import _status_response

        assert _status_response({"lifecycle": "ready"})["runtime_host"] == "agentcore"
