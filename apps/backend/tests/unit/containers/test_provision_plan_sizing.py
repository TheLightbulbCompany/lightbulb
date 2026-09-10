"""Durable Ultra sizing: the provision/reprovision path sizes the per-user task
def by the owner's billing plan.

The upgrade webhook resizes a running Ultra container to 2 vCPU / 8 GB, but
the provision path re-registers the task def from the CDK base on every
image-roll, provider switch, restart, and admin reprovision. If that path
hardcoded the standard 1 vCPU / 4 GB, any of those events would silently undo
the resize. These tests pin that the provision path re-derives the size from the
plan instead — so the Ultra box is durable.

Uses mocked boto3 clients + repos — no real AWS or DynamoDB.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.containers.ecs_manager import (
    PER_USER_CPU,
    PER_USER_MEMORY_MIB,
    ULTRA_CPU,
    ULTRA_MEMORY_MIB,
    EcsManager,
)
from core.services.company_package import COMPANY_STANDARD_PLAN_MAX_AGENTS


# ---------------------------------------------------------------------------
# Fixtures (mirror test_ecs_manager.py's boto3/lease/mint stubs, self-contained)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _provision_lease_stub(monkeypatch):
    """provision_user_container is lease-fenced (real DDB conditional writes on a
    direct call). Stub the lease primitives so these tests exercise sizing, not
    the lease."""
    monkeypatch.setattr("core.repositories.container_repo.takeover_provision", AsyncMock(return_value="test-claim"))
    monkeypatch.setattr("core.repositories.container_repo.update_container_leased", AsyncMock())
    monkeypatch.setattr("core.repositories.container_repo.release_provision_to_error", AsyncMock())


@pytest.fixture(autouse=True)
def _mock_bedrock_mint(monkeypatch):
    """Stub the synchronous per-owner AIP + scoped-role mint the bedrock_claude
    branch runs — these tests drive that branch as the simplest provider path
    but don't exercise the Bedrock/IAM control plane."""
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_aips",
        AsyncMock(
            return_value={
                "opus": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/o",
                "sonnet": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/s",
                "haiku": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/h",
            }
        ),
    )
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        AsyncMock(return_value="arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-owner"),
    )


@pytest.fixture
def mock_ecs_client():
    client = MagicMock()
    client.create_service.return_value = {"service": {"serviceName": "openclaw-owner-hash"}}
    client.update_service.return_value = {}
    client.describe_task_definition.return_value = {
        "taskDefinition": {
            "family": "isol8-dev-openclaw",
            "taskRoleArn": "arn:aws:iam::123456789:role/shared-task-role",
            "executionRoleArn": "arn:aws:iam::123456789:role/exec-role",
            "networkMode": "awsvpc",
            "containerDefinitions": [{"name": "openclaw", "image": "ghcr.io/openclaw:1"}],
            "volumes": [
                {
                    "name": "openclaw-workspace",
                    "efsVolumeConfiguration": {
                        "fileSystemId": "fs-test123",
                        "transitEncryption": "ENABLED",
                        "authorizationConfig": {"accessPointId": "fsap-base", "iam": "ENABLED"},
                    },
                }
            ],
            "requiresCompatibilities": ["FARGATE"],
            # A stale base cpu/memory that per-user sizing must OVERRIDE.
            "cpu": "256",
            "memory": "512",
            "runtimePlatform": None,
        }
    }
    client.register_task_definition.return_value = {
        "taskDefinition": {
            "taskDefinitionArn": "arn:aws:ecs:us-east-1:123456789:task-definition/isol8-dev-openclaw-user:9"
        }
    }
    client.describe_services.return_value = {"services": []}
    return client


@pytest.fixture
def mock_efs_client():
    client = MagicMock()
    client.create_access_point.return_value = {"AccessPointId": "fsap-user123"}
    client.exceptions = MagicMock()
    client.exceptions.AccessPointNotFound = type("AccessPointNotFound", (Exception,), {})
    return client


@pytest.fixture
def mock_settings():
    with patch("core.containers.ecs_manager.settings") as s:
        s.AWS_REGION = "us-east-1"
        s.ENVIRONMENT = "dev"
        s.ECS_CLUSTER_ARN = "arn:aws:ecs:us-east-1:123456789:cluster/test-cluster"
        s.ECS_SUBNETS = "subnet-aaa,subnet-bbb"
        s.ECS_SECURITY_GROUP_ID = "sg-12345"
        s.CLOUD_MAP_SERVICE_ARN = "arn:aws:servicediscovery:us-east-1:123456789:service/srv-test"
        s.CLOUD_MAP_NAMESPACE_NAME = "lightbulb-dev.local"
        s.CLOUD_MAP_SERVICE_NAME = "openclaw-containers"
        s.EFS_FILE_SYSTEM_ID = "fs-test123"
        yield s


@pytest.fixture
def manager(mock_settings, mock_ecs_client, mock_efs_client):
    with patch("core.containers.ecs_manager.boto3") as mock_boto3:
        mock_boto3.client.side_effect = lambda service, **kw: (
            mock_ecs_client if service == "ecs" else mock_efs_client if service == "efs" else MagicMock()
        )
        mgr = EcsManager()
    return mgr


def _billing(plan):
    """A billing row (or None) with the given plan."""
    return None if plan is None else {"owner_id": "owner", "plan": plan}


# ---------------------------------------------------------------------------
# _size_for_owner — the sizing predicate
# ---------------------------------------------------------------------------


class TestSizeForOwner:
    @pytest.fixture(autouse=True)
    def _small_roster(self, monkeypatch):
        """Default every case in this class to a small, KNOWN roster.

        Without this the helper would build a real httpx client and try to
        reach Paperclip. Tests that care about the count override it.
        """
        monkeypatch.setattr(
            "core.services.paperclip_openclaw_pairing.count_paperclip_agents_for_owner",
            AsyncMock(return_value=2),
        )

    @pytest.mark.asyncio
    async def test_ultra_pro_owner_gets_ultra_box(self, manager, monkeypatch):
        monkeypatch.setattr(
            "core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=_billing("ultra_pro"))
        )
        assert await manager._size_for_owner("owner") == (ULTRA_CPU, ULTRA_MEMORY_MIB)

    @pytest.mark.asyncio
    async def test_agent_count_over_budget_upgrades_a_standard_owner(self, manager, monkeypatch):
        """The organic-growth gap this fix closes.

        An owner on the flat plan who accumulates agents one at a time — via
        chat, the Teams REST endpoint, a marketplace hire, or Atlas creating
        them straight against Paperclip — never crossed the marketplace or
        Stripe gates, so nothing sized their box up. Prod owner with 11 agents
        crash-looped on boot for exactly this reason (24 SQLite DBs restoring
        on one vCPU -> "database is locked").
        """
        monkeypatch.setattr(
            "core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=_billing("standard"))
        )
        monkeypatch.setattr(
            "core.services.paperclip_openclaw_pairing.count_paperclip_agents_for_owner",
            AsyncMock(return_value=COMPANY_STANDARD_PLAN_MAX_AGENTS + 1),
        )
        assert await manager._size_for_owner("owner") == (ULTRA_CPU, ULTRA_MEMORY_MIB)

    @pytest.mark.asyncio
    async def test_agent_count_exactly_at_budget_stays_standard(self, manager, monkeypatch):
        """Boundary: the budget is inclusive — N agents is still standard."""
        monkeypatch.setattr(
            "core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=_billing("standard"))
        )
        monkeypatch.setattr(
            "core.services.paperclip_openclaw_pairing.count_paperclip_agents_for_owner",
            AsyncMock(return_value=COMPANY_STANDARD_PLAN_MAX_AGENTS),
        )
        assert await manager._size_for_owner("owner") == (PER_USER_CPU, PER_USER_MEMORY_MIB)

    @pytest.mark.asyncio
    async def test_undeterminable_agent_count_does_not_upgrade(self, manager, monkeypatch):
        """0 means "Paperclip unreachable", never "a tiny owner".

        It must leave the plan-derived size alone rather than be read as a
        number below the budget (which is what it literally is).
        """
        monkeypatch.setattr(
            "core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=_billing("standard"))
        )
        monkeypatch.setattr(
            "core.services.paperclip_openclaw_pairing.count_paperclip_agents_for_owner",
            AsyncMock(return_value=0),
        )
        assert await manager._size_for_owner("owner") == (PER_USER_CPU, PER_USER_MEMORY_MIB)

    @pytest.mark.asyncio
    async def test_agent_count_never_shrinks_an_ultra_owner(self, manager, monkeypatch):
        """Sizing up on count must not become sizing DOWN on count.

        Shrinking is owned by ``stripe_webhook_handlers._apply_ultra_resize``,
        which defers on oversized/unknown counts; a competing shrink here would
        bounce an Ultra owner onto the small box mid-provision.
        """
        monkeypatch.setattr(
            "core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=_billing("ultra_pro"))
        )
        monkeypatch.setattr(
            "core.services.paperclip_openclaw_pairing.count_paperclip_agents_for_owner",
            AsyncMock(return_value=1),
        )
        assert await manager._size_for_owner("owner") == (ULTRA_CPU, ULTRA_MEMORY_MIB)

    @pytest.mark.asyncio
    async def test_agent_count_lookup_error_fails_safe_to_standard(self, manager, monkeypatch):
        """A Paperclip outage must not fail a provision any more than a
        billing outage does."""
        monkeypatch.setattr(
            "core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=_billing("standard"))
        )
        monkeypatch.setattr(
            "core.services.paperclip_openclaw_pairing.count_paperclip_agents_for_owner",
            AsyncMock(side_effect=RuntimeError("paperclip down")),
        )
        assert await manager._size_for_owner("owner") == (PER_USER_CPU, PER_USER_MEMORY_MIB)

    @pytest.mark.asyncio
    async def test_standard_owner_gets_standard_box(self, manager, monkeypatch):
        monkeypatch.setattr(
            "core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=_billing("standard"))
        )
        assert await manager._size_for_owner("owner") == (PER_USER_CPU, PER_USER_MEMORY_MIB)

    @pytest.mark.asyncio
    async def test_no_billing_row_defaults_standard(self, manager, monkeypatch):
        monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=None))
        assert await manager._size_for_owner("owner") == (PER_USER_CPU, PER_USER_MEMORY_MIB)

    @pytest.mark.asyncio
    async def test_billing_read_error_fails_safe_to_standard(self, manager, monkeypatch):
        """A sizing helper must NEVER raise — a momentary billing read failure
        falls back to the standard box, not a failed provision."""
        monkeypatch.setattr(
            "core.repositories.billing_repo.get_by_owner_id",
            AsyncMock(side_effect=RuntimeError("dynamo down")),
        )
        assert await manager._size_for_owner("owner") == (PER_USER_CPU, PER_USER_MEMORY_MIB)


# ---------------------------------------------------------------------------
# provision_user_container — fresh provision (create_user_service path)
# ---------------------------------------------------------------------------


class TestFreshProvisionSizing:
    async def _provision_and_get_reg_kwargs(self, manager, mock_ecs_client, plan, monkeypatch):
        monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=_billing(plan)))
        mock_ecs_client.describe_services.return_value = {"services": []}
        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)
            mock_repo.upsert = AsyncMock(return_value={"owner_id": "owner", "status": "provisioning"})
            mock_repo.update_fields = AsyncMock(return_value={"owner_id": "owner"})
            mock_repo.update_status = AsyncMock(return_value={"owner_id": "owner"})
            await manager.provision_user_container("owner", provider_choice="bedrock_claude")
        return mock_ecs_client.register_task_definition.call_args.kwargs

    @pytest.mark.asyncio
    async def test_ultra_owner_registers_ultra_size(self, manager, mock_ecs_client, monkeypatch):
        reg = await self._provision_and_get_reg_kwargs(manager, mock_ecs_client, "ultra_pro", monkeypatch)
        assert reg["cpu"] == ULTRA_CPU
        assert reg["memory"] == ULTRA_MEMORY_MIB

    @pytest.mark.asyncio
    async def test_standard_owner_registers_standard_size(self, manager, mock_ecs_client, monkeypatch):
        reg = await self._provision_and_get_reg_kwargs(manager, mock_ecs_client, "standard", monkeypatch)
        assert reg["cpu"] == PER_USER_CPU
        assert reg["memory"] == PER_USER_MEMORY_MIB

    @pytest.mark.asyncio
    async def test_no_billing_row_registers_standard_size(self, manager, mock_ecs_client, monkeypatch):
        reg = await self._provision_and_get_reg_kwargs(manager, mock_ecs_client, None, monkeypatch)
        assert reg["cpu"] == PER_USER_CPU
        assert reg["memory"] == PER_USER_MEMORY_MIB


# ---------------------------------------------------------------------------
# reprovision path (scale-from-zero restart branch) — the durability case
# ---------------------------------------------------------------------------


class TestReprovisionRestartSizing:
    """A full reprovision (stop -> dispatch_provision) lands on the
    service-exists-with-0-desired restart branch, which re-registers the task
    def. Sizing that branch by plan is what keeps an Ultra reprovision from
    reverting to standard."""

    async def _reprovision_and_get_reg_kwargs(self, manager, mock_ecs_client, plan, monkeypatch):
        monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=_billing(plan)))
        # An ACTIVE service already exists, scaled to zero -> restart branch.
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {"status": "ACTIVE", "desiredCount": 0, "runningCount": 0, "serviceName": "openclaw-owner-hash"}
            ]
        }
        existing_row = {
            "owner_id": "owner",
            "service_name": "openclaw-owner-hash",
            "gateway_token": "gw",
            "hook_token": "ht",
            "access_point_id": "fsap-owner",
            "task_definition_arn": "arn:aws:ecs:us-east-1:123456789:task-definition/isol8-dev-openclaw-user:8",
        }
        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=existing_row)
            mock_repo.update_fields = AsyncMock(return_value=existing_row)
            mock_repo.update_status = AsyncMock(return_value=existing_row)
            await manager.provision_user_container("owner", provider_choice="bedrock_claude")
        return mock_ecs_client.register_task_definition.call_args.kwargs

    @pytest.mark.asyncio
    async def test_ultra_owner_reprovision_registers_ultra_size(self, manager, mock_ecs_client, monkeypatch):
        reg = await self._reprovision_and_get_reg_kwargs(manager, mock_ecs_client, "ultra_pro", monkeypatch)
        assert reg["cpu"] == ULTRA_CPU
        assert reg["memory"] == ULTRA_MEMORY_MIB

    @pytest.mark.asyncio
    async def test_standard_owner_reprovision_registers_standard_size(self, manager, mock_ecs_client, monkeypatch):
        reg = await self._reprovision_and_get_reg_kwargs(manager, mock_ecs_client, "standard", monkeypatch)
        assert reg["cpu"] == PER_USER_CPU
        assert reg["memory"] == PER_USER_MEMORY_MIB


# ---------------------------------------------------------------------------
# reapply_standard_sizing (admin recovery lever) — sizes by plan
# ---------------------------------------------------------------------------


class TestReapplyStandardSizing:
    @pytest.mark.asyncio
    async def test_ultra_owner_reapplies_ultra_size(self, manager, monkeypatch):
        monkeypatch.setattr(
            "core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=_billing("ultra_pro"))
        )
        manager.resize_user_container = AsyncMock(return_value="arn:new")
        result = await manager.reapply_standard_sizing("owner")
        manager.resize_user_container.assert_awaited_once_with("owner", new_cpu=ULTRA_CPU, new_memory=ULTRA_MEMORY_MIB)
        assert result == {"status": "resized", "cpu": ULTRA_CPU, "memory": ULTRA_MEMORY_MIB}

    @pytest.mark.asyncio
    async def test_standard_owner_reapplies_standard_size(self, manager, monkeypatch):
        monkeypatch.setattr(
            "core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=_billing("standard"))
        )
        manager.resize_user_container = AsyncMock(return_value="arn:new")
        result = await manager.reapply_standard_sizing("owner")
        manager.resize_user_container.assert_awaited_once_with(
            "owner", new_cpu=PER_USER_CPU, new_memory=PER_USER_MEMORY_MIB
        )
        assert result == {"status": "resized", "cpu": PER_USER_CPU, "memory": PER_USER_MEMORY_MIB}
