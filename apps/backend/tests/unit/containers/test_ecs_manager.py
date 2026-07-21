"""Tests for EcsManager (ECS Fargate service lifecycle with per-user EFS isolation).

Uses mocked boto3 clients and container_repo -- no real AWS or
DynamoDB required.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from core.containers.ecs_manager import (
    START_REFUSED_CAP,
    START_REFUSED_CONVERTING,
    ContainerStartRefused,
    EcsManager,
    EcsManagerError,
    GATEWAY_PORT,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_TEST_AIP_ARNS = {
    "opus": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/o",
    "sonnet": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/s",
    "haiku": "arn:aws:bedrock:us-east-1:123456789:application-inference-profile/h",
}


@pytest.fixture(autouse=True)
def _provision_lease_stub(monkeypatch):
    """provision_user_container is now lease-fenced: a direct call (the
    deliberate-door path these tests exercise) takes the lease over and
    checkpoints it, which are real DynamoDB conditional writes. Stub the lease
    primitives so the tests keep exercising the provisioning logic itself;
    the lease semantics have their own moto-backed suite
    (test_container_repo_provision_claim.py)."""
    monkeypatch.setattr(
        "core.repositories.container_repo.takeover_provision",
        AsyncMock(return_value="test-claim"),
    )
    monkeypatch.setattr(
        "core.repositories.container_repo.update_container_leased",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "core.repositories.container_repo.release_provision_to_error",
        AsyncMock(),
    )


@pytest.fixture(autouse=True)
def _mock_bedrock_mint(monkeypatch):
    """Stub the synchronous per-owner AIP + scoped-role mint that the
    bedrock_claude provision branch now runs (Task 9). These tests drive that
    branch as the simplest provider path but don't exercise the Bedrock/IAM
    control plane — without this they'd hit real boto3 and 500 on the
    bedrock-aip-owners table. Mirrors TestBedrockProvisionMint's patching."""
    from unittest.mock import AsyncMock as _AsyncMock

    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_aips",
        _AsyncMock(return_value=dict(_TEST_AIP_ARNS)),
    )
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _AsyncMock(return_value="arn:aws:iam::123456789:role/isol8-dev-owner-bedrock-user_test_123"),
    )


@pytest.fixture(autouse=True)
def _start_guards_clear(monkeypatch):
    """Default the two start-chokepoint guards to "clear".

    ``start_user_service`` now refuses to start a container for an over-cap
    trial owner or while a trial->paid conversion owns the row. Both guards read
    DynamoDB (``billing_repo`` for the cap, ``container_repo`` for the
    conversion stamp), so without a default the pre-existing lifecycle tests —
    which patch neither — would reach real AWS. ``None`` from both reads is the
    honest "no billing row, no container row" state: not a trial owner, no
    conversion. TestStartUserServiceGuards overrides these per-test.
    """
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id",
        AsyncMock(return_value=None),
    )


@pytest.fixture
def mock_ecs_client():
    """Create a mock ECS boto3 client."""
    client = MagicMock()
    client.create_service.return_value = {"service": {"serviceName": "openclaw-user_test_123-f4ae64abb2db"}}
    client.update_service.return_value = {}
    client.delete_service.return_value = {}
    client.describe_task_definition.return_value = {
        "taskDefinition": {
            "family": "isol8-dev-openclaw",
            "taskRoleArn": "arn:aws:iam::123456789:role/task-role",
            "executionRoleArn": "arn:aws:iam::123456789:role/exec-role",
            "networkMode": "awsvpc",
            "containerDefinitions": [{"name": "openclaw", "image": "alpine/openclaw:latest"}],
            "volumes": [
                {
                    "name": "openclaw-workspace",
                    "efsVolumeConfiguration": {
                        "fileSystemId": "fs-test123",
                        "transitEncryption": "ENABLED",
                        "authorizationConfig": {
                            "accessPointId": "fsap-base",
                            "iam": "ENABLED",
                        },
                    },
                }
            ],
            "requiresCompatibilities": ["FARGATE"],
            "cpu": "256",
            "memory": "512",
            "runtimePlatform": None,
        }
    }
    client.register_task_definition.return_value = {
        "taskDefinition": {"taskDefinitionArn": "arn:aws:ecs:us-east-1:123456789:task-definition/isol8-dev-openclaw:42"}
    }
    client.describe_services.return_value = {
        "services": [
            {
                "serviceName": "openclaw-user_test_123-f4ae64abb2db",
                "deployments": [{"id": "ecs-svc/primary", "status": "PRIMARY", "rolloutState": "IN_PROGRESS"}],
            }
        ]
    }
    client.list_tasks.return_value = {"taskArns": []}
    client.describe_tasks.return_value = {"tasks": []}
    client.deregister_task_definition.return_value = {}
    return client


@pytest.fixture
def mock_efs_client():
    """Create a mock EFS boto3 client."""
    client = MagicMock()
    client.create_access_point.return_value = {"AccessPointId": "fsap-user123"}
    client.delete_access_point.return_value = {}
    # Create a mock exceptions attribute for AccessPointNotFound
    client.exceptions = MagicMock()
    client.exceptions.AccessPointNotFound = type("AccessPointNotFound", (Exception,), {})
    return client


@pytest.fixture
def mock_servicediscovery_client():
    """Create a mock AWS Cloud Map (servicediscovery) boto3 client."""
    client = MagicMock()
    client.discover_instances.return_value = {"Instances": []}
    return client


@pytest.fixture
def mock_settings():
    """Mock settings with test ECS configuration."""
    with patch("core.containers.ecs_manager.settings") as s:
        s.AWS_REGION = "us-east-1"
        s.ENVIRONMENT = "dev"
        s.ECS_CLUSTER_ARN = "arn:aws:ecs:us-east-1:123456789:cluster/test-cluster"
        s.ECS_SUBNETS = "subnet-aaa,subnet-bbb"
        s.ECS_SECURITY_GROUP_ID = "sg-12345"
        s.CLOUD_MAP_SERVICE_ARN = "arn:aws:servicediscovery:us-east-1:123456789:service/srv-test"
        s.CLOUD_MAP_NAMESPACE_NAME = "isol8-dev.local"
        s.CLOUD_MAP_SERVICE_NAME = "openclaw-containers"
        s.EFS_FILE_SYSTEM_ID = "fs-test123"
        yield s


@pytest.fixture
def manager(mock_settings, mock_ecs_client, mock_efs_client, mock_servicediscovery_client):
    """Create an EcsManager with mocked boto3 clients."""
    with patch("core.containers.ecs_manager.boto3") as mock_boto3:

        def client_factory(service, **kwargs):
            if service == "ecs":
                return mock_ecs_client
            elif service == "efs":
                return mock_efs_client
            elif service == "servicediscovery":
                return mock_servicediscovery_client
            return MagicMock()

        mock_boto3.client.side_effect = client_factory
        mgr = EcsManager()
    return mgr


def _make_container_dict(
    user_id="user_test_123",
    service_name="openclaw-user_test_123-f4ae64abb2db",
    gateway_token="tok-abc",
    status="running",
    access_point_id="fsap-test-default",
    task_definition_arn=None,
    substatus=None,
):
    """Helper to create a container dict for mocking DynamoDB repo responses.

    Default access_point_id is set so resize_user_container (which now reads
    it from the row) works in tests that don't override it explicitly. Pass
    access_point_id=None to omit it (e.g. to test the missing-field error).
    """
    d = {
        "owner_id": user_id,
        "service_name": service_name,
        "gateway_token": gateway_token,
        "status": status,
    }
    if access_point_id is not None:
        d["access_point_id"] = access_point_id
    if task_definition_arn is not None:
        d["task_definition_arn"] = task_definition_arn
    if substatus is not None:
        d["substatus"] = substatus
    return d


# ---------------------------------------------------------------------------
# Service naming
# ---------------------------------------------------------------------------


class TestServiceNaming:
    """Test deterministic, collision-resistant service name generation."""

    def test_service_name_is_deterministic(self, manager):
        """Same user_id always produces the same service name."""
        name1 = manager._service_name("user_test_123")
        name2 = manager._service_name("user_test_123")
        assert name1 == name2

    def test_service_name_includes_user_id_and_hash(self, manager):
        """Service name includes sanitized user_id and hash suffix."""
        name = manager._service_name("user_test_123")
        assert name.startswith("openclaw-user_test_123-")
        # Hash suffix is 12 hex chars
        hash_part = name.split("-")[-1]
        assert len(hash_part) == 12
        assert all(c in "0123456789abcdef" for c in hash_part)

    def test_service_name_different_users(self, manager):
        """Different user IDs produce different service names."""
        name1 = manager._service_name("user_2abc123")
        name2 = manager._service_name("user_2xyz789")
        assert name1 != name2

    def test_service_name_similar_prefix_no_collision(self, manager):
        """Users sharing a prefix (e.g. 'user_') get unique names."""
        name1 = manager._service_name("user_2aaaaaa")
        name2 = manager._service_name("user_2aaaaaab")
        assert name1 != name2


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


class TestEcsManagerInit:
    """Test EcsManager initialization."""

    def test_parses_subnets(self, manager):
        """Subnets are parsed from comma-separated string."""
        assert manager._subnets == ["subnet-aaa", "subnet-bbb"]

    def test_security_groups(self, manager):
        """Security group is wrapped in a list."""
        assert manager._security_groups == ["sg-12345"]

    def test_cluster_set(self, manager):
        """Cluster ARN is set from settings."""
        assert "test-cluster" in manager._cluster

    def test_efs_file_system_id_set(self, manager):
        """EFS file system ID is set from settings."""
        assert manager._efs_file_system_id == "fs-test123"

    def test_empty_subnets(self, mock_settings, mock_efs_client):
        """Empty subnet string produces empty list."""
        mock_settings.ECS_SUBNETS = ""
        with patch("core.containers.ecs_manager.boto3") as mock_boto3:
            mock_boto3.client.side_effect = lambda svc, **kw: mock_efs_client if svc == "efs" else MagicMock()
            mgr = EcsManager()
        assert mgr._subnets == []

    def test_subnets_with_whitespace(self, mock_settings, mock_efs_client):
        """Subnets with extra whitespace are trimmed."""
        mock_settings.ECS_SUBNETS = " subnet-aaa , subnet-bbb , "
        with patch("core.containers.ecs_manager.boto3") as mock_boto3:
            mock_boto3.client.side_effect = lambda svc, **kw: mock_efs_client if svc == "efs" else MagicMock()
            mgr = EcsManager()
        assert mgr._subnets == ["subnet-aaa", "subnet-bbb"]


# ---------------------------------------------------------------------------
# _create_access_point
# ---------------------------------------------------------------------------


class TestCreateAccessPoint:
    """Test per-user EFS access point creation."""

    def test_creates_access_point(self, manager, mock_efs_client):
        """_create_access_point calls EFS API with correct parameters."""
        ap_id = manager._create_access_point("user_test_123")

        assert ap_id == "fsap-user123"
        mock_efs_client.create_access_point.assert_called_once()
        call_kwargs = mock_efs_client.create_access_point.call_args.kwargs
        assert call_kwargs["FileSystemId"] == "fs-test123"
        assert call_kwargs["PosixUser"] == {"Uid": 1000, "Gid": 1000}
        assert call_kwargs["RootDirectory"]["Path"] == "/users/user_test_123"
        assert call_kwargs["RootDirectory"]["CreationInfo"]["OwnerUid"] == 1000
        assert call_kwargs["RootDirectory"]["CreationInfo"]["Permissions"] == "0755"

    def test_access_point_failure_raises(self, manager, mock_efs_client):
        """EFS API failure raises EcsManagerError."""
        mock_efs_client.create_access_point.side_effect = ClientError(
            {"Error": {"Code": "FileSystemNotFound", "Message": "not found"}},
            "CreateAccessPoint",
        )

        with pytest.raises(EcsManagerError, match="Failed to create EFS access point"):
            manager._create_access_point("user_test_123")


# ---------------------------------------------------------------------------
# _register_task_definition
# ---------------------------------------------------------------------------


class TestRegisterTaskDefinition:
    """Test per-user task definition registration."""

    def test_clones_task_def_with_access_point(self, manager, mock_ecs_client):
        """_register_task_definition clones base task def with per-user access point."""
        task_def_arn = manager._register_task_definition("fsap-user123")

        assert "task-definition" in task_def_arn

        # Verify describe was called against the BASE family (not an ARN with
        # revision). The base family is uncontaminated by per-user clones —
        # those go to `<base>-user` — so a bare family lookup deterministically
        # returns the latest CDK base.
        mock_ecs_client.describe_task_definition.assert_called_once_with(
            taskDefinition=manager._base_task_def_family,
        )
        assert manager._base_task_def_family == "isol8-dev-openclaw"

        # Verify register was called with overridden access point AND in the
        # per-user family (not the base family) so future describes of the
        # base family don't pick up this revision.
        mock_ecs_client.register_task_definition.assert_called_once()
        call_kwargs = mock_ecs_client.register_task_definition.call_args.kwargs
        assert call_kwargs["family"] == "isol8-dev-openclaw-user"
        volumes = call_kwargs["volumes"]
        assert len(volumes) == 1
        efs_config = volumes[0]["efsVolumeConfiguration"]
        assert efs_config["authorizationConfig"]["accessPointId"] == "fsap-user123"

    def test_register_failure_raises(self, manager, mock_ecs_client):
        """ECS API failure during register raises EcsManagerError."""
        mock_ecs_client.register_task_definition.side_effect = ClientError(
            {"Error": {"Code": "ClientException", "Message": "failed"}},
            "RegisterTaskDefinition",
        )

        with pytest.raises(EcsManagerError, match="Failed to register per-user task definition"):
            manager._register_task_definition("fsap-user123")

    def test_register_preserves_env_vars_from_base(self, manager, mock_ecs_client):
        """Per-user registration must preserve env vars on the base container.

        Regression: in incident 2026-04-17 the per-user cloner produced task
        defs missing CLAWHUB_WORKDIR, even though the CDK base had it. This
        sent every clawhub-installed skill to a path no agent scanner reads.
        """
        mock_ecs_client.describe_task_definition.return_value = {
            "taskDefinition": {
                "family": "isol8-dev-openclaw",
                "taskRoleArn": "arn:aws:iam::123456789:role/task-role",
                "executionRoleArn": "arn:aws:iam::123456789:role/exec-role",
                "networkMode": "awsvpc",
                "containerDefinitions": [
                    {
                        "name": "openclaw",
                        "image": "alpine/openclaw:latest",
                        "environment": [
                            {"name": "HOME", "value": "/home/node"},
                            {"name": "CHOKIDAR_USEPOLLING", "value": "true"},
                            {"name": "CLAWHUB_WORKDIR", "value": "/home/node/.openclaw"},
                        ],
                    }
                ],
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
                "cpu": "256",
                "memory": "512",
            }
        }

        manager._register_task_definition("fsap-user123")

        call_kwargs = mock_ecs_client.register_task_definition.call_args.kwargs
        env = {e["name"]: e["value"] for e in call_kwargs["containerDefinitions"][0]["environment"]}
        assert env["CLAWHUB_WORKDIR"] == "/home/node/.openclaw"
        assert env["HOME"] == "/home/node"
        assert env["CHOKIDAR_USEPOLLING"] == "true"


# ---------------------------------------------------------------------------
# create_user_service
# ---------------------------------------------------------------------------


class TestCreateUserService:
    """Test ECS service creation with per-user EFS isolation."""

    @pytest.mark.asyncio
    async def test_creates_service_and_repo_record(self, manager, mock_ecs_client, mock_efs_client):
        """create_user_service upserts repo record early, then creates access point, task def, service."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict())

            service_name = await manager.create_user_service("user_test_123", "token-abc")

            assert service_name == "openclaw-user_test_123-f4ae64abb2db"

            # Verify repo upsert called early
            mock_repo.upsert.assert_called_once_with(
                "user_test_123",
                {
                    "service_name": "openclaw-user_test_123-f4ae64abb2db",
                    "gateway_token": "token-abc",
                    "status": "provisioning",
                    "substatus": None,
                    "owner_type": "personal",
                },
            )

            # Verify EFS access point was created
            mock_efs_client.create_access_point.assert_called_once()

            # Verify task definition was cloned
            mock_ecs_client.describe_task_definition.assert_called_once()
            mock_ecs_client.register_task_definition.assert_called_once()

            # Verify ECS create_service called
            mock_ecs_client.create_service.assert_called_once()
            call_kwargs = mock_ecs_client.create_service.call_args.kwargs
            assert call_kwargs["cluster"] == manager._cluster
            assert call_kwargs["serviceName"] == "openclaw-user_test_123-f4ae64abb2db"
            assert call_kwargs["desiredCount"] == 0
            assert call_kwargs["capacityProviderStrategy"] == [{"capacityProvider": "FARGATE_SPOT", "weight": 1}]
            assert "launchType" not in call_kwargs
            assert call_kwargs["enableExecuteCommand"] is True

            # Verify update_fields called for substatus progression (3 calls)
            assert mock_repo.update_fields.call_count == 3

    @pytest.mark.asyncio
    async def test_create_service_enables_deployment_circuit_breaker(self, manager, mock_ecs_client, mock_efs_client):
        """create_service MUST pass deploymentCircuitBreaker so ECS surfaces a
        rolloutState=FAILED signal when a per-user provision fails (bad image,
        crash loop). Without this, _await_running_transition has no way to
        distinguish a slow start from a permanent failure."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict())

            await manager.create_user_service("user_test_123", "token-abc")

            call_kwargs = mock_ecs_client.create_service.call_args.kwargs
            dc = call_kwargs.get("deploymentConfiguration") or {}
            cb = dc.get("deploymentCircuitBreaker") or {}
            assert cb.get("enable") is True, (
                "Deployment circuit breaker must be enabled so rolloutState=FAILED "
                "can be used as the failure signal by _await_running_transition."
            )
            # rollback=False: no previous deployment to roll back to on first deploy.
            assert cb.get("rollback") is False

    @pytest.mark.asyncio
    async def test_create_service_pins_single_writer_deployment_config(self, manager, mock_ecs_client, mock_efs_client):
        """Per-user services MUST deploy with maximumPercent=100 +
        minimumHealthyPercent=0 so ECS stops the old task before starting the
        new one. The per-owner EFS state is WAL-mode SQLite, which corrupts if
        two tasks (draining old + new) hold the same DB open over NFS during a
        rolling deploy — there must only ever be a single writer."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict())

            await manager.create_user_service("user_test_123", "token-abc")

            dc = mock_ecs_client.create_service.call_args.kwargs.get("deploymentConfiguration") or {}
            assert dc.get("maximumPercent") == 100, (
                "maximumPercent must be 100 so ECS never runs two tasks at once (single EFS SQLite writer)."
            )
            assert dc.get("minimumHealthyPercent") == 0, (
                "minimumHealthyPercent must be 0 so ECS can drop to zero tasks before starting the replacement."
            )

    @pytest.mark.asyncio
    async def test_ecs_failure_raises_and_rolls_back(self, manager, mock_ecs_client, mock_efs_client):
        """ECS API failure raises EcsManagerError, sets error status, and rolls back AWS resources."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict())

            mock_ecs_client.create_service.side_effect = ClientError(
                {"Error": {"Code": "ClusterNotFoundException", "Message": "not found"}},
                "CreateService",
            )

            with pytest.raises(EcsManagerError, match="Failed to create ECS service"):
                await manager.create_user_service("user_test_123", "token")

            # Error status set via update_fields
            error_calls = [c for c in mock_repo.update_fields.call_args_list if c[0][1].get("status") == "error"]
            assert len(error_calls) >= 1

            # Rollback: task def deregistered and access point deleted
            mock_ecs_client.deregister_task_definition.assert_called_once()
            mock_efs_client.delete_access_point.assert_called_once_with(AccessPointId="fsap-user123")

    @pytest.mark.asyncio
    async def test_access_point_failure_raises(self, manager, mock_efs_client, mock_ecs_client):
        """EFS access point creation failure raises EcsManagerError and sets error status."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict())

            mock_efs_client.create_access_point.side_effect = ClientError(
                {"Error": {"Code": "FileSystemNotFound", "Message": "not found"}},
                "CreateAccessPoint",
            )

            with pytest.raises(EcsManagerError, match="Failed to create EFS access point"):
                await manager.create_user_service("user_test_123", "token")

            # Error status set
            error_calls = [c for c in mock_repo.update_fields.call_args_list if c[0][1].get("status") == "error"]
            assert len(error_calls) >= 1

            # No AWS rollback needed -- nothing was created yet
            mock_ecs_client.create_service.assert_not_called()
            mock_ecs_client.deregister_task_definition.assert_not_called()

    @pytest.mark.asyncio
    async def test_task_def_failure_rolls_back_access_point(self, manager, mock_ecs_client, mock_efs_client):
        """Task definition failure rolls back the access point and sets error status."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict())

            mock_ecs_client.register_task_definition.side_effect = ClientError(
                {"Error": {"Code": "ClientException", "Message": "failed"}},
                "RegisterTaskDefinition",
            )

            with pytest.raises(EcsManagerError, match="Failed to register per-user task definition"):
                await manager.create_user_service("user_test_123", "token")

            # Error status set
            error_calls = [c for c in mock_repo.update_fields.call_args_list if c[0][1].get("status") == "error"]
            assert len(error_calls) >= 1

            # Access point should be cleaned up
            mock_efs_client.delete_access_point.assert_called_once_with(AccessPointId="fsap-user123")
            # No ECS service should have been created
            mock_ecs_client.create_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_substatus_progression(self, manager, mock_ecs_client, mock_efs_client):
        """create_user_service updates substatus at each step via container_repo."""
        update_calls = []

        async def track_update(user_id, fields):
            update_calls.append(fields)
            return _make_container_dict()

        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(side_effect=track_update)

            await manager.create_user_service("user_test_123", "token-abc")

            # _update_container is called for each substatus progression
            assert len(update_calls) == 3
            assert update_calls[0]["substatus"] == "efs_created"
            assert "access_point_id" in update_calls[0]
            assert update_calls[1]["substatus"] == "task_registered"
            assert "task_definition_arn" in update_calls[1]
            assert update_calls[2]["substatus"] == "service_created"

    @pytest.mark.asyncio
    async def test_falls_back_to_launch_type_when_spot_provider_rejected(
        self, manager, mock_ecs_client, mock_efs_client
    ):
        """A cluster without FARGATE_SPOT associated (CDK deploy lagging the
        backend deploy) rejects the strategy with InvalidParameterException.
        Provisioning must not hard-fail on a cost optimization — retry with
        launchType=FARGATE, which works on any cluster."""
        mock_ecs_client.create_service.side_effect = [
            ClientError(
                {"Error": {"Code": "InvalidParameterException", "Message": "capacity provider not associated"}},
                "CreateService",
            ),
            {"service": {}},
        ]
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict())

            await manager.create_user_service("user_test_123", "token-abc")

        assert mock_ecs_client.create_service.call_count == 2
        first = mock_ecs_client.create_service.call_args_list[0].kwargs
        retry = mock_ecs_client.create_service.call_args_list[1].kwargs
        assert first["capacityProviderStrategy"] == [{"capacityProvider": "FARGATE_SPOT", "weight": 1}]
        assert "capacityProviderStrategy" not in retry
        assert retry["launchType"] == "FARGATE"

    @pytest.mark.asyncio
    async def test_non_capacity_provider_create_errors_still_raise(self, manager, mock_ecs_client, mock_efs_client):
        """Only the capacity-provider rejection triggers the on-demand retry;
        any other create_service failure keeps the existing error path
        (mark error + rollback), it must not be swallowed."""
        mock_ecs_client.create_service.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
            "CreateService",
        )
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict())

            with pytest.raises(Exception):
                await manager.create_user_service("user_test_123", "token-abc")

        assert mock_ecs_client.create_service.call_count == 1


# ---------------------------------------------------------------------------
# stop_user_service
# ---------------------------------------------------------------------------


class TestStopUserService:
    """Test scaling service to 0."""

    @pytest.mark.asyncio
    async def test_stop_scales_to_zero(self, manager, mock_ecs_client):
        """stop_user_service calls update_service with desiredCount=0."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="running"))
            mock_repo.update_status = AsyncMock(return_value=_make_container_dict(status="stopped"))
            mock_repo.stamp_stop_reason = AsyncMock()

            await manager.stop_user_service("user_test_123")

            mock_ecs_client.update_service.assert_called_once_with(
                cluster=manager._cluster,
                service="openclaw-user_test_123-f4ae64abb2db",
                desiredCount=0,
            )
            mock_repo.update_status.assert_called_once_with("user_test_123", "stopped")
            # Default reason "manual" is stamped (clears any stale parked_at).
            mock_repo.stamp_stop_reason.assert_awaited_once_with("user_test_123", "manual")

    @pytest.mark.asyncio
    async def test_stop_no_db_record(self, manager, mock_ecs_client):
        """stop_user_service with no repo record still calls ECS but skips status update."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)

            await manager.stop_user_service("user_test_123")

            mock_ecs_client.update_service.assert_called_once()
            mock_repo.update_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_ecs_failure_raises(self, manager, mock_ecs_client):
        """ECS API failure raises EcsManagerError."""
        mock_ecs_client.update_service.side_effect = ClientError(
            {"Error": {"Code": "ServiceNotFoundException", "Message": "not found"}},
            "UpdateService",
        )

        with pytest.raises(EcsManagerError, match="Failed to stop ECS service"):
            await manager.stop_user_service("user_test_123")


# ---------------------------------------------------------------------------
# start_user_service
# ---------------------------------------------------------------------------


class TestStartUserService:
    """Test scaling service to 1."""

    @pytest.mark.asyncio
    async def test_start_scales_to_one_without_force_new_deployment(self, manager, mock_ecs_client):
        """start_user_service calls update_service with desiredCount=1 ONLY.

        forceNewDeployment is NOT passed: when stop_user_service has just run
        but ECS hasn't yet stopped the old task, a follow-up start_user_service
        with forceNewDeployment=True would terminate the still-running task
        and produce a ~30s post-login outage. Plain desiredCount=1 lets ECS
        keep the existing healthy task if there is one.
        """
        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="stopped"))
            mock_repo.update_status = AsyncMock(return_value=_make_container_dict(status="provisioning"))

            await manager.start_user_service("user_test_123")

            mock_ecs_client.update_service.assert_called_once_with(
                cluster=manager._cluster,
                service="openclaw-user_test_123-f4ae64abb2db",
                desiredCount=1,
                deploymentConfiguration={
                    "deploymentCircuitBreaker": {
                        "enable": True,
                        "rollback": False,
                    },
                    "minimumHealthyPercent": 0,
                    "maximumPercent": 100,
                },
            )
            # Belt + suspenders: forceNewDeployment must NOT be in kwargs.
            call_kwargs = mock_ecs_client.update_service.call_args.kwargs
            assert "forceNewDeployment" not in call_kwargs, (
                "start_user_service must not force a new deployment — kills "
                "still-running tasks during the stop -> start race"
            )
            mock_repo.update_status.assert_called_once_with("user_test_123", "provisioning")

    @pytest.mark.asyncio
    async def test_start_fires_running_transition_poller(self, manager, mock_ecs_client):
        """Cold-start restart MUST fire _await_running_transition, or the cold-
        started container will get stuck at status=provisioning forever when
        its ECS task takes >10s to become healthy and the user leaves before
        making another request."""
        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock) as mock_await,
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="stopped"))
            mock_repo.update_status = AsyncMock(return_value=_make_container_dict(status="provisioning"))

            await manager.start_user_service("user_test_123")

            # Give the fire-and-forget task a chance to be scheduled.
            # asyncio.create_task schedules it immediately; a yield is enough.
            import asyncio as _asyncio

            await _asyncio.sleep(0)

            mock_await.assert_called_once_with("user_test_123")

    @pytest.mark.asyncio
    async def test_start_no_db_record(self, manager, mock_ecs_client):
        """start_user_service with no repo record still calls ECS."""
        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)

            await manager.start_user_service("user_test_123")

            mock_ecs_client.update_service.assert_called_once()
            mock_repo.update_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_ecs_failure_raises(self, manager, mock_ecs_client):
        """ECS API failure raises EcsManagerError."""
        mock_ecs_client.update_service.side_effect = ClientError(
            {"Error": {"Code": "ServiceNotFoundException", "Message": "not found"}},
            "UpdateService",
        )

        with patch.object(manager, "_await_running_transition", new_callable=AsyncMock):
            with pytest.raises(EcsManagerError, match="Failed to start ECS service"):
                await manager.start_user_service("user_test_123")

    @pytest.mark.asyncio
    async def test_start_service_enables_circuit_breaker_on_existing_services(self, manager, mock_ecs_client):
        """start_user_service's update_service call must include the circuit
        breaker so pre-existing services without circuit-breaker get upgraded
        the first time we touch them. Otherwise _await_running_transition
        can never receive rolloutState=FAILED and polls forever on a broken
        pre-existing service."""
        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="stopped"))
            mock_repo.update_status = AsyncMock(return_value=_make_container_dict(status="provisioning"))

            await manager.start_user_service("user_test_123")

            call_kwargs = mock_ecs_client.update_service.call_args.kwargs
            dc = call_kwargs.get("deploymentConfiguration") or {}
            cb = dc.get("deploymentCircuitBreaker") or {}
            assert cb.get("enable") is True
            assert cb.get("rollback") is False


# ---------------------------------------------------------------------------
# start_user_service — the start chokepoint's guards
# ---------------------------------------------------------------------------


class TestStartUserServiceGuards:
    """``start_user_service`` is the ONE place every container start passes.

    Both guards used to live only on the ``GET /container/status`` auto-retry
    branch, so ``POST /container/provision`` — which restarts a stopped
    container directly — bypassed both:

      1. The free-tier daily cap. A trial_local owner whose container the cap
         scaled to zero could simply wake it back up.
      2. The trial->paid conversion. ``reprovision_for_user`` scales the trial
         container to zero and then BLOCKS in a ~90s drain wait, because the
         per-owner state is WAL-mode SQLite on EFS and a second live task on it
         is our documented corruption mode. A start during that window IS that
         second task.

    So the checks moved down here. These tests drive the REAL predicates
    (``trial_cap.trial_block_reason`` and ``conversion_guard_active``) through
    stubbed repos rather than mocking the predicates out, so a change in either
    one is caught here.
    """

    @staticmethod
    def _billing(monkeypatch, *, subscription_status: str | None, spent_microcents: int = 0):
        """Stub the billing repo the real ``trial_block_reason`` reads through."""
        monkeypatch.setattr(
            "core.repositories.billing_repo.get_by_owner_id",
            AsyncMock(return_value=({"subscription_status": subscription_status} if subscription_status else None)),
        )
        monkeypatch.setattr(
            "core.repositories.billing_repo.get_trial_daily_spend",
            AsyncMock(return_value=spent_microcents),
        )

    @staticmethod
    def _container(monkeypatch, row: dict | None):
        """Stub the container row the conversion guard reads."""
        monkeypatch.setattr(
            "core.repositories.container_repo.get_by_owner_id",
            AsyncMock(return_value=row),
        )
        monkeypatch.setattr(
            "core.repositories.container_repo.update_status",
            AsyncMock(return_value=row),
        )

    @pytest.mark.asyncio
    async def test_over_cap_trial_owner_is_refused(self, manager, mock_ecs_client, monkeypatch):
        """The whole point: a capped-out trial owner cannot wake their own
        container. ECS is never touched — the container stays down until the
        counter resets (UTC midnight) or the owner upgrades."""
        from core.services.trial_cap import CAP_MICROCENTS

        self._billing(monkeypatch, subscription_status="trial_local", spent_microcents=CAP_MICROCENTS)
        self._container(monkeypatch, _make_container_dict(status="stopped"))

        with patch.object(manager, "_await_running_transition", new_callable=AsyncMock):
            with pytest.raises(ContainerStartRefused) as exc:
                await manager.start_user_service("user_test_123")

        assert exc.value.reason == START_REFUSED_CAP
        mock_ecs_client.update_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_under_cap_trial_owner_starts(self, manager, mock_ecs_client, monkeypatch):
        """A trial owner UNDER the cap is a normal user: their container starts."""
        from core.services.trial_cap import CAP_MICROCENTS

        self._billing(monkeypatch, subscription_status="trial_local", spent_microcents=CAP_MICROCENTS - 1)
        self._container(monkeypatch, _make_container_dict(status="stopped"))

        with patch.object(manager, "_await_running_transition", new_callable=AsyncMock):
            await manager.start_user_service("user_test_123")

        mock_ecs_client.update_service.assert_called_once()

    @pytest.mark.asyncio
    async def test_paid_owner_starts_regardless_of_spend(self, manager, mock_ecs_client, monkeypatch):
        """The cap is a FREE-TIER cap. A paying owner is never subject to it,
        no matter what today's spend is — trial_block_reason self-gates on
        subscription_status == "trial_local"."""
        from core.services.trial_cap import CAP_MICROCENTS

        self._billing(monkeypatch, subscription_status="active", spent_microcents=CAP_MICROCENTS * 100)
        self._container(monkeypatch, _make_container_dict(status="stopped"))

        with patch.object(manager, "_await_running_transition", new_callable=AsyncMock):
            await manager.start_user_service("user_test_123")

        mock_ecs_client.update_service.assert_called_once()

    @pytest.mark.asyncio
    async def test_conversion_in_flight_is_refused(self, manager, mock_ecs_client, monkeypatch):
        """The corruption door. Note substatus is None, not "converting": the
        conversion's own ``stop_user_service`` writes update_status(owner,
        "stopped") — substatus defaults to None — so the label is GONE the
        instant the drain starts. Only the ``conversion_started_at`` timestamp
        survives, and it alone has to hold the start off for the whole drain."""
        row = _make_container_dict(status="stopped")
        row["substatus"] = None
        row["conversion_pending"] = True
        row["conversion_started_at"] = datetime.now(timezone.utc).isoformat()
        self._billing(monkeypatch, subscription_status="active")
        self._container(monkeypatch, row)

        with patch.object(manager, "_await_running_transition", new_callable=AsyncMock):
            with pytest.raises(ContainerStartRefused) as exc:
                await manager.start_user_service("user_test_123")

        assert exc.value.reason == START_REFUSED_CONVERTING
        mock_ecs_client.update_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_conversion_in_flight_starts(self, manager, mock_ecs_client, monkeypatch):
        """A container whose conversion stamp has lapsed (the reprovision is
        long done, or its task died) must not be wedged shut forever — the
        guard window is time-boxed for exactly this reason."""
        from core.services.stripe_webhook_handlers import CONVERSION_GUARD_SECONDS

        row = _make_container_dict(status="stopped")
        row["substatus"] = None
        row["conversion_started_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=CONVERSION_GUARD_SECONDS + 60)
        ).isoformat()
        self._billing(monkeypatch, subscription_status="active")
        self._container(monkeypatch, row)

        with patch.object(manager, "_await_running_transition", new_callable=AsyncMock):
            await manager.start_user_service("user_test_123")

        mock_ecs_client.update_service.assert_called_once()

    @pytest.mark.asyncio
    async def test_force_bypasses_both_guards(self, manager, mock_ecs_client, monkeypatch):
        """``force=True`` is the named escape hatch for SYSTEM and OPERATOR
        callers — provision_user_container's own restart (which IS the
        conversion's reprovision, so guarding it would deadlock it), the
        audited admin start, and the dev-only debug routes. Nothing a customer
        can reach passes it."""
        from core.services.trial_cap import CAP_MICROCENTS

        row = _make_container_dict(status="stopped")
        row["conversion_pending"] = True
        row["conversion_started_at"] = datetime.now(timezone.utc).isoformat()
        self._billing(monkeypatch, subscription_status="trial_local", spent_microcents=CAP_MICROCENTS)
        self._container(monkeypatch, row)

        with patch.object(manager, "_await_running_transition", new_callable=AsyncMock):
            await manager.start_user_service("user_test_123", force=True)

        mock_ecs_client.update_service.assert_called_once()

    @pytest.mark.asyncio
    async def test_refusal_is_not_an_ecs_error(self, manager, mock_ecs_client, monkeypatch):
        """A refusal is NOT a failure. Every ``except EcsManagerError`` in the
        codebase means "provisioning broke" — it 503s the caller and writes
        status="error" on the row. Neither is true here, so ContainerStartRefused
        must not be catchable as one."""
        from core.services.trial_cap import CAP_MICROCENTS

        self._billing(monkeypatch, subscription_status="trial_local", spent_microcents=CAP_MICROCENTS)
        self._container(monkeypatch, _make_container_dict(status="stopped"))

        with patch.object(manager, "_await_running_transition", new_callable=AsyncMock):
            with pytest.raises(ContainerStartRefused):
                try:
                    await manager.start_user_service("user_test_123")
                except EcsManagerError as e:  # pragma: no cover - the bug this guards
                    pytest.fail(f"a refusal must not be an EcsManagerError: {e!r}")

    @pytest.mark.asyncio
    async def test_provision_user_container_start_is_not_guarded(self, manager, mock_ecs_client, monkeypatch):
        """provision_user_container's own start MUST bypass the guards, or the
        trial->paid conversion deadlocks itself: the conversion stamps the row,
        THEN dispatches background_provision -> provision_user_container, which
        would find its own stamp and refuse to start the container it just
        reprovisioned."""
        from core.services.trial_cap import CAP_MICROCENTS

        row = _make_container_dict(status="stopped")
        row["conversion_pending"] = True
        row["conversion_started_at"] = datetime.now(timezone.utc).isoformat()
        self._billing(monkeypatch, subscription_status="trial_local", spent_microcents=CAP_MICROCENTS)
        self._container(monkeypatch, row)

        started = AsyncMock()
        with (
            patch("core.containers.ecs_manager.container_repo") as repo,
            patch.object(manager, "_service_exists", new=MagicMock(return_value=None)),
            patch.object(manager, "create_user_service", new=AsyncMock(return_value="svc")),
            patch.object(manager, "write_user_configs", new=AsyncMock()),
            patch.object(manager, "start_user_service", new=started),
            patch("core.containers.ecs_manager.put_metric"),
        ):
            repo.get_by_owner_id = AsyncMock(return_value=None)
            await manager.provision_user_container("user_test_123", provider_choice="bedrock_claude")

        started.assert_awaited_once_with("user_test_123", force=True)


# ---------------------------------------------------------------------------
# delete_user_service
# ---------------------------------------------------------------------------


class TestDeleteUserService:
    """Test service deletion with per-user resource cleanup."""

    @pytest.mark.asyncio
    async def test_delete_scales_then_deletes_with_cleanup(self, manager, mock_ecs_client, mock_efs_client):
        """delete_user_service scales to 0, deletes service, and cleans up per-user resources."""
        container_dict = _make_container_dict(
            status="running",
            access_point_id="fsap-user123",
            task_definition_arn="arn:aws:ecs:us-east-1:123456789:task-definition/openclaw:42",
        )
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=container_dict)
            mock_repo.delete = AsyncMock()

            await manager.delete_user_service("user_test_123")

            # Verify update_service (scale to 0) called first
            mock_ecs_client.update_service.assert_called_once_with(
                cluster=manager._cluster,
                service="openclaw-user_test_123-f4ae64abb2db",
                desiredCount=0,
            )
            # Verify delete_service called
            mock_ecs_client.delete_service.assert_called_once_with(
                cluster=manager._cluster,
                service="openclaw-user_test_123-f4ae64abb2db",
                force=True,
            )
            # Verify per-user task definition deregistered
            mock_ecs_client.deregister_task_definition.assert_called_once_with(
                taskDefinition="arn:aws:ecs:us-east-1:123456789:task-definition/openclaw:42"
            )
            # Verify per-user access point deleted
            mock_efs_client.delete_access_point.assert_called_once_with(AccessPointId="fsap-user123")
            # Verify repo record deleted
            mock_repo.delete.assert_called_once_with("user_test_123")

    @pytest.mark.asyncio
    async def test_delete_no_db_record(self, manager, mock_ecs_client, mock_efs_client):
        """delete_user_service with no repo record still deletes ECS service."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)
            mock_repo.delete = AsyncMock()

            await manager.delete_user_service("user_test_123")

            mock_ecs_client.update_service.assert_called_once()
            mock_ecs_client.delete_service.assert_called_once()
            # No per-user resources to clean up
            mock_ecs_client.deregister_task_definition.assert_not_called()
            mock_efs_client.delete_access_point.assert_not_called()
            mock_repo.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_without_per_user_resources(self, manager, mock_ecs_client, mock_efs_client):
        """delete_user_service skips cleanup when container has no per-user resources."""
        container_dict = _make_container_dict(status="running", access_point_id=None)
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=container_dict)
            mock_repo.delete = AsyncMock()

            await manager.delete_user_service("user_test_123")

            # ECS service still deleted
            mock_ecs_client.delete_service.assert_called_once()
            # No per-user cleanup
            mock_ecs_client.deregister_task_definition.assert_not_called()
            mock_efs_client.delete_access_point.assert_not_called()
            # Repo record still deleted
            mock_repo.delete.assert_called_once_with("user_test_123")

    @pytest.mark.asyncio
    async def test_delete_ecs_failure_raises(self, manager, mock_ecs_client):
        """A non-idempotent ECS API failure raises EcsManagerError."""
        mock_ecs_client.update_service.side_effect = ClientError(
            {"Error": {"Code": "ClusterNotFoundException", "Message": "no cluster"}},
            "UpdateService",
        )

        with pytest.raises(EcsManagerError, match="Failed to delete ECS service"):
            await manager.delete_user_service("user_test_123")

    @pytest.mark.asyncio
    async def test_delete_service_not_found_is_idempotent(self, manager, mock_ecs_client):
        """ServiceNotFoundException is treated as idempotent success but the
        function continues into per-user resource cleanup (task-def, EFS
        access point, container row). Service-gone but resources orphaned
        is a real partial-failure state we need to recover from."""
        mock_ecs_client.update_service.side_effect = ClientError(
            {"Error": {"Code": "ServiceNotFoundException", "Message": "not found"}},
            "UpdateService",
        )

        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            # Container row still exists with an orphaned access point —
            # mirrors the partial-cleanup state the early-return bug created.
            mock_repo.get_by_owner_id = AsyncMock(
                return_value={
                    "owner_id": "user_test_123",
                    "access_point_id": "fsap-orphaned",
                    "task_definition_arn": "arn:aws:ecs:...:task-definition/foo:5",
                }
            )
            mock_repo.delete = AsyncMock()

            # Must NOT raise even though the service is gone.
            await manager.delete_user_service("user_test_123")

            # Service delete itself isn't called (already gone).
            mock_ecs_client.delete_service.assert_not_called()
            # But the per-user cleanup STILL runs — this is the load-bearing fix.
            mock_repo.delete.assert_called_once_with("user_test_123")


# ---------------------------------------------------------------------------
# discover_ip
# ---------------------------------------------------------------------------


class TestDiscoverIp:
    """Test Cloud Map (DiscoverInstances) task IP discovery."""

    def test_returns_ip(self, manager, mock_servicediscovery_client):
        """discover_ip returns the instance IP for the matching tenant."""
        mock_servicediscovery_client.discover_instances.return_value = {
            "Instances": [
                {
                    "InstanceId": "task-abc",
                    "Attributes": {
                        "ECS_SERVICE_NAME": "openclaw-abc123def4",
                        "AWS_INSTANCE_IPV4": "10.0.1.42",
                    },
                }
            ]
        }

        ip = manager.discover_ip("openclaw-abc123def4")

        assert ip == "10.0.1.42"
        mock_servicediscovery_client.discover_instances.assert_called_once_with(
            NamespaceName="isol8-dev.local",
            ServiceName="openclaw-containers",
            QueryParameters={"ECS_SERVICE_NAME": "openclaw-abc123def4"},
            HealthStatus="HEALTHY_OR_ELSE_ALL",
        )

    def test_returns_none_when_no_instances(self, manager, mock_servicediscovery_client):
        """No instance for the tenant -> None."""
        mock_servicediscovery_client.discover_instances.return_value = {"Instances": []}
        assert manager.discover_ip("openclaw-abc123def4") is None

    def test_returns_none_on_error(self, manager, mock_servicediscovery_client):
        """SDK error -> None (caller treats as not-ready)."""
        mock_servicediscovery_client.discover_instances.side_effect = ClientError(
            {"Error": {"Code": "ServiceNotFound", "Message": "x"}},
            "DiscoverInstances",
        )
        assert manager.discover_ip("openclaw-abc123def4") is None

    def test_ignores_instances_for_other_tenants(self, manager, mock_servicediscovery_client):
        """Defensive client-side filter: never return another tenant's IP."""
        mock_servicediscovery_client.discover_instances.return_value = {
            "Instances": [{"Attributes": {"ECS_SERVICE_NAME": "openclaw-other", "AWS_INSTANCE_IPV4": "10.0.9.9"}}]
        }
        assert manager.discover_ip("openclaw-abc123def4") is None

    def test_namespace_not_configured_returns_none(self, manager, mock_servicediscovery_client):
        """Missing Cloud Map names -> None, no SDK call."""
        manager._cloud_map_namespace_name = ""
        assert manager.discover_ip("openclaw-abc123def4") is None
        mock_servicediscovery_client.discover_instances.assert_not_called()


# ---------------------------------------------------------------------------
# is_healthy
# ---------------------------------------------------------------------------


class TestIsHealthy:
    """Test gateway health checks via TCP socket."""

    def test_healthy_connection_succeeds(self, manager):
        """Successful TCP connection means healthy."""
        with patch("core.containers.ecs_manager.socket.create_connection") as mock_conn:
            mock_socket = MagicMock()
            mock_conn.return_value.__enter__ = MagicMock(return_value=mock_socket)
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)

            assert manager.is_healthy("10.0.1.42") is True
            mock_conn.assert_called_once_with(("10.0.1.42", GATEWAY_PORT), timeout=5)

    def test_unhealthy_connection_refused(self, manager):
        """Connection refused means unhealthy."""
        with patch("core.containers.ecs_manager.socket.create_connection") as mock_conn:
            mock_conn.side_effect = ConnectionRefusedError("refused")

            assert manager.is_healthy("10.0.1.42") is False

    def test_unhealthy_timeout(self, manager):
        """Timeout means unhealthy."""
        import socket

        with patch("core.containers.ecs_manager.socket.create_connection") as mock_conn:
            mock_conn.side_effect = socket.timeout("timed out")

            assert manager.is_healthy("10.0.1.42") is False


# ---------------------------------------------------------------------------
# resolve_running_container
# ---------------------------------------------------------------------------


class TestResolveRunningContainer:
    """Test container resolution with auto-transition."""

    @pytest.mark.asyncio
    async def test_returns_container_and_ip(self, manager):
        """Returns container dict and IP for a running container."""
        container_dict = _make_container_dict(status="running", service_name="openclaw-user_test_123-f4ae64abb2db")
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=container_dict)
            manager.discover_ip = MagicMock(return_value="10.0.1.42")

            container, ip = await manager.resolve_running_container("user_test_123")

            assert container == container_dict
            assert ip == "10.0.1.42"

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_container(self, manager):
        """Returns (None, None) when no container exists."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)

            container, ip = await manager.resolve_running_container("user_test_123")

            assert container is None
            assert ip is None

    @pytest.mark.asyncio
    async def test_returns_none_for_stopped_container(self, manager):
        """Returns (None, None) when container status is not provisioning/running."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="stopped"))

            container, ip = await manager.resolve_running_container("user_test_123")

            assert container is None
            assert ip is None

    @pytest.mark.asyncio
    async def test_auto_transitions_provisioning_to_running(self, manager):
        """Provisioning container transitions to running when healthy."""
        container_dict = _make_container_dict(status="provisioning", service_name="openclaw-user_test_123-f4ae64abb2db")
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=container_dict)
            mock_repo.update_fields = AsyncMock(return_value=container_dict)
            manager.discover_ip = MagicMock(return_value="10.0.1.42")
            manager.is_healthy = MagicMock(return_value=True)

            container, ip = await manager.resolve_running_container("user_test_123")

            assert container["status"] == "running"
            assert ip == "10.0.1.42"
            mock_repo.update_fields.assert_called_once()


# ---------------------------------------------------------------------------
# get_service_status
# ---------------------------------------------------------------------------


class TestGetServiceStatus:
    """Test repo status lookup."""

    @pytest.mark.asyncio
    async def test_returns_container(self, manager):
        """get_service_status returns the container dict."""
        container_dict = _make_container_dict(status="running")
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=container_dict)

            result = await manager.get_service_status("user_test_123")
            assert result == container_dict
            assert result["status"] == "running"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, manager):
        """get_service_status returns None when no record exists."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)

            result = await manager.get_service_status("user_nonexistent")
            assert result is None


# ---------------------------------------------------------------------------
# EcsManagerError
# ---------------------------------------------------------------------------


class TestEcsManagerError:
    """Test custom exception."""

    def test_error_with_user_id(self):
        """EcsManagerError stores user_id."""
        err = EcsManagerError("something failed", user_id="user_123")
        assert str(err) == "something failed"
        assert err.user_id == "user_123"

    def test_error_without_user_id(self):
        """EcsManagerError defaults user_id to empty string."""
        err = EcsManagerError("generic failure")
        assert err.user_id == ""


# ---------------------------------------------------------------------------
# _await_running_transition (durable provisioning -> running poller)
# ---------------------------------------------------------------------------


class TestAwaitRunningTransition:
    """The poller that drives provisioning -> running in the background.

    Must be durable (no fixed timeout) and have proper exit conditions so a
    container can never be left stuck at status=provisioning forever while
    actually running in ECS.
    """

    @pytest.mark.asyncio
    async def test_transitions_to_running_when_task_healthy(self, manager, mock_ecs_client):
        """Container becomes reachable -> write status=running and exit."""
        mock_ecs_client.list_tasks.return_value = {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/abc"]}
        mock_ecs_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "lastStatus": "RUNNING",
                    "startedBy": "ecs-svc/primary",
                    "attachments": [
                        {
                            "type": "ElasticNetworkInterface",
                            "details": [{"name": "privateIPv4Address", "value": "10.0.1.42"}],
                        }
                    ],
                }
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "is_healthy", return_value=True),
            patch("core.containers.ecs_manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock()

            await manager._await_running_transition("user_test_123")

            mock_repo.update_fields.assert_called_once()
            fields = mock_repo.update_fields.call_args.args[1]
            assert fields["status"] == "running"
            assert fields["substatus"] == "gateway_healthy"
            assert fields["task_arn"] == "arn:aws:ecs:us-east-1:123:task/cluster/abc"

    @pytest.mark.asyncio
    async def test_transitions_to_error_when_circuit_breaker_trips(self, manager, mock_ecs_client):
        """ECS deployment rolloutState=FAILED -> write status=error and exit.

        This is the definitive failure signal: the circuit breaker only trips
        after N failed task placements, so we know the provision will never
        succeed on its own (bad image, crash loop, etc.)."""
        mock_ecs_client.list_tasks.return_value = {"taskArns": []}
        mock_ecs_client.describe_services.return_value = {
            "services": [{"deployments": [{"id": "ecs-svc/primary", "status": "PRIMARY", "rolloutState": "FAILED"}]}]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch("core.containers.ecs_manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock()

            await manager._await_running_transition("user_test_123")

            mock_repo.update_fields.assert_called_once()
            fields = mock_repo.update_fields.call_args.args[1]
            assert fields["status"] == "error"

    @pytest.mark.asyncio
    async def test_exits_silently_when_ddb_status_changed_externally(self, manager, mock_ecs_client):
        """If another actor (admin, reaper, re-provision) changed the DDB status
        under us, exit without writing anything -- our job is done or obsolete."""
        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch("core.containers.ecs_manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="stopped"))
            mock_repo.update_fields = AsyncMock()

            await manager._await_running_transition("user_test_123")

            mock_repo.update_fields.assert_not_called()

    @pytest.mark.asyncio
    async def test_exits_silently_when_row_missing(self, manager, mock_ecs_client):
        """Container row deleted -> exit. No row to transition."""
        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch("core.containers.ecs_manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)
            mock_repo.update_fields = AsyncMock()

            await manager._await_running_transition("user_test_123")

            mock_repo.update_fields.assert_not_called()

    @pytest.mark.asyncio
    async def test_keeps_polling_when_task_not_yet_healthy(self, manager, mock_ecs_client):
        """On iteration N no running task, on iteration N+1 it's healthy -> transition.

        Regression for the 120s timeout bug: a container that takes >2 minutes
        to become reachable MUST still get transitioned when it eventually is."""
        # First poll: no tasks. Second poll: a healthy task.
        mock_ecs_client.list_tasks.side_effect = [
            {"taskArns": []},
            {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/abc"]},
        ]
        mock_ecs_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "lastStatus": "RUNNING",
                    "startedBy": "ecs-svc/primary",
                    "attachments": [
                        {
                            "type": "ElasticNetworkInterface",
                            "details": [{"name": "privateIPv4Address", "value": "10.0.1.42"}],
                        }
                    ],
                }
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "is_healthy", return_value=True),
            patch("core.containers.ecs_manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock()

            await manager._await_running_transition("user_test_123")

            mock_repo.update_fields.assert_called_once()
            fields = mock_repo.update_fields.call_args.args[1]
            assert fields["status"] == "running"

    @pytest.mark.asyncio
    async def test_one_describe_services_per_iteration_on_happy_path(self, manager, mock_ecs_client):
        """describe_services consolidates two concerns (primary-deployment
        filter + rolloutState failure detection) so a happy-path transition
        costs one describe_services + one list_tasks + one describe_tasks."""
        mock_ecs_client.list_tasks.return_value = {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/abc"]}
        mock_ecs_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "lastStatus": "RUNNING",
                    "startedBy": "ecs-svc/primary",
                    "attachments": [
                        {
                            "type": "ElasticNetworkInterface",
                            "details": [{"name": "privateIPv4Address", "value": "10.0.1.42"}],
                        }
                    ],
                }
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "is_healthy", return_value=True),
            patch("core.containers.ecs_manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock()

            await manager._await_running_transition("user_test_123")

            assert mock_ecs_client.describe_services.call_count == 1

    @pytest.mark.asyncio
    async def test_filters_running_tasks_by_primary_deployment(self, manager, mock_ecs_client):
        """During a forced redeploy, the OLD task is still RUNNING while the
        NEW deployment rolls out. _poll_running_task MUST only consider tasks
        from the current PRIMARY deployment -- picking the old task would
        flip status=running using the pre-deploy task_arn and mask a failing
        rollout.

        Filter mechanism: pass startedBy=<primary-deployment-id> to list_tasks.
        ECS tags service-launched tasks with startedBy=ecs-svc/<deployment-id>.
        """
        # describe_services returns one PRIMARY and one ACTIVE (draining) deployment.
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "deployments": [
                        {
                            "id": "ecs-svc/new",
                            "status": "PRIMARY",
                            "rolloutState": "IN_PROGRESS",
                        },
                        {
                            "id": "ecs-svc/old",
                            "status": "ACTIVE",
                            "rolloutState": "COMPLETED",
                        },
                    ]
                }
            ]
        }

        # list_tasks should be called with startedBy pointing at the PRIMARY.
        mock_ecs_client.list_tasks.return_value = {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/new-task"]}
        mock_ecs_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "lastStatus": "RUNNING",
                    "startedBy": "ecs-svc/new",
                    "attachments": [
                        {
                            "type": "ElasticNetworkInterface",
                            "details": [{"name": "privateIPv4Address", "value": "10.0.1.99"}],
                        }
                    ],
                }
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "is_healthy", return_value=True),
            patch("core.containers.ecs_manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock()

            await manager._await_running_transition("user_test_123")

            # list_tasks must NOT pass startedBy; AWS rejects it combined with
            # serviceName. Filtering is done in-code using describe_tasks.
            list_kwargs = mock_ecs_client.list_tasks.call_args.kwargs
            assert "startedBy" not in list_kwargs, (
                "list_tasks must not pass startedBy; AWS rejects it combined with serviceName."
            )

            # With the in-code filter correctly applied, the new task's ARN is recorded.
            fields = mock_repo.update_fields.call_args.args[1]
            assert fields["task_arn"] == "arn:aws:ecs:us-east-1:123:task/cluster/new-task"

    @pytest.mark.asyncio
    async def test_list_tasks_does_not_pass_startedby(self, manager, mock_ecs_client):
        """ECS ListTasks rejects startedBy combined with serviceName:
        `InvalidParameterException: cannot specify startedBy with other
        arguments`. We must filter by deployment-id in-code (from
        describe_tasks' startedBy field), not via the API filter."""
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "deployments": [
                        {
                            "id": "ecs-svc/new",
                            "status": "PRIMARY",
                            "rolloutState": "IN_PROGRESS",
                        },
                    ]
                }
            ]
        }
        mock_ecs_client.list_tasks.return_value = {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/new-task"]}
        mock_ecs_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "lastStatus": "RUNNING",
                    "startedBy": "ecs-svc/new",
                    "attachments": [
                        {
                            "type": "ElasticNetworkInterface",
                            "details": [{"name": "privateIPv4Address", "value": "10.0.1.99"}],
                        }
                    ],
                }
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "is_healthy", return_value=True),
            patch("core.containers.ecs_manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock()

            await manager._await_running_transition("user_test_123")

            # list_tasks must NOT pass startedBy (AWS rejects it).
            list_kwargs = mock_ecs_client.list_tasks.call_args.kwargs
            assert "startedBy" not in list_kwargs, (
                f"list_tasks must not pass startedBy; AWS rejects it combined "
                f"with serviceName. Got kwargs: {list_kwargs}"
            )

    @pytest.mark.asyncio
    async def test_ignores_old_deployment_tasks_via_in_code_filter(self, manager, mock_ecs_client):
        """During a rollout overlap, list_tasks returns both the OLD
        drain-phase task and the NEW task. Filter in-code using each task's
        startedBy from describe_tasks so we ignore the old task."""
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "deployments": [
                        {
                            "id": "ecs-svc/new",
                            "status": "PRIMARY",
                            "rolloutState": "IN_PROGRESS",
                        },
                    ]
                }
            ]
        }
        # Two running tasks come back; old one listed first.
        mock_ecs_client.list_tasks.return_value = {
            "taskArns": [
                "arn:aws:ecs:us-east-1:123:task/cluster/old-task",
                "arn:aws:ecs:us-east-1:123:task/cluster/new-task",
            ]
        }
        mock_ecs_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": "arn:aws:ecs:us-east-1:123:task/cluster/old-task",
                    "lastStatus": "RUNNING",
                    "startedBy": "ecs-svc/old",
                    "attachments": [
                        {
                            "type": "ElasticNetworkInterface",
                            "details": [{"name": "privateIPv4Address", "value": "10.0.1.1"}],
                        }
                    ],
                },
                {
                    "taskArn": "arn:aws:ecs:us-east-1:123:task/cluster/new-task",
                    "lastStatus": "RUNNING",
                    "startedBy": "ecs-svc/new",
                    "attachments": [
                        {
                            "type": "ElasticNetworkInterface",
                            "details": [{"name": "privateIPv4Address", "value": "10.0.1.2"}],
                        }
                    ],
                },
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "is_healthy", return_value=True),
            patch("core.containers.ecs_manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock()

            await manager._await_running_transition("user_test_123")

            # We must have transitioned using the NEW task, not the OLD one.
            fields = mock_repo.update_fields.call_args.args[1]
            assert fields["task_arn"] == "arn:aws:ecs:us-east-1:123:task/cluster/new-task"

    @pytest.mark.asyncio
    async def test_no_primary_deployment_keeps_polling(self, manager, mock_ecs_client):
        """If describe_services returns no PRIMARY deployment (transient ECS
        state during deployment churn), the poller should just sleep and
        retry next iteration -- not write status=running or status=error."""
        mock_ecs_client.describe_services.return_value = {"services": [{"deployments": []}]}
        mock_ecs_client.list_tasks.return_value = {"taskArns": []}

        # Cancel after one sleep so the loop exits.
        import asyncio as _asyncio

        sleep_calls = [0]

        async def count_and_cancel(*args, **kwargs):
            sleep_calls[0] += 1
            if sleep_calls[0] >= 1:
                raise _asyncio.CancelledError()

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch("core.containers.ecs_manager.asyncio.sleep", side_effect=count_and_cancel),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock()

            with pytest.raises(_asyncio.CancelledError):
                await manager._await_running_transition("user_test_123")

            # No status transition written either direction.
            mock_repo.update_fields.assert_not_called()

    @pytest.mark.asyncio
    async def test_detects_circuit_breaker_via_primary_deployment_rollout_state(self, manager, mock_ecs_client):
        """rolloutState='FAILED' must be read from the PRIMARY deployment
        specifically, not from deployments[0] blindly. During a rollout,
        the ACTIVE (old) deployment can coexist with the new PRIMARY and
        the list order is not guaranteed."""
        import asyncio as _asyncio

        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "deployments": [
                        # Draining old deployment listed first.
                        {
                            "id": "ecs-svc/old",
                            "status": "ACTIVE",
                            "rolloutState": "COMPLETED",
                        },
                        # New PRIMARY failed.
                        {
                            "id": "ecs-svc/new",
                            "status": "PRIMARY",
                            "rolloutState": "FAILED",
                        },
                    ]
                }
            ]
        }
        mock_ecs_client.list_tasks.return_value = {"taskArns": []}

        # If the implementation incorrectly reads deployments[0] and misses
        # the FAILED PRIMARY, the loop would spin forever -- cancel after a
        # few sleeps so the test fails loudly instead of hanging.
        sleep_count = [0]

        async def cancel_after_a_few(*args, **kwargs):
            sleep_count[0] += 1
            if sleep_count[0] > 3:
                raise _asyncio.CancelledError()

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch("core.containers.ecs_manager.asyncio.sleep", side_effect=cancel_after_a_few),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock()

            # Expected: implementation finds FAILED PRIMARY -> writes error -> returns
            # without ever reaching the cancellation guard.
            await manager._await_running_transition("user_test_123")

            mock_repo.update_fields.assert_called_once()
            fields = mock_repo.update_fields.call_args.args[1]
            assert fields["status"] == "error"

    @pytest.mark.asyncio
    async def test_respects_cancellation(self, manager, mock_ecs_client):
        """Clean shutdown: if the event loop cancels the task (backend restart),
        the poller exits without raising into the caller and without writing
        a status transition."""
        import asyncio as _asyncio

        mock_ecs_client.list_tasks.return_value = {"taskArns": []}
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {"deployments": [{"id": "ecs-svc/primary", "status": "PRIMARY", "rolloutState": "IN_PROGRESS"}]}
            ]
        }

        # asyncio.sleep raises CancelledError inside the poller loop.
        async def cancel_on_sleep(*args, **kwargs):
            raise _asyncio.CancelledError()

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch("core.containers.ecs_manager.asyncio.sleep", side_effect=cancel_on_sleep),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock()

            # Must not swallow CancelledError -- asyncio task cancellation
            # semantics require it to propagate out.
            with pytest.raises(_asyncio.CancelledError):
                await manager._await_running_transition("user_test_123")

            mock_repo.update_fields.assert_not_called()

    @pytest.mark.asyncio
    async def test_spot_placement_timeout_flips_service_to_on_demand(self, manager, mock_ecs_client):
        """Zero placed tasks past SPOT_PLACEMENT_FALLBACK_S is the Spot
        capacity-unavailable signature (no tasks, no failures, no rollout
        change). The poller must flip the service to on-demand exactly once
        and keep polling; the flip passes no desiredCount so it can never
        restart a concurrently-stopped service."""
        mock_ecs_client.list_tasks.return_value = {"taskArns": []}
        rows = [
            _make_container_dict(status="provisioning"),
            _make_container_dict(status="provisioning"),
            _make_container_dict(status="stopped"),
        ]
        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch("core.containers.ecs_manager.asyncio.sleep", new_callable=AsyncMock),
            patch("core.containers.ecs_manager.time.monotonic", side_effect=[0.0, 301.0]),
        ):
            mock_repo.get_by_owner_id = AsyncMock(side_effect=rows)
            mock_repo.update_fields = AsyncMock()
            manager._get_primary_deployment = AsyncMock(
                return_value={"id": "ecs-svc/primary", "rolloutState": "IN_PROGRESS"}
            )

            await manager._await_running_transition("user_test_123", interval_s=0)

        mock_ecs_client.update_service.assert_called_once()
        kwargs = mock_ecs_client.update_service.call_args.kwargs
        assert kwargs["capacityProviderStrategy"] == [{"capacityProvider": "FARGATE", "weight": 1}]
        assert kwargs["forceNewDeployment"] is True
        assert "desiredCount" not in kwargs

    @pytest.mark.asyncio
    async def test_pending_task_pulling_image_never_triggers_spot_fallback(self, manager, mock_ecs_client):
        """A PENDING task (multi-GB image pull, minutes long) counts as
        placed — the fallback timer must never even start, no matter how
        long the pull takes. Only a service ECS has placed NOTHING into
        may be flipped."""
        mock_ecs_client.list_tasks.return_value = {"taskArns": ["arn:aws:ecs:us-east-1:123:task/cluster/abc"]}
        mock_ecs_client.describe_tasks.return_value = {
            "tasks": [{"lastStatus": "PENDING", "startedBy": "ecs-svc/primary", "attachments": []}]
        }
        rows = [
            _make_container_dict(status="provisioning"),
            _make_container_dict(status="provisioning"),
            _make_container_dict(status="stopped"),
        ]
        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch("core.containers.ecs_manager.asyncio.sleep", new_callable=AsyncMock),
            patch(
                "core.containers.ecs_manager.time.monotonic",
                side_effect=AssertionError("fallback timer must not run while a task is placed"),
            ),
        ):
            mock_repo.get_by_owner_id = AsyncMock(side_effect=rows)
            mock_repo.update_fields = AsyncMock()
            manager._get_primary_deployment = AsyncMock(
                return_value={"id": "ecs-svc/primary", "rolloutState": "IN_PROGRESS"}
            )

            await manager._await_running_transition("user_test_123", interval_s=0)

        mock_ecs_client.update_service.assert_not_called()


# ---------------------------------------------------------------------------
# resize_user_container (per-user CPU/memory/image update path)
# ---------------------------------------------------------------------------


class TestResizeUserContainer:
    """Tests resize path fires the transition poller.

    resize writes status=provisioning via update_fields + ECS forceNewDeployment.
    Without firing the poller, the row stays stuck at provisioning until the
    next backend restart catches it via the startup reconciler."""

    @pytest.mark.asyncio
    async def test_resize_reads_env_from_base_not_current(self, manager, mock_ecs_client):
        """resize must read containerDefinitions from the CDK-managed base
        family, NOT from the user's prior per-user revision.

        Regression: if the resize path reads from the user's own task def
        any env-var drift propagates forever. Read the base FAMILY live —
        per-user clones live in `<base>-user`, so the base family always
        returns the latest CDK base.
        """
        # Base task def has CLAWHUB_WORKDIR. User's *prior* per-user task def
        # does NOT (simulating tonight's incident). Resize must produce an env
        # that mirrors the base, not the user's drifted prior state.
        mock_ecs_client.describe_task_definition.return_value = {
            "taskDefinition": {
                "family": "isol8-dev-openclaw",
                "taskRoleArn": "",
                "executionRoleArn": "",
                "networkMode": "awsvpc",
                "containerDefinitions": [
                    {
                        "name": "openclaw",
                        "image": "alpine/openclaw:latest",
                        "environment": [
                            {"name": "HOME", "value": "/home/node"},
                            {"name": "CLAWHUB_WORKDIR", "value": "/home/node/.openclaw"},
                        ],
                    }
                ],
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
                "cpu": "256",
                "memory": "512",
            }
        }
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "openclaw-user_test_123-f4ae64abb2db",
                    "status": "ACTIVE",
                    "desiredCount": 1,
                    "runningCount": 1,
                    "deployments": [{"id": "ecs-svc/primary", "status": "PRIMARY", "rolloutState": "IN_PROGRESS"}],
                }
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch("core.containers.ecs_manager.asyncio.to_thread") as mock_to_thread,
        ):

            async def passthrough(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_to_thread.side_effect = passthrough
            mock_repo.get_by_owner_id = AsyncMock(
                return_value=_make_container_dict(status="running", access_point_id="fsap-user-789")
            )
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict(status="provisioning"))

            await manager.resize_user_container("user_test_123", new_cpu="1024", new_memory="2048")

            call_kwargs = mock_ecs_client.register_task_definition.call_args.kwargs
            env = {e["name"]: e["value"] for e in call_kwargs["containerDefinitions"][0]["environment"]}
            assert env["CLAWHUB_WORKDIR"] == "/home/node/.openclaw"

            # Per-user state correctly layered on top of base
            volumes = call_kwargs["volumes"]
            assert volumes[0]["efsVolumeConfiguration"]["authorizationConfig"]["accessPointId"] == "fsap-user-789"
            assert call_kwargs["cpu"] == "1024"
            assert call_kwargs["memory"] == "2048"

    @pytest.mark.asyncio
    async def test_resize_fires_running_transition_poller(self, manager, mock_ecs_client):
        # Service is currently running with tasks -- resize should fire the
        # poller to drive tasks through the rolling replacement.
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "openclaw-user_test_123-f4ae64abb2db",
                    "status": "ACTIVE",
                    "desiredCount": 1,
                    "runningCount": 1,
                    "deployments": [{"id": "ecs-svc/primary", "status": "PRIMARY", "rolloutState": "IN_PROGRESS"}],
                }
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock) as mock_await,
        ):
            mock_repo.get_by_owner_id = AsyncMock(
                return_value=_make_container_dict(
                    status="running",
                    task_definition_arn="arn:aws:ecs:us-east-1:123:task-definition/base:1",
                )
            )
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict(status="provisioning"))

            await manager.resize_user_container("user_test_123", new_cpu="1024", new_memory="2048")

            import asyncio as _asyncio

            await _asyncio.sleep(0)

            mock_await.assert_called_once_with("user_test_123")

    @pytest.mark.asyncio
    async def test_resize_enables_circuit_breaker_on_update_service(self, manager, mock_ecs_client):
        """resize path's update_service must also carry the circuit breaker
        so pre-existing services get upgraded on resize."""
        # Service running with tasks so we exercise the full flip+poller path.
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "openclaw-user_test_123-f4ae64abb2db",
                    "status": "ACTIVE",
                    "desiredCount": 1,
                    "runningCount": 1,
                    "deployments": [{"id": "ecs-svc/primary", "status": "PRIMARY", "rolloutState": "IN_PROGRESS"}],
                }
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch("core.containers.ecs_manager.asyncio.to_thread") as mock_to_thread,
        ):
            # asyncio.to_thread runs the sync boto3 call in a thread;
            # intercept it so we can inspect kwargs.
            async def passthrough(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_to_thread.side_effect = passthrough

            mock_repo.get_by_owner_id = AsyncMock(
                return_value=_make_container_dict(
                    status="running",
                    task_definition_arn="arn:aws:ecs:us-east-1:123:task-definition/base:1",
                )
            )
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict(status="provisioning"))

            await manager.resize_user_container("user_test_123", new_cpu="1024", new_memory="2048")

            # resize issues two to_thread calls -- register_task_definition
            # and update_service. Find the update_service call.
            update_calls = [
                call
                for call in mock_to_thread.call_args_list
                if getattr(call.args[0], "__name__", "") == "update_service"
                or (call.args and call.args[0] == mock_ecs_client.update_service)
            ]
            assert update_calls, "Expected update_service to be called via asyncio.to_thread"
            call_kwargs = update_calls[0].kwargs
            dc = call_kwargs.get("deploymentConfiguration") or {}
            cb = dc.get("deploymentCircuitBreaker") or {}
            assert cb.get("enable") is True
            assert cb.get("rollback") is False

    @pytest.mark.asyncio
    async def test_resize_skips_status_flip_and_poller_when_service_stopped(self, manager, mock_ecs_client):
        """If the service is at desiredCount=0 when resize runs, there are no
        tasks to become healthy. Flipping status=provisioning and firing the
        poller in that state leaves an orphan poller that polls forever.

        Instead: record the new task_definition_arn but leave status alone;
        the new task def takes effect on next start_user_service call."""
        # Service currently stopped.
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "openclaw-user_test_123-f4ae64abb2db",
                    "status": "ACTIVE",
                    "desiredCount": 0,
                    "runningCount": 0,
                    "deployments": [{"id": "ecs-svc/primary", "status": "PRIMARY", "rolloutState": "COMPLETED"}],
                }
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock) as mock_await,
            patch("core.containers.ecs_manager.asyncio.to_thread") as mock_to_thread,
        ):

            async def passthrough(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_to_thread.side_effect = passthrough

            mock_repo.get_by_owner_id = AsyncMock(
                return_value=_make_container_dict(
                    status="stopped",
                    task_definition_arn="arn:aws:ecs:us-east-1:123:task-definition/base:1",
                )
            )
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict(status="stopped"))

            await manager.resize_user_container("user_test_123", new_cpu="1024", new_memory="2048")

            import asyncio as _asyncio

            await _asyncio.sleep(0)

            # No poller should have been fired -- there would be nothing to poll.
            mock_await.assert_not_called()

            # task_definition_arn should be updated, but status must NOT flip
            # to provisioning for a stopped service.
            update_calls = mock_repo.update_fields.call_args_list
            for call in update_calls:
                fields = call.args[1] if len(call.args) > 1 else call.kwargs.get("fields", {})
                assert fields.get("status") != "provisioning", (
                    f"Must not flip status to provisioning on a stopped service; got fields={fields}"
                )

    @pytest.mark.asyncio
    async def test_resize_fires_poller_when_service_running(self, manager, mock_ecs_client):
        """When the service has desiredCount>0, resize forces a new deployment
        AND there are running tasks to be replaced, so we do need the poller
        to drive the provisioning -> running transition. Confirm the old
        behavior still holds for running services."""
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "openclaw-user_test_123-f4ae64abb2db",
                    "status": "ACTIVE",
                    "desiredCount": 1,
                    "runningCount": 1,
                    "deployments": [{"id": "ecs-svc/primary", "status": "PRIMARY", "rolloutState": "IN_PROGRESS"}],
                }
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock) as mock_await,
            patch("core.containers.ecs_manager.asyncio.to_thread") as mock_to_thread,
        ):

            async def passthrough(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_to_thread.side_effect = passthrough

            mock_repo.get_by_owner_id = AsyncMock(
                return_value=_make_container_dict(
                    status="running",
                    task_definition_arn="arn:aws:ecs:us-east-1:123:task-definition/base:1",
                )
            )
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict(status="provisioning"))

            await manager.resize_user_container("user_test_123", new_cpu="1024", new_memory="2048")

            import asyncio as _asyncio

            await _asyncio.sleep(0)

            mock_await.assert_called_once_with("user_test_123")


# ---------------------------------------------------------------------------
# provision_user_container (full provisioning flow + recovery branches)
# ---------------------------------------------------------------------------


class TestProvisionUserContainer:
    """Tests for the recovery branches of provision_user_container that set
    status=provisioning on an existing service row. Both branches must fire
    the transition poller so the row drains back to status=running without
    relying on the startup reconciler."""

    @pytest.mark.asyncio
    async def test_provision_redeploying_branch_fires_poller(self, manager, mock_ecs_client):
        """When provision is called and the ECS service is already running,
        the redeploying branch forces a new deployment and writes
        status=provisioning. It MUST fire the poller so the row transitions
        back to status=running once the new task is healthy."""
        # Service exists with desired=1, running=1 -> redeploying branch.
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "openclaw-user_test_123-f4ae64abb2db",
                    "status": "ACTIVE",
                    "desiredCount": 1,
                    "runningCount": 1,
                }
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock) as mock_await,
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="running"))
            mock_repo.update_fields = AsyncMock()

            await manager.provision_user_container("user_test_123", provider_choice="bedrock_claude")

            import asyncio as _asyncio

            await _asyncio.sleep(0)

            mock_await.assert_called_once_with("user_test_123")

    @pytest.mark.asyncio
    async def test_provision_ecs_starting_branch_fires_poller(self, manager, mock_ecs_client):
        """When provision is called and ECS is mid-launch (desired=1, running=0),
        the 'ECS is starting' branch writes status=provisioning and returns
        without taking ECS action. It MUST fire the poller to drive the
        transition once the task becomes healthy."""
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "openclaw-user_test_123-f4ae64abb2db",
                    "status": "ACTIVE",
                    "desiredCount": 1,
                    "runningCount": 0,
                }
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock) as mock_await,
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="stopped"))
            mock_repo.update_fields = AsyncMock()

            await manager.provision_user_container("user_test_123", provider_choice="bedrock_claude")

            import asyncio as _asyncio

            await _asyncio.sleep(0)

            mock_await.assert_called_once_with("user_test_123")

    @pytest.mark.asyncio
    async def test_provision_full_flow_fires_poller_exactly_once(self, manager, mock_ecs_client, mock_efs_client):
        """Full-provision path (no existing service) must fire
        _await_running_transition EXACTLY ONCE, not twice.

        start_user_service fires one poller. A second poller fired by
        provision_user_container at the end would be duplicate work --
        two identical long-lived tasks doing the same
        list_tasks/describe_services polling and racing to write the DDB
        transition. Keep only one."""

        # Make _service_exists return None so we go through the full-provisioning path.
        mock_ecs_client.describe_services.return_value = {"services": []}

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock) as mock_await,
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_status = AsyncMock(return_value=_make_container_dict(status="provisioning"))

            await manager.provision_user_container("user_test_123", provider_choice="bedrock_claude")

            # Let any fire-and-forget tasks be scheduled.
            import asyncio as _asyncio

            await _asyncio.sleep(0)

            # Exactly one poller, not two.
            assert mock_await.await_count == 1, (
                f"Expected exactly 1 poller, got {mock_await.await_count}. "
                "start_user_service fires the poller; the outer create_task "
                "at the end of provision_user_container is redundant."
            )
            mock_await.assert_called_with("user_test_123")

    @pytest.mark.asyncio
    async def test_redeploying_branch_enables_circuit_breaker(self, manager, mock_ecs_client):
        """The redeploying branch (desired=1, running>0) issues an
        update_service(forceNewDeployment=True). That call must carry the
        circuit breaker so pre-existing services get upgraded mid-flight."""
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "openclaw-user_test_123-f4ae64abb2db",
                    "status": "ACTIVE",
                    "desiredCount": 1,
                    "runningCount": 1,
                }
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="running"))
            mock_repo.update_fields = AsyncMock()

            await manager.provision_user_container("user_test_123", provider_choice="bedrock_claude")

            # update_service is called directly (not via asyncio.to_thread)
            # in the redeploying branch.
            call_kwargs = mock_ecs_client.update_service.call_args.kwargs
            dc = call_kwargs.get("deploymentConfiguration") or {}
            cb = dc.get("deploymentCircuitBreaker") or {}
            assert cb.get("enable") is True
            assert cb.get("rollback") is False

    @pytest.mark.asyncio
    async def test_provision_ecs_starting_no_poller_when_status_already_provisioning(self, manager, mock_ecs_client):
        """If the DDB status is already 'provisioning' when we hit the
        ECS-is-starting branch, we should NOT spawn another poller. An
        earlier one is already running (or the startup reconciler will
        pick up the row on next deploy). Spawning multiple pollers per
        owner wastes ECS API quota under a slow cold start with repeated
        /container/provision calls."""
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "openclaw-user_test_123-f4ae64abb2db",
                    "status": "ACTIVE",
                    "desiredCount": 1,
                    "runningCount": 0,
                }
            ]
        }

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock) as mock_await,
        ):
            # Row already at status=provisioning.
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock()

            await manager.provision_user_container("user_test_123", provider_choice="bedrock_claude")

            import asyncio as _asyncio

            await _asyncio.sleep(0)

            # No poller fired -- one's already running (or reconciler will catch it).
            mock_await.assert_not_called()
            # Also no DDB update (status already correct).
            mock_repo.update_fields.assert_not_called()

    # NOTE: pre-existing tier-resolution tests
    # (test_provision_resolves_tier_from_billing_when_not_passed,
    #  test_provision_defaults_to_free_when_no_billing_record,
    #  test_provision_falls_back_to_free_on_billing_lookup_error,
    #  test_provision_respects_explicit_tier_arg_over_billing,
    #  test_provision_produces_paid_tier_openclaw_config_for_starter [xfail])
    # were deleted with the flat-fee pivot — per-tier plan resolution is
    # gone. Coverage of the new shape lives below in
    # TestProvisionUserContainerProviderChoice and in
    # tests/unit/containers/test_config_provider_routing.py.


class TestProvisionUserContainerProviderChoice:
    """Tests for the provider_choice plumbing.

    Each provider_choice triggers a different pre-task setup:
      * chatgpt_oauth -> pre_stage_auth_profile_store runs BEFORE the ECS service
        is created so the codex auth.json is on EFS at first boot.
      * byo_key       -> per-user Secrets Manager ARN is added to the task
        definition's secrets[] block under OPENAI_API_KEY/ANTHROPIC_API_KEY.
      * bedrock_claude -> no extra setup; AWS creds come from the task IAM role.
    """

    @pytest.mark.asyncio
    async def test_provision_byo_key_attaches_secret_arn_to_task_def(self, manager, mock_ecs_client, mock_efs_client):
        """For byo_key provider, the per-user Secrets Manager ARN resolved by
        key_service.get_secret_arn lands in the registered task def's secrets[]
        block under OPENAI_API_KEY. The first task that boots reads the key
        from Secrets Manager via the task IAM role grant — no plaintext on
        disk."""
        mock_ecs_client.describe_services.return_value = {"services": []}
        secret_arn = "arn:aws:secretsmanager:us-east-1:123:secret:isol8/dev/user-keys/user_test_123/openai-AbCdE"

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch(
                "core.services.key_service.get_secret_arn",
                new_callable=AsyncMock,
            ) as mock_get_secret_arn,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_status = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_get_secret_arn.return_value = secret_arn

            await manager.provision_user_container(
                "user_test_123",
                provider_choice="byo_key",
                byo_provider="openai",
            )

            # The per-user task def registration must have included the
            # OPENAI_API_KEY secret entry. With register_task_definition mocked,
            # we inspect the kwargs of the call.
            assert mock_ecs_client.register_task_definition.called
            reg_kwargs = mock_ecs_client.register_task_definition.call_args.kwargs
            container_def = reg_kwargs["containerDefinitions"][0]
            assert "secrets" in container_def, "byo_key must add a secrets[] block"
            secret_names = {s["name"] for s in container_def["secrets"]}
            assert "OPENAI_API_KEY" in secret_names, f"Expected OPENAI_API_KEY in task-def secrets, got {secret_names}"
            entry = next(s for s in container_def["secrets"] if s["name"] == "OPENAI_API_KEY")
            assert entry["valueFrom"] == secret_arn

            # The ARN was resolved from Secrets Manager keyed on (user, provider).
            mock_get_secret_arn.assert_awaited_once_with("user_test_123", "openai")

    @pytest.mark.asyncio
    async def test_provision_byo_key_anthropic_uses_anthropic_env_var(self, manager, mock_ecs_client, mock_efs_client):
        """byo_provider=anthropic pivots the secrets[] env var name to
        ANTHROPIC_API_KEY (the env var the OpenClaw anthropic provider reads)."""
        mock_ecs_client.describe_services.return_value = {"services": []}
        secret_arn = "arn:aws:secretsmanager:us-east-1:123:secret:isol8/dev/user-keys/user_test_123/anthropic-XyZqr"

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch(
                "core.services.key_service.get_secret_arn",
                new_callable=AsyncMock,
            ) as mock_get_secret_arn,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_status = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_get_secret_arn.return_value = secret_arn

            await manager.provision_user_container(
                "user_test_123",
                provider_choice="byo_key",
                byo_provider="anthropic",
            )

            reg_kwargs = mock_ecs_client.register_task_definition.call_args.kwargs
            container_def = reg_kwargs["containerDefinitions"][0]
            secret_names = {s["name"] for s in container_def["secrets"]}
            assert "ANTHROPIC_API_KEY" in secret_names
            entry = next(s for s in container_def["secrets"] if s["name"] == "ANTHROPIC_API_KEY")
            assert entry["valueFrom"] == secret_arn
            mock_get_secret_arn.assert_awaited_once_with("user_test_123", "anthropic")

    @pytest.mark.asyncio
    async def test_provision_byo_key_missing_secret_arn_raises_before_service_create(
        self, manager, mock_ecs_client, mock_efs_client
    ):
        """If Secrets Manager has no per-user key (key never saved or deleted),
        get_secret_arn returns None and provisioning must abort BEFORE
        create_user_service so we don't end up with a half-provisioned ECS
        service the user can't use. Caller is expected to gate provisioning on
        a successful key save."""
        mock_ecs_client.describe_services.return_value = {"services": []}

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch(
                "core.services.key_service.get_secret_arn",
                new_callable=AsyncMock,
            ) as mock_get_secret_arn,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_status = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            # No per-user secret in Secrets Manager.
            mock_get_secret_arn.return_value = None

            with pytest.raises(EcsManagerError):
                await manager.provision_user_container(
                    "user_test_123",
                    provider_choice="byo_key",
                    byo_provider="openai",
                )

            # Service was never created.
            assert not mock_ecs_client.create_service.called

    @pytest.mark.asyncio
    async def test_provision_chatgpt_oauth_pre_stages_auth_before_service_create(
        self, manager, mock_ecs_client, mock_efs_client
    ):
        """For chatgpt_oauth, the codex auth.json must be written to EFS
        BEFORE create_service is called — otherwise the first task boots
        without auth and crashes on first chat."""
        mock_ecs_client.describe_services.return_value = {"services": []}
        call_order: list[str] = []

        async def _record_pre_stage(*, user_id, oauth_tokens):
            call_order.append("pre_stage")

        def _record_create_service(**kwargs):
            call_order.append("create_service")
            return {"service": {"serviceName": kwargs.get("serviceName")}}

        mock_ecs_client.create_service.side_effect = _record_create_service

        tokens = {"access_token": "tok-a", "refresh_token": "tok-r"}

        # The connector (token owner) is a DIFFERENT id than the container owner
        # so a regression that drops ``token_owner_id=`` at the call site —
        # reading the owner's row instead of the org connector's — would surface
        # as get_decrypted_tokens being awaited with the owner id, which the
        # assertion below catches. (For orgs the connector is the workspace
        # owner's Clerk id, not the org id.)
        connector_id = "user_connector_abc"
        mock_get_tokens = AsyncMock(return_value=tokens)

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch(
                "core.services.oauth_service.resolve_token_owner",
                AsyncMock(return_value=connector_id),
            ),
            patch("core.services.oauth_service.get_decrypted_tokens", mock_get_tokens),
            patch("core.containers.workspace.pre_stage_auth_profile_store", side_effect=_record_pre_stage),
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_status = AsyncMock(return_value=_make_container_dict(status="provisioning"))

            await manager.provision_user_container("user_test_123", provider_choice="chatgpt_oauth")

            # Tokens were read from the resolved CONNECTOR's row, not the
            # container owner's — guards the token_owner_id= plumbing.
            mock_get_tokens.assert_awaited_once_with(user_id=connector_id, bound_to="user_test_123")

            assert call_order, "pre_stage_auth_profile_store and create_service must both be called"
            # pre_stage_auth_profile_store runs before create_service.
            assert call_order.index("pre_stage") < call_order.index("create_service"), (
                f"pre_stage_auth_profile_store must run before create_service, got order={call_order}"
            )

            # The per-user task def MUST NOT carry CODEX_HOME. We used to
            # inject it pointing at codex/auth.json — that approach was
            # wrong: OpenClaw 6.6 imports the container-global
            # credentials/oauth.json into each agent's auth store, not
            # $CODEX_HOME/auth.json. The fix in pre_stage_auth_profile_store
            # writes to the right place; CODEX_HOME is now dead weight.
            # Asserting absence to prevent regression to the old approach.
            reg_kwargs = mock_ecs_client.register_task_definition.call_args.kwargs
            envs = reg_kwargs["containerDefinitions"][0].get("environment") or []
            codex_home = next((e for e in envs if e["name"] == "CODEX_HOME"), None)
            assert codex_home is None, (
                "chatgpt_oauth must NOT inject CODEX_HOME — credentials go in "
                "credentials/oauth.json, not codex/auth.json"
            )

    @pytest.mark.asyncio
    async def test_provision_chatgpt_oauth_aborts_when_no_tokens(self, manager, mock_ecs_client, mock_efs_client):
        """If no active OAuth row exists for the user, provisioning aborts
        before any AWS calls. Caller is expected to gate on a completed
        device-code flow."""
        mock_ecs_client.describe_services.return_value = {"services": []}

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch(
                "core.services.oauth_service.resolve_token_owner",
                AsyncMock(return_value="user_test_123"),
            ),
            patch("core.services.oauth_service.get_decrypted_tokens", AsyncMock(return_value=None)),
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)

            with pytest.raises(EcsManagerError):
                await manager.provision_user_container("user_test_123", provider_choice="chatgpt_oauth")

            assert not mock_ecs_client.create_service.called

    @pytest.mark.asyncio
    async def test_provision_bedrock_claude_skips_pre_task_setup(self, manager, mock_ecs_client, mock_efs_client):
        """bedrock_claude routes auth via the task IAM role — no per-user
        secrets injection, no codex auth pre-stage. The registered task def
        must NOT carry a per-user secrets[] block beyond whatever the CDK
        base already had."""
        mock_ecs_client.describe_services.return_value = {"services": []}

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch(
                "core.services.key_service.get_secret_arn",
                new_callable=AsyncMock,
            ) as mock_get_secret_arn,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_status = AsyncMock(return_value=_make_container_dict(status="provisioning"))

            await manager.provision_user_container("user_test_123", provider_choice="bedrock_claude")

            # Secrets Manager never queried for bedrock.
            assert not mock_get_secret_arn.called

            reg_kwargs = mock_ecs_client.register_task_definition.call_args.kwargs
            container_def = reg_kwargs["containerDefinitions"][0]
            # No per-user secrets layered on top.
            assert not container_def.get("secrets"), (
                "bedrock_claude must not add per-user secrets; auth comes from task IAM role"
            )

    @pytest.mark.asyncio
    async def test_provision_byo_key_requires_byo_provider(self, manager, mock_ecs_client, mock_efs_client):
        """byo_key without byo_provider is a programming error from the
        caller. Raise before creating any AWS resources."""
        mock_ecs_client.describe_services.return_value = {"services": []}

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)

            with pytest.raises(EcsManagerError):
                await manager.provision_user_container("user_test_123", provider_choice="byo_key")

            assert not mock_ecs_client.create_service.called

    @pytest.mark.asyncio
    async def test_provision_unknown_provider_choice_raises(self, manager, mock_ecs_client, mock_efs_client):
        """An unknown provider_choice value is rejected before any AWS work."""
        mock_ecs_client.describe_services.return_value = {"services": []}

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)

            with pytest.raises(EcsManagerError):
                await manager.provision_user_container("user_test_123", provider_choice="something-else")

            assert not mock_ecs_client.create_service.called

    @pytest.mark.asyncio
    async def test_provision_byo_key_restart_from_zero_re_registers_task_def_with_secrets(
        self, manager, mock_ecs_client, mock_efs_client
    ):
        """When a BYOK user's container is at desired=0 and re-provisions,
        the restart path MUST register a fresh task def carrying the per-user
        OPENAI_API_KEY/ANTHROPIC_API_KEY secret entry. Without this, the
        service comes back on the prior task definition (which may not have
        the secret, e.g. user just switched provider while the container was
        scaled to zero) and the container fails provider auth on cold start.
        """
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "openclaw-user_test_123-f4ae64abb2db",
                    "status": "ACTIVE",
                    "desiredCount": 0,
                    "runningCount": 0,
                }
            ]
        }
        old_arn = "arn:aws:ecs:us-east-1:123:task-definition/openclaw:5"
        new_arn = "arn:aws:ecs:us-east-1:123:task-definition/openclaw:6"
        mock_ecs_client.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": new_arn}}
        secret_arn = "arn:aws:secretsmanager:us-east-1:123:secret:isol8/dev/user-keys/user_test_123/openai-AbCdE"

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch(
                "core.services.key_service.get_secret_arn",
                new_callable=AsyncMock,
            ) as mock_get_secret_arn,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(
                return_value=_make_container_dict(
                    status="stopped",
                    access_point_id="fsap-user-restart",
                    task_definition_arn=old_arn,
                )
            )
            mock_repo.update_fields = AsyncMock()
            mock_get_secret_arn.return_value = secret_arn

            await manager.provision_user_container(
                "user_test_123",
                provider_choice="byo_key",
                byo_provider="openai",
            )

            # A fresh task def was registered carrying the BYO secret.
            assert mock_ecs_client.register_task_definition.called, (
                "Restart-from-zero must register a new task definition so the "
                "BYO secrets get applied; otherwise the cold-started task "
                "boots on a stale revision without OPENAI_API_KEY."
            )
            reg_kwargs = mock_ecs_client.register_task_definition.call_args.kwargs
            container_def = reg_kwargs["containerDefinitions"][0]
            secret_names = {s["name"] for s in container_def.get("secrets", [])}
            assert "OPENAI_API_KEY" in secret_names, (
                f"Expected OPENAI_API_KEY in restart-path task-def secrets, got {secret_names}"
            )

            # update_service was called with the new ARN + desiredCount=1.
            update_calls = [
                c for c in mock_ecs_client.update_service.call_args_list if c.kwargs.get("desiredCount") == 1
            ]
            assert update_calls, "Expected an update_service call to scale up to 1"
            kw = update_calls[-1].kwargs
            assert kw.get("taskDefinition") == new_arn, (
                "update_service must point at the freshly-registered task def; "
                "without taskDefinition= the service stays pinned to the prior revision."
            )

            # Old task def was deregistered.
            assert any(
                c.kwargs.get("taskDefinition") == old_arn
                for c in mock_ecs_client.deregister_task_definition.call_args_list
            ), "Expected the prior per-user task def to be deregistered after refresh"


class TestPerUserTaskSize:
    """Every per-user task def is registered with the single PER_USER_CPU
    / PER_USER_MEMORY constants, regardless of plan tier."""

    @pytest.mark.asyncio
    async def test_register_task_definition_uses_per_user_constants(self, manager, mock_ecs_client, mock_efs_client):
        """Per-user task definitions clone the CDK base but enforce the
        canonical 0.5 vCPU / 1 GB shape — they don't inherit a stale base
        cpu/memory value."""
        from core.containers.ecs_manager import PER_USER_CPU, PER_USER_MEMORY_MIB

        mock_ecs_client.describe_services.return_value = {"services": []}

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_status = AsyncMock(return_value=_make_container_dict(status="provisioning"))

            await manager.provision_user_container("user_test_123", provider_choice="bedrock_claude")

            reg_kwargs = mock_ecs_client.register_task_definition.call_args.kwargs
            assert reg_kwargs["cpu"] == PER_USER_CPU
            assert reg_kwargs["memory"] == PER_USER_MEMORY_MIB


def test_user_owned_mcp_servers_excludes_integration_and_paperclip():
    """Only the user's own servers are carried forward — registry integration
    keys (re-injected from credentials) and the seeded paperclip block are not,
    and non-dict junk is ignored."""
    from core.containers.ecs_manager import _user_owned_mcp_servers

    existing = {
        "paperclip": {"command": "npx"},
        "stripe": {"command": "npx"},  # registry integration key
        "mine": {"command": "npx", "args": ["-y", "x"]},  # user-added
        "bad": "not-a-dict",
    }
    assert _user_owned_mcp_servers(existing, {"stripe"}) == {"mine": {"command": "npx", "args": ["-y", "x"]}}


@pytest.mark.asyncio
async def test_provision_mcp_servers_preserves_user_and_merges_connected(monkeypatch):
    """A full regenerate keeps the user's manual server, drops a stale on-disk
    integration copy, and lets the freshly-built connected server win."""
    from core.containers import ecs_manager as em
    from core.services.credentials import registry

    async def _read(owner_id):
        return {
            "mcp": {
                "servers": {
                    "paperclip": {"command": "npx"},  # re-seeded by generator
                    "stripe": {"command": "stale-on-disk"},  # stale integration copy
                    "mycustom": {"command": "npx", "args": ["-y", "x"]},  # user-added
                }
            }
        }

    monkeypatch.setattr(em, "read_openclaw_config_from_efs", _read)

    class _Spec:
        key = "stripe"

    class _Wiring:
        mcp_server = _Spec()

    class _Provider:
        wiring = _Wiring()

    monkeypatch.setattr(registry, "all_providers", lambda: {"stripe": _Provider()})

    result = await em._provision_mcp_servers("org_1", {"stripe": {"command": "fresh-connected"}})
    assert result == {
        "mycustom": {"command": "npx", "args": ["-y", "x"]},
        "stripe": {"command": "fresh-connected"},
    }


@pytest.mark.asyncio
async def test_provision_mcp_servers_failsafe_returns_connected_on_error(monkeypatch):
    """A read/registry hiccup must not fail provisioning — fall back to the
    connected servers (worst case the user re-adds their manual server)."""
    from core.containers import ecs_manager as em

    async def _boom(owner_id):
        raise RuntimeError("efs unreachable")

    monkeypatch.setattr(em, "read_openclaw_config_from_efs", _boom)
    connected = {"stripe": {"command": "npx"}}
    assert await em._provision_mcp_servers("org_1", connected) == connected


@pytest.mark.asyncio
async def test_provision_channel_accounts_extracts_accounts(monkeypatch):
    """Carry the live on-disk channel accounts across a reprovision overwrite;
    skip providers with no accounts or an empty accounts map."""
    from core.containers import ecs_manager as em

    async def _read(owner_id):
        return {
            "channels": {
                "telegram": {
                    "enabled": True,
                    "dmPolicy": "pairing",
                    "accounts": {"a": {"botToken": "123:ABC", "allowFrom": ["u1"]}},
                },
                "discord": {"enabled": True, "dmPolicy": "pairing"},  # no accounts
                "slack": {"enabled": True, "accounts": {}},  # empty accounts
            }
        }

    monkeypatch.setattr(em, "read_openclaw_config_from_efs", _read)
    out = await em._provision_channel_accounts("org_x")
    assert out == {"telegram": {"a": {"botToken": "123:ABC", "allowFrom": ["u1"]}}}


@pytest.mark.asyncio
async def test_provision_channel_accounts_handles_missing_config(monkeypatch):
    """No openclaw.json on disk yet → nothing to carry forward."""
    from core.containers import ecs_manager as em

    async def _none(owner_id):
        return None

    monkeypatch.setattr(em, "read_openclaw_config_from_efs", _none)
    assert await em._provision_channel_accounts("org_x") == {}


@pytest.mark.asyncio
async def test_provision_channel_accounts_failsafe_returns_empty_on_error(monkeypatch):
    """An EFS read hiccup must not fail provisioning — fall back to {} (worst
    case the user re-runs the channel wizard)."""
    from core.containers import ecs_manager as em

    async def _boom(owner_id):
        raise OSError("efs down")

    monkeypatch.setattr(em, "read_openclaw_config_from_efs", _boom)
    assert await em._provision_channel_accounts("org_x") == {}


# ---------------------------------------------------------------------------
# _await_running_transition substatus progression
# ---------------------------------------------------------------------------


class TestAwaitRunningTransitionSubstatus:
    """The cold-start poller must surface the task-running-but-gateway-
    booting window as substatus="task_running" so the frontend stepper can
    show a real phase instead of a time-based guess. The window between
    "service_created" and "gateway_healthy" is minutes long (image pull +
    OpenClaw boot) and previously wrote nothing."""

    @pytest.mark.asyncio
    async def test_writes_task_running_once_while_gateway_port_closed(self, manager):
        rows = [
            _make_container_dict(status="provisioning", substatus="service_created"),
            _make_container_dict(status="provisioning", substatus="task_running"),
            _make_container_dict(status="provisioning", substatus="task_running"),
        ]
        update_calls = []

        async def track_update(user_id, fields):
            update_calls.append(fields)

        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(side_effect=rows)
            mock_repo.update_fields = AsyncMock(side_effect=track_update)
            manager._get_primary_deployment = AsyncMock(return_value={"id": "dep-1", "rolloutState": "COMPLETED"})
            manager._poll_running_task = AsyncMock(return_value=("arn:aws:ecs:task/abc", "10.0.0.1", 1))
            manager.is_healthy = MagicMock(side_effect=[False, False, True])

            await manager._await_running_transition("user_test_123", interval_s=0)

        # Exactly ONE task_running write (not one per poll iteration), then
        # the terminal running/gateway_healthy write.
        task_running_writes = [f for f in update_calls if f.get("substatus") == "task_running"]
        assert len(task_running_writes) == 1
        assert update_calls[-1]["status"] == "running"
        assert update_calls[-1]["substatus"] == "gateway_healthy"


# ---------------------------------------------------------------------------
# reprovision_for_user — full reprovision, not stop+start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reprovision_for_user_scales_to_zero_then_full_reprovision(manager):
    """The admin reprovision must run a FULL reprovision: scale to 0 first (so
    provision takes its scale-from-zero branch that re-registers the task def +
    rewrites the EFS config), wait for the old task to fully drain (single EFS
    SQLite writer), then dispatch background_provision. The old stale-task-def
    force-redeploy path (start_user_service) must NOT be used.
    """
    order = []
    dispatched = []

    async def _fake_stop(uid, *, reason="manual"):
        order.append("stop")

    async def _fake_drain(uid, timeout_s=90.0):
        order.append("drain")
        return True

    async def _fake_bg(owner_id, clerk_user_id):
        order.append("provision")
        dispatched.append((owner_id, clerk_user_id))

    manager.stop_user_service = AsyncMock(side_effect=_fake_stop)
    manager._wait_for_service_drained = AsyncMock(side_effect=_fake_drain)
    manager.start_user_service = AsyncMock()
    with patch(
        "core.services.container_provision.background_provision",
        side_effect=_fake_bg,
    ):
        result = await manager.reprovision_for_user("org_abc")
        # Let the create_task'd coroutine run.
        import asyncio

        await asyncio.sleep(0)

    assert result == {"status": "reprovisioning"}
    # Scale to 0 first so provision hits the re-register + write-config branch.
    manager.stop_user_service.assert_awaited_once_with("org_abc", reason="reprovision")
    # The old task MUST be fully drained before the new one is provisioned —
    # otherwise the draining old task and the new task share the EFS WAL DB and
    # corrupt it. Ordering is load-bearing: stop -> drain -> provision.
    manager._wait_for_service_drained.assert_awaited_once_with("org_abc")
    assert order == ["stop", "drain", "provision"], order
    # Then the FULL provision is dispatched.
    assert dispatched == [("org_abc", "org_abc")]
    # The old stale-task-def scale-up path must NOT be used.
    manager.start_user_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_service_drained_returns_true_when_zero(manager, mock_ecs_client):
    """Returns True as soon as the service reports 0 running + 0 pending."""
    mock_ecs_client.describe_services.return_value = {"services": [{"runningCount": 0, "pendingCount": 0}]}
    assert await manager._wait_for_service_drained("user_test_123", timeout_s=9.0) is True


@pytest.mark.asyncio
async def test_wait_for_service_drained_times_out_returns_false(manager, mock_ecs_client):
    """If the service never drains, returns False so the caller proceeds rather
    than wedging an admin reprovision on a task that refuses to stop."""
    mock_ecs_client.describe_services.return_value = {"services": [{"runningCount": 1, "pendingCount": 0}]}
    with patch("core.containers.ecs_manager.asyncio.sleep", new=AsyncMock()):
        assert await manager._wait_for_service_drained("user_test_123", timeout_s=9.0) is False


@pytest.mark.asyncio
async def test_pre_stage_oauth_does_not_clear_default_agent_row(manager):
    """Ops re-stage (provision/reprovision) must STAGE the seed but never
    force-reimport: clearing the live container token resurrects a stale
    DDB seed (the refresh_token_reused incident, 2026-06-23)."""
    with (
        patch(
            "core.services.oauth_service.get_decrypted_tokens",
            new=AsyncMock(return_value={"access_token": "a", "refresh_token": "r"}),
        ),
        patch("core.containers.workspace.pre_stage_auth_profile_store", new=AsyncMock()) as stage,
        patch(
            "core.containers.workspace.clear_default_agent_auth_profile_row",
            new=AsyncMock(),
        ) as clear,
    ):
        await manager._pre_stage_oauth_for_user("user_test_123")

    stage.assert_awaited_once()
    clear.assert_not_called()


def test_start_refusal_codes_match_the_trial_cap_reasons():
    """The refusal codes are literals in ecs_manager (trial_cap is imported
    lazily there to avoid a module cycle), so pin them to the real reasons.

    They MUST be the same strings the provision gate uses, because a router maps
    a refusal straight to the matching gate's payload — an owner must see the
    identical 402 card whichever door they knocked on."""
    from core.containers.ecs_manager import START_REFUSED_CAP, START_REFUSED_EXPIRED
    from core.services import trial_cap

    assert START_REFUSED_CAP == trial_cap.DAILY_CAP
    assert START_REFUSED_EXPIRED == trial_cap.TRIAL_EXPIRED


class TestProvisionLeaseFencing:
    """Codex P1/P2 on the lease PR: the fence must reach INSIDE
    create_user_service, and ECS 'liveness' must mean live."""

    @pytest.mark.asyncio
    async def test_superseded_run_cannot_deposit_its_row_before_service_create(self, manager, monkeypatch):
        """A run superseded during the mint window must raise at the pre-upsert
        checkpoint — BEFORE writing its gateway_token/AIP map onto the row and
        before any ECS artifact is registered."""
        from core.repositories.container_repo import ProvisionSuperseded

        monkeypatch.setattr(
            "core.repositories.container_repo.update_container_leased",
            AsyncMock(side_effect=ProvisionSuperseded("taken over")),
        )
        upsert = AsyncMock()
        monkeypatch.setattr("core.repositories.container_repo.upsert", upsert)

        with pytest.raises(ProvisionSuperseded):
            await manager.create_user_service("user_z", "gw-token", claim_id="stale-claim")

        upsert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claimless_create_user_service_is_unfenced(self, manager, monkeypatch):
        """debug.py calls create_user_service directly with no lease — the
        checkpoint must no-op, not blow up."""
        leased = AsyncMock()
        monkeypatch.setattr("core.repositories.container_repo.update_container_leased", leased)
        monkeypatch.setattr("core.repositories.container_repo.upsert", AsyncMock())
        # Fail fast inside the AWS steps — we only care that no checkpoint ran
        # and the upsert was reached.
        monkeypatch.setattr(manager, "_create_access_point", MagicMock(side_effect=RuntimeError("stop here")))
        monkeypatch.setattr("core.repositories.container_repo.update_fields", AsyncMock())
        monkeypatch.setattr("core.repositories.container_repo.update_status", AsyncMock())

        with pytest.raises(Exception):
            await manager.create_user_service("user_d", "gw-token")

        leased.assert_not_awaited()

    def _describe(self, *, desired=1, running=0, rollout=None, status="ACTIVE"):
        svc = {"status": status, "desiredCount": desired, "runningCount": running}
        if rollout is not None:
            svc["deployments"] = [{"status": "PRIMARY", "rolloutState": rollout}]
        return {"services": [svc]}

    @pytest.mark.asyncio
    async def test_has_live_service_liveness_matrix(self, manager, monkeypatch):
        """desiredCount alone is not liveness: a crash-looping deployment
        (ACTIVE, desired=1, running=0, rollout FAILED) must NOT count as live,
        or stale-lease recovery is suppressed forever."""
        monkeypatch.setattr(
            "core.repositories.container_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": "u", "service_name": "svc-u"}),
        )

        cases = [
            # (desired, running, rollout, expected)
            (1, 1, "COMPLETED", True),  # healthy running task
            (1, 0, "IN_PROGRESS", True),  # legitimate boot window
            (1, 0, "FAILED", False),  # THE WEDGE: crash-loop must be reclaimable
            (1, 0, "COMPLETED", False),  # settled with nothing running
            (0, 0, "COMPLETED", False),  # scaled to zero
        ]
        for desired, running, rollout, expected in cases:
            manager._ecs.describe_services = MagicMock(
                return_value=self._describe(desired=desired, running=running, rollout=rollout)
            )
            assert await manager.has_live_service("u") is expected, (desired, running, rollout)

    @pytest.mark.asyncio
    async def test_has_live_service_fails_safe_on_blind_describe(self, manager, monkeypatch):
        """A describe failure must assume LIVE (never re-provision on a blind
        read) — the recovery can wait a poll; a wrong force-restart cannot."""
        monkeypatch.setattr(
            "core.repositories.container_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": "u", "service_name": "svc-u"}),
        )
        manager._ecs.describe_services = MagicMock(side_effect=RuntimeError("ecs down"))
        assert await manager.has_live_service("u") is True
