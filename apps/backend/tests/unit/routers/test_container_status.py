"""Tests for container endpoints: GET /status."""

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from core.services.messaging import ApiProblem
from core.services.stripe_webhook_handlers import CONVERSION_GUARD_SECONDS


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale_iso() -> str:
    """A conversion stamp older than the guard window — i.e. a dead dispatch."""
    return (datetime.now(timezone.utc) - timedelta(seconds=CONVERSION_GUARD_SECONDS + 60)).isoformat()


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


async def _noop_ensure_user_ready(auth):
    """No-op stand-in with the real qualname so capture_task can verify
    we dispatched the right coroutine. Using AsyncMock here would yield
    coro.__qualname__ == 'AsyncMockMixin._execute_mock_call', which
    defeats the contract assertion. Mirrors the real ensure_user_ready
    signature — one positional AuthContext, no flags."""
    return None


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
    companies read (core.repositories.paperclip_repo.PaperclipRepo.
    get_onboarding_state). Every PaperclipRepo test double in this file must
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
    """A PaperclipRepo test double with BOTH `.get` (legacy direct readers)
    and `.get_onboarding_state` (compute_status) configured consistently
    off the same `status`."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=SimpleNamespace(status=status) if status is not None else None)
    repo.get_onboarding_state = AsyncMock(return_value=_onboarding_state_stub(status))
    # resolve_owner_row is the canonical owner-row resolver compute_status
    # (via get_onboarding_state) delegates to. Kept on the stub so any test
    # that patches PaperclipRepo directly gets a coherent double even though
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
    Stub PaperclipRepo to an empty read by default so this file's tests
    exercise the real composition without hitting DynamoDB; the tests that
    care re-patch PaperclipRepo inline (an active/failed row, a
    funnel_state=="complete" row, or a raising read)."""
    repo = _paperclip_repo_stub(None)
    with patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=repo):
        yield repo


@pytest.fixture(autouse=True)
def _stub_trial_armer(request):
    if request.node.name == "test_workspace_trial_clock_starts_only_at_first_canonical_ready_poll":
        yield
    else:
        with patch("routers.container.arm_workspace_trial_if_unset", AsyncMock(return_value=None)):
            yield


@pytest.mark.asyncio
async def test_workspace_trial_clock_starts_only_at_first_canonical_ready_poll():
    """The trial clock arms exactly once, at the poll where the composed `runtime_phase`
    first reads "ready" -- never before, never twice. This used to cycle a RUNNING row
    through a "starting" (not-yet-gateway-connected) intermediate before "ready", but
    `resolve_runtime_phase` now reports "ready" for any running row unconditionally, with
    no health probe at all (`host_for` is pinned to "agentcore" fleet-wide) -- a running row
    is ready on its very first poll. "provisioning"/"error" are what still exercise the
    not-yet-ready leg the arm-only-once assertion depends on."""
    import boto3
    from moto import mock_aws
    from core.repositories import billing_repo
    from routers.container import container_status

    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="workspace-trials",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.put_item(Item={"owner_id": "org_x", "owner_type": "org", "subscription_status": "trial_local"})
        row = {"owner_id": "org_x", "status": "provisioning"}
        _row = row

        with (
            patch.object(billing_repo, "_get_table", return_value=table),
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch("core.services.container_rpc.call", AsyncMock(return_value={"ok": True})),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close()),
            patch("routers.container.evaluate_provision_gate", AsyncMock(return_value=None)),
        ):
            for status, phase in [("provisioning", "provisioning"), ("error", "provisioning")]:
                row["status"] = status
                result = await container_status(_workspace_auth(), flow=None)
                assert result["runtime_phase"] == phase
                assert "trial_end" not in (await billing_repo.get_by_owner_id("org_x"))
            row["status"] = "running"
            with patch("core.repositories.billing_repo.time.time", return_value=1_000):
                result = await container_status(_workspace_auth(), flow=None)
            assert result["runtime_phase"] == "ready"
            assert (await billing_repo.get_by_owner_id("org_x"))["trial_end"] == 2_593_000
            with patch("core.repositories.billing_repo.time.time", return_value=9_999):
                await container_status(_workspace_auth(), flow=None)
                assert (await billing_repo.get_by_owner_id("org_x"))["trial_end"] == 2_593_000


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
            patch(
                "routers.container.arm_workspace_trial_if_unset",
                AsyncMock(side_effect=AssertionError("paid workspaces must not hit the trial armer")),
            ),
        ):
            result = await container_status(_workspace_auth(), flow=None)

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
    @patch("routers.container.ensure_user_ready", new_callable=AsyncMock)
    @patch("routers.container.evaluate_provision_gate", new_callable=AsyncMock)
    @patch("routers.container.container_repo.get_by_owner_id")
    async def test_returns_provisioning_without_container(
        self, mock_get_row, mock_gate, mock_ensure, mock_create_task, async_client
    ):
        """No container row + no gate fires (owner) -> dispatch ensure_user_ready
        and return 200 with phase=provisioning. Replaces former 404 contract."""
        mock_get_row.return_value = None
        mock_gate.return_value = None  # no gate fires
        mock_create_task.side_effect = lambda c: c.close() or MagicMock()
        response = await async_client.get("/api/v1/container/status")
        assert response.status_code == 200
        body = response.json()
        assert body["container_status"] == "provisioning"
        assert body["runtime_phase"] == "provisioning"
        assert mock_create_task.call_count == 1


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
    async def test_no_container_no_gate_dispatches_ensure_user_ready(self):
        """No container row + no gate fires (owner) -> dispatch ensure_user_ready
        and return synthetic provisioning response (was 404 pre-Task-8)."""
        from routers.container import container_status

        captured = []
        _row = None

        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "routers.container.evaluate_provision_gate",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("routers.container.ensure_user_ready", new=_noop_ensure_user_ready),
            patch(
                "routers.container.asyncio.create_task",
                side_effect=lambda c: captured.append(c) or c.close() or c,
            ),
        ):
            result = await container_status(auth=_workspace_auth())

        assert result["container_status"] == "provisioning"
        assert result["runtime_phase"] == "provisioning"
        assert result["container_substatus"] == "ensure_user_ready"
        assert [c.__qualname__ for c in captured] == ["_noop_ensure_user_ready"]

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
            patch("routers.container.ensure_user_ready", new=AsyncMock()),
            patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=repo),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close() or c),
        ):
            result = await container_status(auth=_workspace_auth())

        assert result["container_status"] == "provisioning"
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
    async def test_revoked_zombie_running_row_returns_the_limit_card_not_a_spinner(self):
        """The meter Lambda's kill stamps status="stopped", but that write is
        one non-atomic step of a best-effort kill. If it fails (or a racing
        writer clobbers it) the row stays "running" with no live task — every
        status-keyed branch below falls through and the poll returns
        phase="starting" forever: an indefinite cold-start stepper with NO
        Upgrade path, for exactly the owner most likely to convert. The
        bedrock_revoked flag itself must therefore gate, whatever the row's
        status says."""
        from core.services import trial_cap
        from routers.container import container_status

        _row = {
            "status": "running",
            "substatus": "gateway_healthy",
            "bedrock_revoked": True,
        }

        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            # Restore declines (still capped) and leaves the flag set.
            patch("routers.container.maybe_restore_trial_bedrock", new=AsyncMock()),
            patch(
                "core.services.trial_cap.trial_block_reason",
                new=AsyncMock(return_value=trial_cap.DAILY_CAP),
            ),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close()),
            pytest.raises(HTTPException) as exc,
        ):
            await container_status(auth=_workspace_auth())

        assert exc.value.status_code == 402
        assert exc.value.detail["blocked"]["code"] == "trial_daily_cap"

    @pytest.mark.asyncio
    async def test_revoked_paid_owner_is_not_gated_by_the_trial_check(self):
        """Paid owners carry the same bedrock_revoked flag (Metronome
        low-balance enforcement) — their trial reason is None, so the revoked
        gate must fall through and the response stay the normal row (their
        restore path is metronome_gate's, not the trial one)."""
        from routers.container import container_status

        _row = {
            "status": "running",
            "substatus": "gateway_healthy",
            "bedrock_revoked": True,
        }

        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch("routers.container.maybe_restore_trial_bedrock", new=AsyncMock()),
            patch(
                "core.services.trial_cap.trial_block_reason",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "core.services.container_rpc.call",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close()),
        ):
            result = await container_status(auth=_workspace_auth())
        assert result["container_status"] == "running"

    @pytest.mark.asyncio
    async def test_personal_context_requires_workspace_before_provisioning(self):
        from routers.container import container_status

        with pytest.raises(HTTPException) as exc_info:
            await container_status(AuthContext(user_id="user_x"), flow="flow-abc")

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

        _row = None
        with (
            patch(
                "routers.container.evaluate_provision_gate",
                new=AsyncMock(return_value=None),
            ),
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "routers.container.ensure_user_ready",
                new=_noop_ensure_user_ready,
            ),
            patch(
                "routers.container.asyncio.create_task",
                side_effect=capture_task,
            ),
        ):
            result = await container_status(_workspace_auth())
        assert result["container_status"] == "provisioning"
        assert result["runtime_phase"] == "provisioning"
        assert captured == ["_noop_ensure_user_ready"], f"expected ensure_user_ready dispatched, got {captured}"

    @pytest.mark.asyncio
    async def test_status_drains_a_pending_brief(self):
        """THE poll that fulfils the first-run path: the funnel renders during
        provisioning, so the answer almost always lands before there is an
        owner row to claim against. `pending_brief` rides the SAME onboarding
        dict compute_status already read — no extra call here."""
        from routers.container import container_status

        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()

        onboarding = _onboarding_state_stub()
        onboarding["pending_brief"] = True
        repo = _paperclip_repo_stub()
        repo.get_onboarding_state = AsyncMock(return_value=onboarding)

        _row = None
        with (
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(return_value=None)),
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch("routers.container.ensure_user_ready", new=_noop_ensure_user_ready),
            patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=repo),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        ):
            await container_status(_workspace_auth("user_abc", "max@acme.com"))

        assert "reconcile_pending_brief" in captured, f"expected the drain dispatched, got {captured}"

    @pytest.mark.asyncio
    async def test_status_drains_a_pending_brief_even_when_the_poll_returns_early(self):
        """The drain hook sits ABOVE this endpoint's early returns on purpose.
        It used to sit at the bottom, which quietly excluded whole cohorts from
        ever fulfilling -- here, an org member, who 202s on the no-container
        path long before the old hook. The drain needs Paperclip, not a
        container, so those were dead ends for no reason."""
        from fastapi import HTTPException

        from routers.container import container_status

        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()

        onboarding = _onboarding_state_stub()
        onboarding["pending_brief"] = True
        repo = _paperclip_repo_stub()
        repo.get_onboarding_state = AsyncMock(return_value=onboarding)

        _row = None
        with (
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(return_value=None)),
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch("routers.container.ensure_user_ready", new=_noop_ensure_user_ready),
            patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=repo),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        ):
            with pytest.raises(HTTPException) as exc:
                await container_status(_org_member_auth("user_member", "org_xyz", "m@example.com"))

        assert exc.value.status_code == 202
        assert "reconcile_pending_brief" in captured, f"expected the drain dispatched, got {captured}"

    @pytest.mark.asyncio
    async def test_status_without_a_pending_brief_does_not_dispatch_the_drain(self):
        """The steady-state fleet polls this every ~3s — an onboarded owner
        must not schedule a no-op task on every one."""
        from routers.container import container_status

        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()

        _row = None
        with (
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(return_value=None)),
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch("routers.container.ensure_user_ready", new=_noop_ensure_user_ready),
            patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=_paperclip_repo_stub()),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        ):
            await container_status(_workspace_auth("user_abc", "max@acme.com"))

        assert "reconcile_pending_brief" not in captured

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

        _row = None
        with (
            patch(
                "routers.container.evaluate_provision_gate",
                new=AsyncMock(return_value=None),
            ),
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "routers.container.ensure_user_ready",
                new=_noop_ensure_user_ready,
            ),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        ):
            result = await container_status(_org_admin_auth("user_admin", "org_xyz", "a@example.com"))
        assert result["container_status"] == "provisioning"
        assert result["runtime_phase"] == "provisioning"
        # Org-context owner_id (org_xyz) also triggers the fire-and-forget
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

        _row = None
        with (
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(return_value=None)),
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
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
        assert captured == ["_noop_ensure_user_ready"], f"expected ensure_user_ready dispatched, got {captured}"

    @pytest.mark.asyncio
    async def test_status_no_container_gate_blocks_returns_402_unchanged(self):
        """Existing 402 behavior preserved when gate is up."""
        from fastapi import HTTPException

        from routers.container import container_status

        fake_gate = MagicMock(to_payload=lambda: {"blocked": {"code": "x"}})
        _row = None
        with (
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(return_value=fake_gate)),
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close()),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await container_status(_workspace_auth())
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
            "status": "running",
            "substatus": None,
            "created_at": "2026-05-08T00:00:00Z",
            "updated_at": "2026-05-08T00:00:00Z",
            "last_error": None,
            "last_error_at": None,
        }
        _row = fake_container
        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()

        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch("routers.container.ensure_user_ready", new=_noop_ensure_user_ready),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
            patch("core.services.container_rpc.call", new_callable=AsyncMock, return_value={"ok": True}),
        ):
            result = await container_status(_org_member_auth())

        assert result["container_status"] == "running"
        # Org-context owner_id also triggers the fire-and-forget seat
        # reconcile (Task 8), dispatched before ensure_user_ready.
        assert captured == ["_noop_ensure_user_ready"], (
            f"expected ensure_user_ready dispatched for org member on container-exists path, got {captured}"
        )

    @pytest.mark.asyncio
    async def test_status_container_exists_org_admin_dispatches_ensure_user_ready(self):
        """Org admin with an existing container MUST dispatch ensure_user_ready
        too. The dispatch is now role-blind (is_org_context), not gated on
        NOT is_org_admin. Excluding admins here was the /teams "team workspace
        not provisioned" bug for multi-admin orgs: a co-admin who joins an
        already-provisioned org never got their paperclip-membership-add."""
        from routers.container import container_status

        fake_container = {
            "status": "running",
            "substatus": None,
            "created_at": "2026-05-08T00:00:00Z",
            "updated_at": "2026-05-08T00:00:00Z",
            "last_error": None,
            "last_error_at": None,
        }
        _row = fake_container
        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()

        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch("routers.container.ensure_user_ready", new=_noop_ensure_user_ready),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
            patch("core.services.container_rpc.call", new_callable=AsyncMock, return_value={"ok": True}),
        ):
            await container_status(_org_admin_auth())

        # Org-context owner_id also triggers the fire-and-forget seat
        # reconcile (Task 8), dispatched before ensure_user_ready.
        assert captured == ["_noop_ensure_user_ready"], (
            f"org admin on container-exists path must dispatch ensure_user_ready, got {captured}"
        )


class TestContainerStatusTrialConversion:
    """GET /container/status vs. the trial→paid conversion (Task 12).

    (a) The conversion's `reprovision_for_user` scales the trial container to
        zero and then BLOCKS in a ~90s drain wait — a single EFS-WAL SQLite
        writer is required, and two live tasks corrupt it. The just-paid owner
        is polling /status every ~3s; without a guard, that poll sees
        status="stopped", passes the gate (they're paid now), and dispatches
        its OWN dispatch_provision with no drain wait. Two provisions race.

    (b) The conversion is one fire-and-forget task. If it dies, Stripe never
        re-fires it (its retry sees old_status != "trial_local"), and the
        paying customer keeps the trial container forever. The status path is
        the only thing that runs again — so it self-heals, rate-limited.
    """

    @staticmethod
    def _patches(row, *, subscription_status="active", captured=None, repo=None):
        """Common wiring: gate clear, not over the cap, phase stubbed, and the
        conversion helpers' repos stubbed. ``repo`` is the fake container_repo
        whose update_fields mutates ``row`` in place, so a re-stamped
        rate-limit anchor is visible to the NEXT poll."""
        from core.services import stripe_webhook_handlers as handlers

        def capture_task(coro):
            name = coro.__qualname__
            if captured is not None and name != "ensure_user_ready":
                captured.append(name)
            coro.close()
            return MagicMock()

        return (
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(return_value=None)),
            patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=None)),
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
        """container_repo double whose writes land on ``row``. The conversion
        marker rides the ALLOWLISTED update_conversion_fields, which cannot
        reach status or the lease (the clobber that stranded #1428)."""

        async def update_fields(owner_id, fields):
            row.update(fields)
            return row

        async def update_status(owner_id, status, substatus=None):
            row.update({"status": status, "substatus": substatus})
            return row

        return MagicMock(
            get_by_owner_id=AsyncMock(side_effect=lambda _owner_id: row),
            update_fields=AsyncMock(side_effect=update_fields),
            update_conversion_fields=AsyncMock(side_effect=update_fields),
            update_status=AsyncMock(side_effect=update_status),
        )

    @pytest.mark.asyncio
    async def test_converting_row_is_not_auto_retried(self):
        """A row the conversion owns (substatus=converting) must NOT be
        auto-retried — that second dispatch_provision has no drain wait and
        would run a task alongside the one still draining."""
        from routers.container import container_status

        row = {
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
            result = await container_status(_workspace_auth())

        repo.update_status.assert_not_awaited()
        assert captured == [], f"a converting row must not dispatch a provision, got {captured}"
        assert result["container_substatus"] == "converting"

    @pytest.mark.asyncio
    async def test_draining_row_with_clobbered_substatus_is_not_auto_retried(self):
        """The actual race. ecs_manager.stop_user_service writes
        update_status(owner, "stopped") — substatus defaults to None — so the
        converting substatus is GONE the moment the drain starts. Only the
        conversion_started_at anchor survives, and it alone must hold the
        auto-retry off for the whole drain."""
        from routers.container import container_status

        row = {
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
            await container_status(_workspace_auth())

        repo.update_status.assert_not_awaited()
        assert captured == [], f"a draining conversion must not dispatch a provision, got {captured}"

    @pytest.mark.asyncio
    async def test_plain_stopped_row_still_auto_retries(self):
        """A stopped workspace initializes its trial before auto-retry gating."""
        from routers.container import container_status

        row = {"status": "stopped", "substatus": None}
        repo = self._repo(row)
        captured: list[str] = []
        initialized = False

        async def initialize(_auth, owner_id):
            nonlocal initialized
            assert owner_id == "org_x"
            initialized = True

        async def gate(**_kwargs):
            assert initialized, "status auto-retry evaluated its gate before trial initialization"
            return None

        with contextlib.ExitStack() as stack:
            for p in self._patches(row, captured=captured, repo=repo):
                stack.enter_context(p)
            stack.enter_context(patch("routers.container.ensure_workspace_trial_default", new=initialize))
            stack.enter_context(patch("routers.container.evaluate_provision_gate", new=gate))
            result = await container_status(_workspace_auth())

        # The racy status pre-write is deliberately gone — dispatch_provision's
        # atomic lease claim IS the transition to "provisioning" now. The poll
        # only dispatches and shapes its own response.
        repo.update_status.assert_not_awaited()
        assert initialized is True
        assert captured == [], (
            "the router only shapes the response; ensure_user_ready is the one dispatcher (see test_container_status_self_heal)"
        )
        assert result["container_status"] == "provisioning"
        assert result["container_substatus"] == "auto_retry"

    @pytest.mark.asyncio
    async def test_stuck_conversion_redispatches_at_most_once_per_guard_window(self):
        """Paid billing + a container still carrying conversion_pending long
        past the guard window == the conversion task died. The status path
        re-dispatches it — ONCE. The poll runs every ~3s; three polls in a row
        must produce exactly one reprovision, or the self-heal is a storm."""
        from routers.container import container_status

        row = {
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
                await container_status(_workspace_auth())

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
            await container_status(_workspace_auth())

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
    def _repo(row):
        async def update_status(owner_id, status, substatus=None):
            row.update({"status": status, "substatus": substatus})
            return row

        return MagicMock(
            get_by_owner_id=AsyncMock(side_effect=lambda _owner_id: row),
            update_status=AsyncMock(side_effect=update_status),
        )

    @pytest.mark.asyncio
    async def test_revoked_stopped_row_restores_bedrock_then_auto_retries(self):
        """Revoked + new day (under cap, not expired): restore_owner_bedrock
        fires, bedrock_revoked clears on the row, AND the ordinary auto-retry
        still proceeds in the SAME poll — 'let the normal provisioning path
        bring the container back'."""
        from routers.container import container_status

        row = {"status": "stopped", "substatus": None, "bedrock_revoked": True}
        repo = self._repo(row)
        restore = AsyncMock()
        captured: list[str] = []

        def capture_task(coro):
            name = coro.__qualname__
            if name != "ensure_user_ready":
                captured.append(name)
            coro.close()
            return MagicMock()

        with (
            patch("routers.container.container_repo", new=repo),
            patch(
                "core.services.provision_gate.billing_repo.get_by_owner_id",
                new=AsyncMock(return_value={"subscription_status": "trial_local", "trial_end": None}),
            ),
            patch("core.services.trial_cap.billing_repo.get_trial_daily_spend", new=AsyncMock(return_value=0)),
            patch("core.billing.bedrock_aip.restore_owner_bedrock", new=restore),
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(return_value=None)),
            patch(
                "core.services.container_rpc.call",
                new_callable=AsyncMock,
                side_effect=ApiProblem("RUNTIME_UNAVAILABLE", "x", retryable=True),
            ),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        ):
            result = await container_status(_workspace_auth())

        restore.assert_awaited_once_with("org_x")
        assert row["bedrock_revoked"] is False
        assert captured == [], (
            "the router only shapes the response; ensure_user_ready is the one dispatcher (see test_container_status_self_heal)"
        )
        assert result["container_status"] == "provisioning"

    @pytest.mark.asyncio
    async def test_not_revoked_row_makes_no_billing_call_for_restore(self):
        """The overwhelmingly common poll: no bedrock_revoked flag at all —
        maybe_restore_trial_bedrock's OWN (second, gated) billing read must
        not fire.

        readiness_state.compute_status now reads the billing row on EVERY
        poll (it's one of the composer's four inputs — see
        core/services/readiness_state.py), so `billing_repo.get_by_owner_id`
        as a whole is no longer a no-call assertion; what stays true, and
        stays worth pinning, is that the restore trigger's OWN conditional
        read (short-circuited by the `bedrock_revoked` flag check before it
        touches billing at all) never fires."""
        from routers.container import container_status

        row = {"status": "running", "substatus": None}

        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=row)),
            patch(
                "core.services.provision_gate.billing_repo.get_by_owner_id",
                new=AsyncMock(return_value=None),
            ),
            patch("core.billing.bedrock_aip.restore_owner_bedrock", new=AsyncMock()) as restore,
            patch("core.services.container_rpc.call", new_callable=AsyncMock, return_value={"ok": True}),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close()),
        ):
            result = await container_status(_workspace_auth())

        restore.assert_not_awaited()
        assert result["container_status"] == "running"

    @pytest.mark.asyncio
    async def test_restore_failure_does_not_break_the_status_poll(self):
        """A restore that raises must be logged and swallowed — /status still
        answers normally (not a 500), and the ordinary auto-retry proceeds
        regardless (bedrock_revoked simply stays set for the next poll)."""
        from routers.container import container_status

        row = {"status": "stopped", "substatus": None, "bedrock_revoked": True}
        repo = self._repo(row)
        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()
            return MagicMock()

        with (
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
            patch(
                "core.services.container_rpc.call",
                new_callable=AsyncMock,
                side_effect=ApiProblem("RUNTIME_UNAVAILABLE", "x", retryable=True),
            ),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        ):
            result = await container_status(_workspace_auth())  # must not raise

        assert row["bedrock_revoked"] is True  # restore never landed
        assert result["container_status"] == "provisioning"  # the poll still answers normally


class TestContainerStatusHonestError:
    """GET /container/status must tell the truth about a FAILED provision
    (Part B). An `error` row is no longer masked as `provisioning`/`auto_retry`:
    it reports status=`error` + last_error, and the row's `retryable` flag
    (written by container_repo.mark_provision_error) decides whether a
    re-provision is auto-dispatched (transient) or the terminal error card is
    shown (deterministic). `stopped` keeps its silent auto-wake.
    """

    @classmethod
    def _patches(cls, row, *, captured):
        """Gate clear, not capped, phase stubbed, conversion inert. Records
        each create_task dispatch's qualname into `captured`."""

        def capture_task(coro):
            name = coro.__qualname__
            if name != "ensure_user_ready":
                captured.append(name)
            coro.close()
            return MagicMock()

        return (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=row)),
            patch("routers.container.evaluate_provision_gate", new=AsyncMock(return_value=None)),
            patch("core.services.trial_cap.trial_block_reason", new=AsyncMock(return_value=None)),
            patch(
                "core.services.container_rpc.call",
                new_callable=AsyncMock,
                side_effect=ApiProblem("RUNTIME_UNAVAILABLE", "x", retryable=True),
            ),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        )

    @pytest.mark.asyncio
    async def test_error_retryable_false_surfaces_error_and_does_not_redispatch(self):
        """Deterministic failure (retryable=False): report status=error +
        last_error, and do NOT auto re-provision."""
        from routers.container import container_status

        row = {
            "status": "error",
            "substatus": "provision_failed",
            "last_error": "ValueError: bad config",
            "retryable": False,
        }
        captured: list[str] = []
        with contextlib.ExitStack() as stack:
            for p in self._patches(row, captured=captured):
                stack.enter_context(p)
            result = await container_status(_workspace_auth())

        assert result["container_status"] == "error"
        assert result["last_error"] == "ValueError: bad config"
        assert "dispatch_provision" not in captured, f"deterministic error must not re-dispatch, got {captured}"
        assert captured == []

    @pytest.mark.asyncio
    async def test_error_retryable_true_reports_error_retrying_and_redispatches(self):
        """Transient failure (retryable=True): still auto re-provision, but
        report honestly — status=error, substatus=error_retrying (NEVER
        provisioning), last_error carried through."""
        from routers.container import container_status

        row = {
            "status": "error",
            "substatus": "provision_failed",
            "last_error": "EcsManagerError: spot reclaimed",
            "retryable": True,
        }
        captured: list[str] = []
        with contextlib.ExitStack() as stack:
            for p in self._patches(row, captured=captured):
                stack.enter_context(p)
            result = await container_status(_workspace_auth())

        assert result["container_status"] == "error"
        assert result["container_substatus"] == "error_retrying"
        assert result["container_substatus"] != "provisioning", "an error must never be masked as provisioning"
        assert result["last_error"] == "EcsManagerError: spot reclaimed"
        assert captured == [], (
            "the router only shapes the response; ensure_user_ready is the one dispatcher (see test_container_status_self_heal)"
        )

    @pytest.mark.asyncio
    async def test_error_defaults_to_retryable_when_flag_absent(self):
        """A legacy error row with no `retryable` field keeps the pre-T3
        self-healing behaviour: auto re-dispatch, reported as error_retrying."""
        from routers.container import container_status

        row = {"status": "error", "substatus": None, "last_error": None}
        captured: list[str] = []
        with contextlib.ExitStack() as stack:
            for p in self._patches(row, captured=captured):
                stack.enter_context(p)
            result = await container_status(_workspace_auth())

        assert result["container_status"] == "error"
        assert result["container_substatus"] == "error_retrying"
        assert captured == [], (
            "the router only shapes the response; ensure_user_ready is the one dispatcher (see test_container_status_self_heal)"
        )

    @pytest.mark.asyncio
    async def test_stopped_row_still_auto_wakes_as_provisioning(self):
        """Regression guard: `stopped` (healthy scaled-to-zero) keeps its silent
        auto-wake — dispatch + status=provisioning/substatus=auto_retry —
        untouched by the error split."""
        from routers.container import container_status

        row = {"status": "stopped", "substatus": None}
        captured: list[str] = []
        with contextlib.ExitStack() as stack:
            for p in self._patches(row, captured=captured):
                stack.enter_context(p)
            result = await container_status(_workspace_auth())

        assert result["container_status"] == "provisioning"
        assert result["container_substatus"] == "auto_retry"
        assert captured == [], (
            "the router only shapes the response; ensure_user_ready is the one dispatcher (see test_container_status_self_heal)"
        )


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
        reports phase=ready (chat works) AND teams_status=failed (degraded). The
        failed seed also triggers a self-heal re-dispatch of ensure_user_ready on
        this poll (asserted by test_personal_running_container_with_failed_seed_
        redispatches_ensure_user_ready); patch it out here so this test doesn't
        leak a real background provisioning task."""
        from routers.container import container_status

        _row = {"status": "running", "substatus": None}
        failed_repo = _paperclip_repo_stub("failed")
        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "core.services.container_rpc.call",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=failed_repo),
            patch("routers.container.ensure_user_ready", new=_noop_ensure_user_ready),
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
            patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=active_repo),
            patch("routers.container.asyncio.create_task", side_effect=lambda c: c.close()),
        ):
            result = await container_status(_workspace_auth())

        assert result["runtime_phase"] == "ready"
        assert result["teams_state"] == "active"

    @pytest.mark.asyncio
    async def test_workspace_running_container_with_failed_seed_dispatches_reconciliation(self):
        """A workspace with a failed Teams seed dispatches both reconciliations."""
        from routers.container import container_status

        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()

        _row = {"status": "running", "substatus": None}
        failed_repo = _paperclip_repo_stub("failed")
        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "core.services.container_rpc.call",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=failed_repo),
            patch("routers.container.ensure_user_ready", new=_noop_ensure_user_ready),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        ):
            result = await container_status(_workspace_auth())

        # CLIENT-HOOK removed the flat teams_status field; the contract's
        # teams_state carries it now. Bootstrap dispatch is call-site-gated on
        # funnel_state=="submitted", which this seed stub is not, so only the
        # failed-seed ensure_user_ready re-dispatch is scheduled here.
        assert result["teams_state"] == "failed"
        assert captured == ["_noop_ensure_user_ready"], (
            f"a workspace with a failed seed must dispatch both reconciliations, got {captured}"
        )

    @pytest.mark.asyncio
    async def test_workspace_running_container_with_healthy_seed_dispatches_membership_reconciliation(self):
        """A healthy workspace still dispatches seat and membership reconciliation."""
        from routers.container import container_status

        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()

        _row = {"status": "running", "substatus": None}
        active_repo = _paperclip_repo_stub("active")
        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "core.services.container_rpc.call",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=active_repo),
            patch("routers.container.ensure_user_ready", new=_noop_ensure_user_ready),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        ):
            result = await container_status(_workspace_auth())

        assert result["teams_state"] == "active"
        assert captured == ["_noop_ensure_user_ready"]

    @pytest.mark.asyncio
    async def test_workspace_running_container_with_missing_ledger_dispatches_reconciliation(self):
        """A workspace with a missing ledger dispatches both reconciliations."""
        from routers.container import container_status

        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()

        _row = {"status": "running", "substatus": None}
        active_repo = _paperclip_repo_stub("active")
        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "core.services.container_rpc.call",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=active_repo),
            patch(
                "core.services.readiness_state.billing_repo.get_by_owner_id",
                new=AsyncMock(
                    return_value={
                        "owner_id": "org_x",
                        "subscription_status": "active",
                        "stripe_customer_id": "cus_1",
                        "stripe_subscription_id": "sub_1",
                        # No metronome_customer_id -- the ledger never got provisioned.
                    }
                ),
            ),
            patch("routers.container.ensure_user_ready", new=_noop_ensure_user_ready),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        ):
            result = await container_status(_workspace_auth())

        assert result["teams_state"] == "active", "isolate the ledger condition -- Teams is healthy here"
        assert captured == ["_noop_ensure_user_ready"]

    @pytest.mark.asyncio
    async def test_workspace_running_container_with_ledger_dispatches_membership_reconciliation(self):
        """A workspace with a ledger still dispatches seat and membership reconciliation."""
        from routers.container import container_status

        captured: list[str] = []

        def capture_task(coro):
            captured.append(coro.__qualname__)
            coro.close()

        _row = {"status": "running", "substatus": None}
        active_repo = _paperclip_repo_stub("active")
        with (
            patch("routers.container.container_repo.get_by_owner_id", new=AsyncMock(return_value=_row)),
            patch(
                "core.services.container_rpc.call",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=active_repo),
            patch(
                "core.services.readiness_state.billing_repo.get_by_owner_id",
                new=AsyncMock(
                    return_value={
                        "owner_id": "org_x",
                        "subscription_status": "active",
                        "stripe_customer_id": "cus_1",
                        "stripe_subscription_id": "sub_1",
                        "metronome_customer_id": "mtr_1",
                        "metronome_contract_id": "ctr_1",
                    }
                ),
            ),
            patch("routers.container.ensure_user_ready", new=_noop_ensure_user_ready),
            patch("routers.container.asyncio.create_task", side_effect=capture_task),
        ):
            result = await container_status(_workspace_auth())

        assert result["teams_state"] == "active"
        assert captured == ["_noop_ensure_user_ready"]

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
            patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=repo),
            patch("routers.container.ensure_user_ready", new=_noop_ensure_user_ready),
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
            patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=repo),
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
    """Part A: the detached ensure_user_ready dispatches carry a done-callback
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
