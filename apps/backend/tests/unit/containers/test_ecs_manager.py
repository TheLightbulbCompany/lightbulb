"""Tests for EcsManager (ECS Fargate service lifecycle with per-user EFS isolation).

Uses mocked boto3 clients and container_repo -- no real AWS or
DynamoDB required.
"""

import asyncio
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from botocore.exceptions import ClientError

from core.billing import bedrock_gate_tokens as _bedrock_gate_tokens_module
from core.repositories import container_repo as _container_repo_module
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

_OWNER_NETWORK_CONFIGURATION = {
    "awsvpcConfiguration": {
        "subnets": ["subnet-aaa", "subnet-bbb"],
        "securityGroups": ["sg-12345"],
        "assignPublicIp": "ENABLED",
    }
}

# Captured at import, BEFORE the autouse fixture below stubs them, so the one
# test that needs the real lease primitives can put them back.
_REAL_LEASED = _container_repo_module.update_container_leased
_REAL_TAKEOVER = _container_repo_module.takeover_provision
_REAL_GET_BY_OWNER = _container_repo_module.get_by_owner_id
_REAL_PERSIST_HASH = _bedrock_gate_tokens_module.persist_gate_token_hash


@contextmanager
def _real_containers_table(monkeypatch):
    """A moto-backed containers table with the REAL lease primitives put back
    (the autouse fixture stubs them), so a test can assert on the row DynamoDB
    actually holds rather than on mock calls. Yields the repo module."""
    import boto3
    from moto import mock_aws

    from core.repositories import container_repo

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr("core.dynamodb._table_prefix", "test-")
        monkeypatch.setattr("core.dynamodb._dynamodb_resource", ddb)
        monkeypatch.setattr("core.repositories.container_repo.update_container_leased", _REAL_LEASED)
        monkeypatch.setattr("core.repositories.container_repo.takeover_provision", _REAL_TAKEOVER)
        # _start_guards_clear stubs the row read to None, which would blind
        # every assertion (and the real upsert's CAS snapshot).
        monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", _REAL_GET_BY_OWNER)
        yield container_repo


def _run_off_loop(coro_factory):
    """Run an async repo call from a SYNCHRONOUS seam. ``_create_access_point``
    and ``_register_task_definition`` are plain sync calls inside the already-
    running test loop, and they are the only hooks that sit between a
    checkpoint and the row write it is supposed to be fencing — so the
    interleaved takeover gets its own loop on a worker thread."""
    out: dict = {}

    def run():
        out["result"] = asyncio.run(coro_factory())

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    return out["result"]


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

    def test_owner_network_configuration_enables_the_public_ice_mux_subnets(self, manager):
        """A missing or stale awsvpc config would leave owner ICE candidates unreachable."""
        assert manager._network_configuration() == _OWNER_NETWORK_CONFIGURATION

    @pytest.mark.asyncio
    async def test_creates_service_and_repo_record(self, manager, mock_ecs_client, mock_efs_client):
        """create_user_service upserts repo record early, then creates access point, task def, service."""
        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch("core.containers.ecs_manager.ULID", return_value="01ARZ3TESTGENERATION0"),
        ):
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict())

            service_name = await manager.create_user_service("user_test_123", "token-abc")

            assert service_name == "openclaw-user_test_123-f4ae64abb2db"

            # Verify repo upsert called early, minting provision_generation
            # (INV-2 single mint point) atomically with the row.
            mock_repo.upsert.assert_called_once_with(
                "user_test_123",
                {
                    "service_name": "openclaw-user_test_123-f4ae64abb2db",
                    "gateway_token": "token-abc",
                    "status": "provisioning",
                    "substatus": None,
                    "owner_type": "personal",
                    "provision_generation": "01ARZ3TESTGENERATION0",
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
            assert call_kwargs["networkConfiguration"] == _OWNER_NETWORK_CONFIGURATION

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
        """ECS API failure raises EcsManagerError and rolls back AWS resources —
        WITHOUT pre-empting the outer release with a status='error' write."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict())

            mock_ecs_client.create_service.side_effect = ClientError(
                {"Error": {"Code": "ClusterNotFoundException", "Message": "not found"}},
                "CreateService",
            )

            with pytest.raises(EcsManagerError, match="Failed to create ECS service"):
                await manager.create_user_service("user_test_123", "token")

            # NO status='error' write: create_user_service no longer pre-empts
            # the claim-fenced release (single-owner rule). It rolls back + raises;
            # release_provision_to_error owns the transition (+ retryable/last_error).
            error_calls = [c for c in mock_repo.update_fields.call_args_list if c[0][1].get("status") == "error"]
            assert error_calls == [], "create_user_service must not write status='error' (outer release owns it)"

            # Rollback: task def deregistered and access point deleted
            mock_ecs_client.deregister_task_definition.assert_called_once()
            mock_efs_client.delete_access_point.assert_called_once_with(AccessPointId="fsap-user123")

    @pytest.mark.asyncio
    async def test_access_point_failure_raises(self, manager, mock_efs_client, mock_ecs_client):
        """EFS access point creation failure raises EcsManagerError WITHOUT
        pre-empting the outer release with a status='error' write."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict())

            mock_efs_client.create_access_point.side_effect = ClientError(
                {"Error": {"Code": "FileSystemNotFound", "Message": "not found"}},
                "CreateAccessPoint",
            )

            with pytest.raises(EcsManagerError, match="Failed to create EFS access point"):
                await manager.create_user_service("user_test_123", "token")

            # NO status='error' write (single-owner rule) — the outer release owns it.
            error_calls = [c for c in mock_repo.update_fields.call_args_list if c[0][1].get("status") == "error"]
            assert error_calls == [], "create_user_service must not write status='error' (outer release owns it)"

            # No AWS rollback needed -- nothing was created yet
            mock_ecs_client.create_service.assert_not_called()
            mock_ecs_client.deregister_task_definition.assert_not_called()

    @pytest.mark.asyncio
    async def test_task_def_failure_rolls_back_access_point(self, manager, mock_ecs_client, mock_efs_client):
        """Task definition failure rolls back the access point WITHOUT pre-empting
        the outer release with a status='error' write."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict())

            mock_ecs_client.register_task_definition.side_effect = ClientError(
                {"Error": {"Code": "ClientException", "Message": "failed"}},
                "RegisterTaskDefinition",
            )

            with pytest.raises(EcsManagerError, match="Failed to register per-user task definition"):
                await manager.create_user_service("user_test_123", "token")

            # NO status='error' write (single-owner rule) — the outer release owns it.
            error_calls = [c for c in mock_repo.update_fields.call_args_list if c[0][1].get("status") == "error"]
            assert error_calls == [], "create_user_service must not write status='error' (outer release owns it)"

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

    @pytest.mark.asyncio
    async def test_mints_provision_generation_ulid(self, manager, mock_ecs_client, mock_efs_client):
        """create_user_service's row-creating upsert (event G1) mints a real
        ULID provision_generation -- the single mint point (INV-2)."""
        from ulid import ULID

        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict())

            await manager.create_user_service("user_test_123", "token-abc")

            fields = mock_repo.upsert.call_args.args[1]
            assert "provision_generation" in fields
            # Round-trips through the real ULID parser -- proves it's a real
            # ULID, not a placeholder string.
            ULID.from_str(fields["provision_generation"])

    @pytest.mark.asyncio
    async def test_each_fresh_service_create_mints_a_new_generation(self, manager, mock_ecs_client, mock_efs_client):
        """Two separate create_user_service calls (e.g. a wipe-and-reprovision)
        each mint their OWN provision_generation -- a fresh service create
        changes it, unlike a restart/redeploy (see the redeploy/scale-from-zero
        tests below)."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.upsert = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict())

            await manager.create_user_service("user_test_123", "token-abc")
            await manager.create_user_service("user_test_123", "token-def")

            first_gen = mock_repo.upsert.call_args_list[0].args[1]["provision_generation"]
            second_gen = mock_repo.upsert.call_args_list[1].args[1]["provision_generation"]
            assert first_gen != second_gen


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
        """A genuine ECS API failure raises EcsManagerError."""
        mock_ecs_client.update_service.side_effect = ClientError(
            {"Error": {"Code": "ClusterNotFoundException", "Message": "no cluster"}},
            "UpdateService",
        )

        with pytest.raises(EcsManagerError, match="Failed to stop ECS service"):
            await manager.stop_user_service("user_test_123")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", ["ServiceNotFoundException", "ServiceNotActiveException"])
    async def test_stop_missing_service_is_idempotent_success(self, manager, mock_ecs_client, code):
        """A service that doesn't exist (or is already INACTIVE) is idempotent
        success — "stopped" is already true. The row (if any) is still marked
        stopped, so a drifted "running" row regains claim/wake eligibility.
        This is what lets a cold provider change on a
        subscribed-but-never-provisioned owner fall through to
        dispatch_provision's create-from-scratch path instead of 500ing."""
        mock_ecs_client.update_service.side_effect = ClientError(
            {"Error": {"Code": code, "Message": "gone"}},
            "UpdateService",
        )

        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="running"))
            mock_repo.update_status = AsyncMock(return_value=_make_container_dict(status="stopped"))
            mock_repo.stamp_stop_reason = AsyncMock()

            await manager.stop_user_service("user_test_123")

            mock_repo.update_status.assert_called_once_with("user_test_123", "stopped")
            mock_repo.stamp_stop_reason.assert_awaited_once_with("user_test_123", "manual")


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
                networkConfiguration=_OWNER_NETWORK_CONFIGURATION,
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


class TestForceRedeployUserService:
    @pytest.mark.asyncio
    async def test_force_redeploy_reapplies_owner_network_configuration(self, manager, mock_ecs_client):
        with patch("core.services.browser_state.snapshot_browser_state", new=AsyncMock()):
            await manager.force_redeploy_user_service("user_test_123")

        kwargs = mock_ecs_client.update_service.call_args.kwargs
        assert kwargs["networkConfiguration"] == _OWNER_NETWORK_CONFIGURATION

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
        from core.services.trial_cap import CAP_MICROCENTS, RESERVE_ESTIMATE_MICROCENTS

        # Below CAP - RESERVE (the gate's actual admission threshold), not just
        # below CAP -- `CAP_MICROCENTS - 1` now sits inside the gate's refusal
        # band and would make this "under cap" fixture spuriously blocked.
        self._billing(
            monkeypatch,
            subscription_status="trial_local",
            spent_microcents=CAP_MICROCENTS - RESERVE_ESTIMATE_MICROCENTS - 1,
        )
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

    @pytest.mark.asyncio
    async def test_reprovision_stopped_row_refuses_direct_start(self, manager, mock_ecs_client, monkeypatch):
        """A row stopped by a provider-change reprovision
        (stop_reason="reprovision") refuses a DIRECT start: waking the STALE
        task def inside the drain window would make the later provision claim
        see a desired/running service and take the branch that rewrites
        nothing — silently dropping the provider switch. The status poll's
        wake path (dispatch_provision → force=True start) bypasses this
        guard and converges the row properly."""
        from core.containers.ecs_manager import START_REFUSED_REPROVISIONING

        self._billing(monkeypatch, subscription_status="active", spent_microcents=0)
        row = _make_container_dict(status="stopped")
        row["stop_reason"] = "reprovision"
        self._container(monkeypatch, row)

        with patch.object(manager, "_await_running_transition", new_callable=AsyncMock):
            with pytest.raises(ContainerStartRefused) as exc:
                await manager.start_user_service("user_test_123")

        assert exc.value.reason == START_REFUSED_REPROVISIONING
        mock_ecs_client.update_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_needs_provision_marker_refuses_direct_start(self, manager, mock_ecs_client, monkeypatch):
        """A provider switch made while the box was ALREADY parked leaves no
        stop_reason breadcrumb — the needs_provision marker is the row-state
        signal that the registered task def is stale. Every direct-start door
        (wake verb, agent mail, POST /container/start) must refuse and route
        to the full executor, whose force=True start bypasses this guard."""
        from core.containers.ecs_manager import START_REFUSED_REPROVISIONING

        self._billing(monkeypatch, subscription_status="active", spent_microcents=0)
        row = _make_container_dict(status="stopped")
        row["stop_reason"] = "trial_idle"
        row["needs_provision"] = True
        self._container(monkeypatch, row)

        with patch.object(manager, "_await_running_transition", new_callable=AsyncMock):
            with pytest.raises(ContainerStartRefused) as exc:
                await manager.start_user_service("user_test_123")

        assert exc.value.reason == START_REFUSED_REPROVISIONING
        mock_ecs_client.update_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_manually_stopped_row_still_starts(self, manager, mock_ecs_client, monkeypatch):
        """Control: an admin/manual stop (stop_reason="manual") keeps the
        direct-restart door open — only the reprovision window is fenced."""
        self._billing(monkeypatch, subscription_status="active", spent_microcents=0)
        row = _make_container_dict(status="stopped")
        row["stop_reason"] = "manual"
        self._container(monkeypatch, row)

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
        THEN dispatches dispatch_provision -> provision_user_container, which
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

        started.assert_awaited_once_with(
            "user_test_123", force=True, claim_id=None, generation=None, attempt_token=None
        )


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
            mock_repo.delete = AsyncMock(return_value=container_dict)

            # The DELETE's own ALL_OLD image is handed back to the caller — an
            # account teardown reads the bedrock-gate token hashes off it,
            # since nothing else can find those rows once this row is gone.
            assert await manager.delete_user_service("user_test_123") is container_dict

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
    async def test_the_bedrock_footprint_is_reaped_here_off_the_all_old_image(self, manager, mock_ecs_client):
        """THE SEAM. Every teardown in the system reaches AWS through this
        function — entitlement cancellation, the admin account-wipe, DELETE
        /debug/provision, DELETE /debug/user-data. Three of those four leaked a
        per-owner IAM role and its inference profiles until the reap moved here,
        into the same account-wide 1,000-role quota (dev, staging and prod share
        one AWS account).

        It has to be THIS function because the row it deletes is the only handle
        on those resources, and it must be the row's own ALL_OLD image rather
        than the read above it — that read is eventually consistent."""
        row_at_death = _make_container_dict(status="running")
        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch("core.billing.bedrock_aip.reap_owner_bedrock_resources", new_callable=AsyncMock) as reap,
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)  # stale read
            mock_repo.delete = AsyncMock(return_value=row_at_death)
            reap.return_value = "reaped"

            await manager.delete_user_service("user_test_123")

            reap.assert_awaited_once_with("user_test_123", row_at_death)

    @pytest.mark.asyncio
    async def test_a_partial_reap_fails_the_teardown_rather_than_passing_silently(self, manager, mock_ecs_client):
        """Residue that nothing can name again is worse than a visible failure:
        the caller retries (every step is idempotent), and the audited wipe
        reports a partial teardown instead of claiming success."""
        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch("core.billing.bedrock_aip.reap_owner_bedrock_resources", new_callable=AsyncMock) as reap,
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)
            mock_repo.delete = AsyncMock(return_value=_make_container_dict(status="running"))
            reap.side_effect = RuntimeError("IAM throttled")

            with pytest.raises(RuntimeError):
                await manager.delete_user_service("user_test_123")

    @pytest.mark.asyncio
    async def test_delete_no_db_record(self, manager, mock_ecs_client, mock_efs_client):
        """delete_user_service with no repo record still deletes ECS service —
        and still issues the row delete.

        The read above it is eventually consistent, so "no row" may just be a
        stale answer about a row a racing provision has already written. The
        DeleteItem is a no-op when there is genuinely nothing, and its ALL_OLD
        image is the only thing that can tell the two apart."""
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=None)
            mock_repo.delete = AsyncMock(return_value=None)

            assert await manager.delete_user_service("user_test_123") is None

            mock_ecs_client.update_service.assert_called_once()
            mock_ecs_client.delete_service.assert_called_once()
            # No per-user resources to clean up
            mock_ecs_client.deregister_task_definition.assert_not_called()
            mock_efs_client.delete_access_point.assert_not_called()
            mock_repo.delete.assert_called_once_with("user_test_123")

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
    async def test_provisioning_row_without_service_name_does_not_raise(self, manager):
        """A `provisioning` row EXISTS BEFORE its ECS service does.

        claim_provisioning's first act creates the row as
        status="provisioning"; `service_name` is stamped only once the service
        exists. Subscripting it raised KeyError('service_name') for that whole
        window, which surfaced on prod 2026-07-29 as
        `RPC agents.list failed ... error_class=KeyError` every ~3s plus a 500
        on `GET /container/status?wake=1` during a fresh provision.
        """
        row = _make_container_dict(status="provisioning")
        row.pop("service_name", None)
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=row)
            manager.discover_ip = MagicMock(side_effect=AssertionError("must not probe without a service"))

            container, ip = await manager.resolve_running_container("user_test_123")

            # Same shape as the "service exists but no task yet" branch: the
            # row is still reported, just not reachable.
            assert container == row
            assert ip is None

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
    async def test_reachable_provisioning_container_does_not_write(self, manager):
        """L2 fix (spec §7): resolve_running_container is READ-ONLY now — SFN
        (via wait_for_running, foreground-awaited by scripts.provision_runner)
        is the SOLE writer of the provisioning -> running transition. A
        healthy-but-not-yet-flipped row still reports its DDB status verbatim
        (one poll tick of latency, not a correctness gap) and this method
        must never call container_repo.update_fields."""
        container_dict = _make_container_dict(status="provisioning", service_name="openclaw-user_test_123-f4ae64abb2db")
        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=container_dict)
            manager.discover_ip = MagicMock(return_value="10.0.1.42")
            manager.is_healthy = MagicMock(return_value=True)

            container, ip = await manager.resolve_running_container("user_test_123")

            assert container["status"] == "provisioning"  # untouched
            assert ip == "10.0.1.42"
            mock_repo.update_fields.assert_not_called()


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
        assert kwargs["networkConfiguration"] == _OWNER_NETWORK_CONFIGURATION
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
# wait_for_running / _await_running_transition's FENCED write mode (L2 fix,
# spec §7): claim_id present -> SFN's fenced helpers own the running/error
# transition instead of a bare update_fields.
# ---------------------------------------------------------------------------


class TestWaitForRunningFencedWrites:
    @pytest.mark.asyncio
    async def test_claim_id_none_uses_unfenced_update_fields_unchanged(self, manager, mock_ecs_client):
        """Every non-SFN caller (wake, resize) passes no claim_id -- must be
        byte-for-byte the original unconditioned write."""
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
            mock_repo.update_container_leased = AsyncMock()

            await manager.wait_for_running("user_test_123")

            mock_repo.update_fields.assert_called_once()
            mock_repo.update_container_leased.assert_not_called()

    @pytest.mark.asyncio
    async def test_claim_id_present_writes_through_update_container_leased(self, manager, mock_ecs_client):
        """The runner's foreground call (claim_id set) must write the
        running transition through the FENCED helper, threading
        generation/attempt_token."""
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
            mock_repo.update_container_leased = AsyncMock()

            await manager.wait_for_running(
                "user_test_123", claim_id="claim-1", generation="gen-1", attempt_token="tok-1"
            )

            mock_repo.update_fields.assert_not_called()
            # Two leased calls now: the iteration-opening pure heartbeat
            # (fields=None) and the terminal fenced running write. The
            # assertions below pin the TERMINAL call's contract.
            calls = mock_repo.update_container_leased.call_args_list
            assert calls[0].args[2] is None  # opening heartbeat
            call = mock_repo.update_container_leased.call_args
            assert call.args[0] == "user_test_123"
            assert call.args[1] == "claim-1"
            assert call.args[2]["status"] == "running"
            assert call.kwargs["generation"] == "gen-1"
            assert call.kwargs["attempt_token"] == "tok-1"
            # Terminal success releases the lease with the same write, so
            # residual claims can't misclassify later wake/resize rows as
            # SFN-owned in the boot reconcile.
            assert call.kwargs["release_lease"] is True

    @pytest.mark.asyncio
    async def test_claim_id_present_rollout_failed_writes_through_release_provision_to_error(
        self, manager, mock_ecs_client
    ):
        """The rolloutState=FAILED write, when fenced, routes through
        release_provision_to_error (retryable=False -- a tripped circuit
        breaker will never self-heal) instead of a bare status="error" write,
        closing the historical single-owner-rule exception."""
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
            mock_repo.release_provision_to_error = AsyncMock()
            # The per-iteration lease heartbeat is the FIRST await of every
            # tick. Left as a bare MagicMock it raises TypeError, which the
            # loop's `except Exception` swallows into a `continue` — and with
            # asyncio.sleep patched out that is an infinite tight loop, so the
            # test hangs instead of failing.
            mock_repo.update_container_leased = AsyncMock()

            await asyncio.wait_for(
                manager.wait_for_running("user_test_123", claim_id="claim-1", generation="gen-1"),
                timeout=5,
            )

            mock_repo.update_fields.assert_not_called()
            mock_repo.release_provision_to_error.assert_called_once()
            kwargs = mock_repo.release_provision_to_error.call_args.kwargs
            assert kwargs["retryable"] is False
            assert kwargs["generation"] == "gen-1"

    @pytest.mark.asyncio
    async def test_fenced_write_superseded_propagates(self, manager, mock_ecs_client):
        """A straggler whose fenced write loses the race must propagate
        ProvisionSuperseded -- the runner's caller maps that to EXIT_SUPERSEDED."""
        from core.repositories.container_repo import ProvisionSuperseded

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
            mock_repo.update_container_leased = AsyncMock(side_effect=ProvisionSuperseded("taken over"))

            with pytest.raises(ProvisionSuperseded):
                await manager.wait_for_running("user_test_123", claim_id="claim-1")


# ---------------------------------------------------------------------------
# Gate-token expiry is scheduled off the ROLL CONFIRMATION, not off a clock.
# _await_running_transition is the sole confirmation point: wait_for_running
# (the SFN runner's foreground wait) is a thin delegate to it, and the detached
# poller enters the same loop.
# ---------------------------------------------------------------------------


def _healthy_task_ecs(mock_ecs_client):
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


class TestGateTokenExpiryOnRollConfirmation:
    @pytest.mark.asyncio
    async def test_confirmed_roll_expires_superseded_tokens(self, manager, mock_ecs_client, monkeypatch):
        """The container is up and healthy on whatever def the service points
        at, so the superseded row can finally be judged."""
        expire = AsyncMock()
        monkeypatch.setattr("core.billing.bedrock_gate_tokens.expire_superseded_gate_tokens", expire)
        _healthy_task_ecs(mock_ecs_client)

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "is_healthy", return_value=True),
            patch("core.containers.ecs_manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock()

            await manager.wait_for_running("user_test_123")

        expire.assert_awaited_once_with("user_test_123")

    @pytest.mark.asyncio
    async def test_superseded_fenced_write_expires_nothing(self, manager, mock_ecs_client, monkeypatch):
        """A straggler's observation is not authoritative: its fenced write
        loses the race, so it must not judge the winner's tokens."""
        from core.repositories.container_repo import ProvisionSuperseded

        expire = AsyncMock()
        monkeypatch.setattr("core.billing.bedrock_gate_tokens.expire_superseded_gate_tokens", expire)
        _healthy_task_ecs(mock_ecs_client)

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "is_healthy", return_value=True),
            patch("core.containers.ecs_manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_container_leased = AsyncMock(side_effect=ProvisionSuperseded("taken over"))

            with pytest.raises(ProvisionSuperseded):
                await manager.wait_for_running("user_test_123", claim_id="claim-1")

        expire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_rollout_expires_nothing(self, manager, mock_ecs_client, monkeypatch):
        """THE INCIDENT's shape: the roll never landed. Nothing was confirmed,
        so nothing may be expired — the container is still on the old def."""
        expire = AsyncMock()
        monkeypatch.setattr("core.billing.bedrock_gate_tokens.expire_superseded_gate_tokens", expire)
        mock_ecs_client.list_tasks.return_value = {"taskArns": []}

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch("core.containers.ecs_manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(status="provisioning"))
            mock_repo.update_fields = AsyncMock()
            manager._get_primary_deployment = AsyncMock(
                return_value={"id": "ecs-svc/primary", "rolloutState": "FAILED"}
            )

            await manager._await_running_transition("user_test_123", interval_s=0)

        expire.assert_not_awaited()


# ---------------------------------------------------------------------------
# _service_exists — L2 fix: a describe_services API failure must SURFACE
# (EcsManagerError), never silently divert to the full-provision branch.
# ---------------------------------------------------------------------------


class TestServiceExists:
    def test_no_service_returns_none(self, manager, mock_ecs_client):
        mock_ecs_client.describe_services.return_value = {"services": []}
        assert manager._service_exists("svc-name") is None

    def test_inactive_service_returns_none(self, manager, mock_ecs_client):
        mock_ecs_client.describe_services.return_value = {"services": [{"status": "INACTIVE"}]}
        assert manager._service_exists("svc-name") is None

    def test_active_service_returns_description(self, manager, mock_ecs_client):
        mock_ecs_client.describe_services.return_value = {"services": [{"status": "ACTIVE", "desiredCount": 1}]}
        result = manager._service_exists("svc-name")
        assert result == {"status": "ACTIVE", "desiredCount": 1}

    def test_describe_services_failure_raises_instead_of_returning_none(self, manager, mock_ecs_client):
        """THE L2 fix: previously this swallowed the exception and returned
        None, which the caller (_provision_user_container_inner) reads as
        "no service exists" -- silently diverting into create_service and
        risking a duplicate-create race against an actually-ACTIVE service.
        Now it raises, so the caller's normal except-and-classify path
        handles it (EcsManagerError defaults retryable=True -- a
        describe_services blip IS transient)."""
        from core.containers.ecs_manager import EcsManagerError

        mock_ecs_client.describe_services.side_effect = RuntimeError("ThrottlingException")
        with pytest.raises(EcsManagerError, match="describe_services failed"):
            manager._service_exists("svc-name", "user_test_123")


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
    async def test_resize_carries_per_user_env_and_secrets_from_prior_revision(self, manager, mock_ecs_client):
        """resize must preserve per-user env/secrets layered on the prior
        revision (bearer token, ACP harness env, BYOK secret refs).

        The CDK base carries only fleet-static values; the per-user set exists
        ONLY on the owner's current revision, injected at provision/restart.
        Cloning from the base without carrying it strips
        AWS_BEARER_TOKEN_BEDROCK / OPENAI_API_KEY etc. on every Track-2 image
        apply — the container reboots with no inference credentials until the
        next full restart. Base values must still win on conflicting names so
        the drift fence of test_resize_reads_env_from_base_not_current holds.
        """
        base_def = {
            "family": "isol8-dev-openclaw",
            "taskRoleArn": "arn:aws:iam::123:role/shared-base",
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
                    "secrets": [
                        {
                            "name": "GOG_KEYRING_PASSWORD",
                            "valueFrom": "arn:aws:secretsmanager:us-east-1:123:secret:gog",
                        },
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
        prior_arn = "arn:aws:ecs:us-east-1:123:task-definition/isol8-dev-openclaw-user:7"
        prior_def = {
            **base_def,
            "family": "isol8-dev-openclaw-user",
            "taskRoleArn": "arn:aws:iam::123:role/prior-owner",
            "containerDefinitions": [
                {
                    "name": "openclaw",
                    "image": "ecr/openclaw-extended:old",
                    "environment": [
                        {"name": "HOME", "value": "/home/stale"},
                        {"name": "OPENCLAW_HOOK_TOKEN", "value": "old-hook"},
                        {"name": "AWS_BEARER_TOKEN_BEDROCK", "value": "bearer-xyz"},
                        {"name": "AWS_BEDROCK_FORCE_HTTP1", "value": "1"},
                    ],
                    "secrets": [
                        {
                            "name": "GOG_KEYRING_PASSWORD",
                            "valueFrom": "arn:aws:secretsmanager:us-east-1:123:secret:gog-stale",
                        },
                        {"name": "OPENAI_API_KEY", "valueFrom": "arn:aws:secretsmanager:us-east-1:123:secret:byok"},
                    ],
                }
            ],
        }

        def describe_side_effect(taskDefinition):
            if taskDefinition == prior_arn:
                return {"taskDefinition": prior_def}
            return {"taskDefinition": base_def}

        mock_ecs_client.describe_task_definition.side_effect = describe_side_effect
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
                return_value=_make_container_dict(
                    status="running",
                    access_point_id="fsap-user-789",
                    task_definition_arn=prior_arn,
                )
            )
            mock_repo.update_fields = AsyncMock(return_value=_make_container_dict(status="provisioning"))

            await manager.resize_user_container("user_test_123", new_image="ecr/openclaw-extended:new")

            call_kwargs = mock_ecs_client.register_task_definition.call_args.kwargs
            env_entries = call_kwargs["containerDefinitions"][0]["environment"]
            env = {e["name"]: e["value"] for e in env_entries}
            # Carried per-user values
            assert env["AWS_BEARER_TOKEN_BEDROCK"] == "bearer-xyz"
            assert env["AWS_BEDROCK_FORCE_HTTP1"] == "1"
            # Base wins on conflicts: drift fence intact, stale values dropped
            assert env["HOME"] == "/home/node"
            assert env["CLAWHUB_WORKDIR"] == "/home/node/.openclaw"
            # The hook token minted THIS call wins over the prior revision's
            assert env["OPENCLAW_HOOK_TOKEN"] != "old-hook"
            assert len(env_entries) == len(env), "duplicate env names registered"
            secrets_entries = call_kwargs["containerDefinitions"][0]["secrets"]
            secrets = {s["name"]: s["valueFrom"] for s in secrets_entries}
            assert secrets["OPENAI_API_KEY"] == "arn:aws:secretsmanager:us-east-1:123:secret:byok"
            assert secrets["GOG_KEYRING_PASSWORD"] == "arn:aws:secretsmanager:us-east-1:123:secret:gog"
            assert len(secrets_entries) == len(secrets), "duplicate secret names registered"
            # New image still stamped on the openclaw container
            assert call_kwargs["containerDefinitions"][0]["image"] == "ecr/openclaw-extended:new"
            assert call_kwargs["taskRoleArn"] == "arn:aws:iam::123:role/prior-owner"

    @pytest.mark.asyncio
    async def test_resize_row_task_role_wins_over_prior_revision(self, manager, mock_ecs_client):
        prior_arn = "arn:aws:ecs:us-east-1:123:task-definition/isol8-dev-openclaw-user:7"
        row_role = "arn:aws:iam::123:role/row-owner"
        prior_role = "arn:aws:iam::123:role/prior-owner"
        base = mock_ecs_client.describe_task_definition.return_value["taskDefinition"]
        prior = {**base, "taskRoleArn": prior_role}

        def describe_side_effect(taskDefinition):
            return {"taskDefinition": prior if taskDefinition == prior_arn else base}

        mock_ecs_client.describe_task_definition.side_effect = describe_side_effect
        mock_ecs_client.describe_services.return_value = {
            "services": [{"desiredCount": 0, "deployments": []}],
        }
        row = _make_container_dict(task_definition_arn=prior_arn)
        row["bedrock_role_arn"] = row_role

        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=row)
            mock_repo.update_fields = AsyncMock()
            await manager.resize_user_container("user_test_123", new_cpu="1024", new_memory="4096")

        assert mock_ecs_client.register_task_definition.call_args.kwargs["taskRoleArn"] == row_role

    @pytest.mark.asyncio
    async def test_resize_keeps_base_task_role_when_row_and_prior_have_none(self, manager, mock_ecs_client):
        prior_arn = "arn:aws:ecs:us-east-1:123:task-definition/isol8-dev-openclaw-user:7"
        base = mock_ecs_client.describe_task_definition.return_value["taskDefinition"]
        base_role = base["taskRoleArn"]
        prior = dict(base)
        prior.pop("taskRoleArn")

        def describe_side_effect(taskDefinition):
            return {"taskDefinition": prior if taskDefinition == prior_arn else base}

        mock_ecs_client.describe_task_definition.side_effect = describe_side_effect
        mock_ecs_client.describe_services.return_value = {
            "services": [{"desiredCount": 0, "deployments": []}],
        }

        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(return_value=_make_container_dict(task_definition_arn=prior_arn))
            mock_repo.update_fields = AsyncMock()
            await manager.resize_user_container("user_test_123", new_cpu="1024", new_memory="4096")

        assert mock_ecs_client.register_task_definition.call_args.kwargs["taskRoleArn"] == base_role

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
            assert call_kwargs["networkConfiguration"] == _OWNER_NETWORK_CONFIGURATION
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
# config-lock protocol rollout
# ---------------------------------------------------------------------------


class TestConfigLockProtocolRollout:
    @pytest.mark.asyncio
    async def test_reclones_current_size_and_waits_for_service_stability(self, manager, mock_ecs_client):
        manager.current_container_size = AsyncMock(return_value=("2048", "8192"))
        manager.resize_user_container = AsyncMock(return_value="arn:task-def:new")
        manager._update_container = AsyncMock()
        waiter = MagicMock()
        mock_ecs_client.get_waiter.return_value = waiter
        mock_ecs_client.describe_task_definition.return_value = {
            "taskDefinition": {
                "containerDefinitions": [
                    {
                        "name": "openclaw",
                        "command": [
                            "sh",
                            "-c",
                            '${ISOL8_CONFIG_LOCK_PROTOCOL:-} protocol_path = lock_path + ".isol8-protocol" || exit 1',
                        ],
                        "environment": [
                            {"name": "ISOL8_CONFIG_LOCK_PROTOCOL", "value": "guard-v1"},
                        ],
                    }
                ]
            }
        }
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "taskDefinition": "arn:task-def:new",
                    "deployments": [
                        {
                            "status": "PRIMARY",
                            "taskDefinition": "arn:task-def:new",
                        }
                    ],
                }
            ]
        }

        with patch("core.containers.ecs_manager.asyncio.to_thread") as mock_to_thread:

            async def passthrough(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_to_thread.side_effect = passthrough
            result = await manager.rollout_config_lock_protocol(
                "user_test_123",
                timeout_s=31,
                poll_interval_s=10,
            )

        assert result == "arn:task-def:new"
        manager.resize_user_container.assert_awaited_once_with(
            "user_test_123",
            new_cpu="2048",
            new_memory="8192",
            _register_only=True,
        )
        update_kwargs = mock_ecs_client.update_service.call_args.kwargs
        assert update_kwargs["taskDefinition"] == "arn:task-def:new"
        assert update_kwargs["networkConfiguration"] == _OWNER_NETWORK_CONFIGURATION
        assert "forceNewDeployment" not in update_kwargs
        mock_ecs_client.get_waiter.assert_called_once_with("services_stable")
        waiter.wait.assert_called_once_with(
            cluster=manager._cluster,
            services=[manager._service_name("user_test_123")],
            WaiterConfig={"Delay": 10, "MaxAttempts": 4},
        )

    @pytest.mark.asyncio
    async def test_refuses_circuit_breaker_rollback_reported_as_stable(self, manager, mock_ecs_client):
        manager.current_container_size = AsyncMock(return_value=("1024", "4096"))
        manager.resize_user_container = AsyncMock(return_value="arn:task-def:new")
        manager._update_container = AsyncMock()
        waiter = MagicMock()
        mock_ecs_client.get_waiter.return_value = waiter
        mock_ecs_client.describe_task_definition.return_value = {
            "taskDefinition": {
                "containerDefinitions": [
                    {
                        "name": "openclaw",
                        "command": [
                            "sh",
                            "-c",
                            '${ISOL8_CONFIG_LOCK_PROTOCOL:-} protocol_path = lock_path + ".isol8-protocol" || exit 1',
                        ],
                        "environment": [
                            {"name": "ISOL8_CONFIG_LOCK_PROTOCOL", "value": "guard-v1"},
                        ],
                    }
                ]
            }
        }
        mock_ecs_client.describe_services.return_value = {
            "services": [
                {
                    "taskDefinition": "arn:task-def:old",
                    "deployments": [
                        {
                            "status": "PRIMARY",
                            "taskDefinition": "arn:task-def:old",
                        }
                    ],
                }
            ]
        }

        with patch("core.containers.ecs_manager.asyncio.to_thread") as mock_to_thread:

            async def passthrough(fn, *args, **kwargs):
                return fn(*args, **kwargs)

            mock_to_thread.side_effect = passthrough
            with pytest.raises(EcsManagerError, match="stabilized on a different task definition"):
                await manager.rollout_config_lock_protocol("user_test_123")

        assert manager._update_container.await_args_list == [
            call("user_test_123", task_definition_arn="arn:task-def:new"),
            call("user_test_123", task_definition_arn="arn:task-def:old"),
        ]

    @pytest.mark.asyncio
    async def test_requires_registered_task_definition(self, manager):
        manager._ecs.describe_task_definition.return_value = {
            "taskDefinition": {
                "containerDefinitions": [
                    {
                        "name": "openclaw",
                        "command": [
                            "sh",
                            "-c",
                            '${ISOL8_CONFIG_LOCK_PROTOCOL:-} protocol_path = lock_path + ".isol8-protocol" || exit 1',
                        ],
                    }
                ]
            }
        }
        manager.current_container_size = AsyncMock(return_value=None)
        manager.resize_user_container = AsyncMock()

        with pytest.raises(EcsManagerError, match="No registered task definition"):
            await manager.rollout_config_lock_protocol("user_test_123")

        manager.resize_user_container.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refuses_rollout_before_recovery_base_is_deployed(self, manager, mock_ecs_client):
        mock_ecs_client.describe_task_definition.return_value = {
            "taskDefinition": {
                "containerDefinitions": [
                    {"name": "openclaw", "command": ["sh", "-c", "exec openclaw gateway"]},
                ]
            }
        }
        manager.current_container_size = AsyncMock(return_value=("1024", "4096"))
        manager.resize_user_container = AsyncMock()

        with pytest.raises(EcsManagerError, match="does not contain fail-fast config-lock recovery"):
            await manager.rollout_config_lock_protocol("user_test_123")

        manager.current_container_size.assert_not_awaited()
        manager.resize_user_container.assert_not_awaited()


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
    async def test_full_provision_refreshes_generation_after_mint(self, manager, mock_ecs_client, mock_efs_client):
        """An EXISTING row whose ECS service is missing takes the
        full-provision path, where create_user_service's upsert MINTS a new
        provision_generation. The caller-supplied generation fence (read off
        the row pre-mint by the SFN runner) must be refreshed after the mint
        — otherwise the Step-3/Step-4 fenced writes compare stale-vs-new and
        this run raises ProvisionSuperseded against ITSELF, exiting
        "superseded" with resources created and the row wedged."""
        mock_ecs_client.describe_services.return_value = {"services": []}

        row_state = {"gen": "gen-OLD"}

        async def _get(owner_id):
            row = _make_container_dict(status="stopped")
            row["provision_generation"] = row_state["gen"]
            return row

        async def _upsert(owner_id, fields):
            row_state["gen"] = fields["provision_generation"]
            row = _make_container_dict(status="provisioning")
            row.update(fields)
            return row

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(side_effect=_get)
            mock_repo.upsert = AsyncMock(side_effect=_upsert)
            mock_repo.update_fields = AsyncMock()
            mock_repo.update_status = AsyncMock()
            mock_repo.update_container_leased = AsyncMock()

            await manager.provision_user_container(
                "user_test_123",
                provider_choice="bedrock_claude",
                claim_id="claim-1",
                generation="gen-OLD",
                attempt_token="tok-1",
            )

            minted = row_state["gen"]
            assert minted != "gen-OLD", "create_user_service should have minted a fresh generation"

            # Every fenced write AFTER the mint must fence on the MINTED
            # generation — the last leased write (start_user_service's or
            # the Step-3 checkpoint's) is necessarily post-mint. The lease
            # writes go through the lazily-imported real module (stubbed by
            # the autouse _provision_lease_stub), not the ecs_manager-bound
            # container_repo name.
            import core.repositories.container_repo as real_repo

            leased = real_repo.update_container_leased
            assert leased.call_count >= 1
            last = leased.call_args_list[-1]
            assert last.kwargs["generation"] == minted

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
            assert call_kwargs["networkConfiguration"] == _OWNER_NETWORK_CONFIGURATION
            dc = call_kwargs.get("deploymentConfiguration") or {}
            cb = dc.get("deploymentCircuitBreaker") or {}
            assert cb.get("enable") is True
            assert cb.get("rollback") is False

    @pytest.mark.asyncio
    async def test_redeploy_preserves_provision_generation(self, manager, mock_ecs_client):
        """A force-redeploy (existing service, running>0) MUST NOT mint or
        overwrite provision_generation (INV-2) -- it's the same container
        incarnation, just a fresh task. The redeploying branch never calls
        create_user_service/upsert (the single mint point), and its
        update_fields writes don't carry the key -- so the row's existing
        value survives untouched."""
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

            mock_repo.upsert.assert_not_called()
            for call in mock_repo.update_fields.call_args_list:
                assert "provision_generation" not in call.args[1]

    @pytest.mark.asyncio
    async def test_scale_from_zero_restart_preserves_provision_generation(
        self, manager, mock_ecs_client, mock_efs_client
    ):
        """A scale-from-zero restart (existing service, desired=0) MUST NOT
        mint or overwrite provision_generation (INV-2) -- same container
        incarnation waking up. This branch never calls
        create_user_service/upsert (the single mint point)."""
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

        with (
            patch("core.containers.ecs_manager.container_repo") as mock_repo,
            patch.object(manager, "_await_running_transition", new_callable=AsyncMock),
            patch.object(manager, "write_user_configs", new_callable=AsyncMock),
        ):
            mock_repo.get_by_owner_id = AsyncMock(
                return_value=_make_container_dict(
                    status="stopped",
                    access_point_id="fsap-user-restart",
                )
            )
            mock_repo.update_fields = AsyncMock()

            await manager.provision_user_container("user_test_123", provider_choice="bedrock_claude")

            mock_repo.upsert.assert_not_called()
            for call in mock_repo.update_fields.call_args_list:
                assert "provision_generation" not in call.args[1]

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
            # container owner's — guards the token_owner_id= plumbing. Two
            # reads: the provider row, then the harness (#harness) row for
            # the OpenCode credential self-heal.
            assert mock_get_tokens.await_count == 2
            provider_call, harness_call = mock_get_tokens.await_args_list
            assert provider_call.kwargs == {"user_id": connector_id, "bound_to": "user_test_123"}
            assert harness_call.kwargs == {
                "user_id": connector_id,
                "bound_to": "user_test_123",
                "purpose": "harness",
            }

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
            assert kw["networkConfiguration"] == _OWNER_NETWORK_CONFIGURATION

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
        canonical 1 vCPU / 4 GB shape — they don't inherit a stale base
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

    @pytest.mark.asyncio
    async def test_leased_poller_heartbeats_the_lease_every_waiting_tick(self, manager):
        """With a claim held, every tick that writes nothing else must issue a
        pure lease-refresh checkpoint (fields=None). The boot window between
        the one-shot task_running write and the terminal running write can run
        ~12 minutes; without the heartbeat the lease read stale at 300s while
        the poller was alive — and /recover's active-lease stand-down keys on
        that freshness, so it fell through and stomped live provisions."""
        rows = [
            _make_container_dict(status="provisioning", substatus="service_created"),
            _make_container_dict(status="provisioning", substatus="task_running"),
            _make_container_dict(status="provisioning", substatus="task_running"),
        ]
        leased_calls = []

        async def track_leased(user_id, claim_id, fields=None, **kw):
            leased_calls.append((fields, kw.get("release_lease", False)))

        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(side_effect=rows)
            mock_repo.update_container_leased = AsyncMock(side_effect=track_leased)
            manager._get_primary_deployment = AsyncMock(return_value={"id": "dep-1", "rolloutState": "COMPLETED"})
            manager._poll_running_task = AsyncMock(return_value=("arn:aws:ecs:task/abc", "10.0.0.1", 1))
            manager.is_healthy = MagicMock(side_effect=[False, False, True])
            with patch("core.billing.bedrock_gate_tokens.expire_superseded_gate_tokens", new=AsyncMock()):
                await manager._await_running_transition(
                    "user_test_123", interval_s=0, claim_id="claim-1", attempt_token="tok-1"
                )

        # EVERY iteration opens with a pure heartbeat (fields=None) — first
        # in the loop so no waiting path (including the no-primary retry
        # `continue`) can skip it — then the real writes: one substatus
        # write, and the terminal running write releasing the lease.
        assert leased_calls[0][0] is None
        heartbeats = [c for c in leased_calls if c[0] is None]
        assert len(heartbeats) == 3  # one per iteration
        substatus_writes = [c for c in leased_calls if c[0] == {"substatus": "task_running"}]
        assert len(substatus_writes) == 1
        assert leased_calls[-1][0]["status"] == "running"
        assert leased_calls[-1][1] is True

    @pytest.mark.asyncio
    async def test_leased_poller_heartbeats_on_the_no_primary_retry_path(self, manager):
        """The no-primary-deployment retry `continue` must ALSO refresh the
        lease — an ECS describe blip longer than the TTL would otherwise make
        a live poller read stale exactly like a dead one."""
        rows = [
            _make_container_dict(status="provisioning", substatus="service_created"),
            _make_container_dict(status="provisioning", substatus="task_running"),
        ]
        leased_calls = []

        async def track_leased(user_id, claim_id, fields=None, **kw):
            leased_calls.append(fields)

        with patch("core.containers.ecs_manager.container_repo") as mock_repo:
            mock_repo.get_by_owner_id = AsyncMock(side_effect=rows)
            mock_repo.update_container_leased = AsyncMock(side_effect=track_leased)
            manager._get_primary_deployment = AsyncMock(
                side_effect=[None, {"id": "dep-1", "rolloutState": "COMPLETED"}]
            )
            manager._poll_running_task = AsyncMock(return_value=("arn:aws:ecs:task/abc", "10.0.0.1", 1))
            manager.is_healthy = MagicMock(return_value=True)
            with patch("core.billing.bedrock_gate_tokens.expire_superseded_gate_tokens", new=AsyncMock()):
                await manager._await_running_transition(
                    "user_test_123", interval_s=0, claim_id="claim-1", attempt_token="tok-1"
                )

        # Iteration 1 hit the no-primary `continue` — its heartbeat must
        # still have fired before the branch.
        assert leased_calls[0] is None
        assert leased_calls[1] is None  # iteration 2's opening heartbeat
        assert leased_calls[-1]["status"] == "running"


# ---------------------------------------------------------------------------
# reprovision_for_user — full reprovision, not stop+start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reprovision_for_user_scales_to_zero_then_full_reprovision(manager):
    """The admin reprovision must run a FULL reprovision: scale to 0 first (so
    provision takes its scale-from-zero branch that re-registers the task def +
    rewrites the EFS config), wait for the old task to fully drain (single EFS
    SQLite writer), then dispatch dispatch_provision. The old stale-task-def
    force-redeploy path (start_user_service) must NOT be used.
    """
    order = []
    dispatched = []

    async def _fake_stop(uid, *, reason="manual"):
        order.append("stop")

    async def _fake_drain(uid, timeout_s=90.0):
        order.append("drain")
        return True

    async def _fake_bg(owner_id):
        order.append("provision")
        dispatched.append(owner_id)

    manager.stop_user_service = AsyncMock(side_effect=_fake_stop)
    manager._wait_for_service_drained = AsyncMock(side_effect=_fake_drain)
    manager.start_user_service = AsyncMock()
    with patch(
        "core.services.provision_dispatch.dispatch_provision",
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
    assert dispatched == ["org_abc"]
    # The old stale-task-def scale-up path must NOT be used.
    manager.start_user_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_service_drained_returns_true_when_no_tasks(manager, mock_ecs_client):
    """No task records at all (never started / long expired) — drained."""
    mock_ecs_client.list_tasks.return_value = {"taskArns": []}
    assert await manager._wait_for_service_drained("user_test_123", timeout_s=9.0) is True


@pytest.mark.asyncio
async def test_wait_for_service_drained_true_when_all_tasks_stopped(manager, mock_ecs_client):
    mock_ecs_client.list_tasks.side_effect = lambda **kw: (
        {"taskArns": ["arn:task/1"]} if kw.get("desiredStatus") == "STOPPED" else {"taskArns": []}
    )
    mock_ecs_client.describe_tasks.return_value = {"tasks": [{"lastStatus": "STOPPED"}]}
    assert await manager._wait_for_service_drained("user_test_123", timeout_s=9.0) is True


@pytest.mark.asyncio
async def test_wait_for_service_drained_not_fooled_by_stopping_task(manager, mock_ecs_client):
    """A task in STOPPING is NOT drained, whatever the service counts say.

    runningCount hits 0 the moment the task leaves RUNNING, but it then sits
    in STOPPING for up to its 120s stopTimeout while litestream performs the
    final replica sync. Draining on counts let the replica-prefix delete and
    the OAuth seed harvest act on a replica missing that final sync — the
    harvest would then write a rotated-past-the-seed but already-SPENT
    refresh token back to DDB."""
    mock_ecs_client.describe_services.return_value = {"services": [{"runningCount": 0, "pendingCount": 0}]}
    mock_ecs_client.list_tasks.side_effect = lambda **kw: (
        {"taskArns": ["arn:task/1"]} if kw.get("desiredStatus") == "STOPPED" else {"taskArns": []}
    )
    mock_ecs_client.describe_tasks.return_value = {"tasks": [{"lastStatus": "STOPPING"}]}
    with patch("core.containers.ecs_manager.asyncio.sleep", new=AsyncMock()):
        assert await manager._wait_for_service_drained("user_test_123", timeout_s=9.0) is False


@pytest.mark.asyncio
async def test_wait_for_service_drained_service_record_gone_reads_drained(manager, mock_ecs_client):
    """ServiceNotFound = ECS garbage-collected the service record, long after
    its tasks stopped — a retried wipe must not hang on it."""
    mock_ecs_client.list_tasks.side_effect = ClientError(
        {"Error": {"Code": "ServiceNotFoundException", "Message": "not found"}}, "ListTasks"
    )
    assert await manager._wait_for_service_drained("user_test_123", timeout_s=9.0) is True


@pytest.mark.asyncio
async def test_wait_for_service_drained_times_out_returns_false(manager, mock_ecs_client):
    """If the task never stops, returns False so the caller proceeds rather
    than wedging an admin reprovision on a task that refuses to stop."""
    mock_ecs_client.list_tasks.side_effect = lambda **kw: (
        {"taskArns": ["arn:task/1"]} if kw.get("desiredStatus") == "RUNNING" else {"taskArns": []}
    )
    mock_ecs_client.describe_tasks.return_value = {"tasks": [{"lastStatus": "RUNNING"}]}
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
    async def test_superseded_upsert_stops_before_every_later_side_effect(self, manager, monkeypatch):
        """The upsert ITSELF can now detect supersession — a takeover landing
        after the pre-upsert checkpoint but before the put — and when it does,
        this run must stop dead. Its own CAS retry used to rebuild against the
        successor's claim and write it back out, letting the loser return and
        go on to mint an EFS access point and a task definition alongside the
        winner: the double-provision hazard, arriving through the fence."""
        from core.repositories.container_repo import ProvisionSuperseded

        monkeypatch.setattr("core.repositories.container_repo.update_container_leased", AsyncMock())
        monkeypatch.setattr(
            "core.repositories.container_repo.upsert",
            AsyncMock(side_effect=ProvisionSuperseded("lease moved during upsert")),
        )
        persist = AsyncMock()
        monkeypatch.setattr("core.billing.bedrock_gate_tokens.persist_gate_token_hash", persist)
        access_point = MagicMock(return_value="fsap-x")
        task_def = MagicMock(return_value="arn:task-def")
        monkeypatch.setattr(manager, "_create_access_point", access_point)
        monkeypatch.setattr(manager, "_register_task_definition", task_def)
        manager._ecs.create_service = MagicMock()

        with pytest.raises(ProvisionSuperseded):
            await manager.create_user_service(
                "user_sup", "gw-token", claim_id="stale-claim", bedrock_gate_token_hash="hash_loser"
            )

        persist.assert_not_awaited()
        access_point.assert_not_called()
        task_def.assert_not_called()
        manager._ecs.create_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_takeover_after_the_upsert_stops_the_loser_before_any_artifact(self, manager, monkeypatch):
        """A REAL takeover, landing after the upsert's put has completed.

        The upsert's own CAS only sees a takeover inside its read-to-write
        window; one landing after it returns is invisible there. Everything from
        that point to the pre-create checkpoint was unfenced — the gate-pointer
        re-assert, the EFS access point, the task-definition revision — so the
        loser deposited artifacts alongside the winner. Real moto row, real
        try_claim_provision, real takeover_provision, real checkpoint: the
        checkpoint is what has to catch this.
        """
        import boto3
        from moto import mock_aws

        from core.repositories import container_repo
        from core.repositories.container_repo import ProvisionSuperseded

        with mock_aws():
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            ddb.create_table(
                TableName="test-containers",
                KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            monkeypatch.setattr("core.dynamodb._table_prefix", "test-")
            monkeypatch.setattr("core.dynamodb._dynamodb_resource", ddb)
            # Undo the module-level lease stubs: this test needs the real ones.
            monkeypatch.setattr("core.repositories.container_repo.update_container_leased", _REAL_LEASED)
            monkeypatch.setattr("core.repositories.container_repo.takeover_provision", _REAL_TAKEOVER)
            # ...and the row read: _start_guards_clear stubs it to None, which
            # would make the real upsert's CAS snapshot see an empty row.
            monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", _REAL_GET_BY_OWNER)

            claim = await container_repo.try_claim_provision("user_race", claim_id="claim-loser")

            real_upsert = container_repo.upsert
            state: dict = {}

            async def upsert_then_takeover(owner_id, fields):
                # The put lands (fenced, and it succeeds), THEN a deliberate
                # door takes the lease over. Exactly the window at issue.
                row = await real_upsert(owner_id, fields)
                state["winner"] = await container_repo.takeover_provision(owner_id)
                return row

            monkeypatch.setattr("core.repositories.container_repo.upsert", upsert_then_takeover)
            # The REAL persist: with the hash DIFFERING from the row's, its swap
            # runs and its own lease clause is what refuses. (The sibling test
            # covers the other branch, where the hash already matches and the
            # swap never runs.)
            monkeypatch.setattr("core.billing.bedrock_gate_tokens.persist_gate_token_hash", _REAL_PERSIST_HASH)
            access_point = MagicMock(return_value="fsap-x")
            task_def = MagicMock(return_value="arn:task-def")
            monkeypatch.setattr(manager, "_create_access_point", access_point)
            monkeypatch.setattr(manager, "_register_task_definition", task_def)
            manager._ecs.create_service = MagicMock()

            with pytest.raises(ProvisionSuperseded):
                await manager.create_user_service(
                    "user_race", "gw-token", claim_id=claim, bedrock_gate_token_hash="hash_loser"
                )

            access_point.assert_not_called()
            task_def.assert_not_called()
            manager._ecs.create_service.assert_not_called()
            # The winner still holds the lease, and the loser's hash never
            # reached the row — the swap refused it.
            row = await container_repo.get_by_owner_id("user_race")
            assert row["provision_claim_id"] == state["winner"] != claim
            assert "bedrock_gate_token_hash" not in row

    @pytest.mark.asyncio
    async def test_takeover_during_persist_is_caught_when_the_hash_is_already_current(self, manager, monkeypatch):
        """The gap the swap's own lease clause cannot see.

        persist_gate_token_hash short-circuits when the row already points at
        this run's hash — the ordinary case, since mint_gate_token's own swap
        usually landed it. That fast path writes NOTHING, so the lease clause
        added to the swap never runs, and a takeover landing between the last
        checkpoint and it went undetected all the way into _create_access_point
        and _register_task_definition.

        The fix is not a second lease check inside persist (it has nothing to
        validate — it writes nothing); it is that the checkpoint sits AFTER
        persist, at the last moment before the first call that cannot carry a
        condition of its own.
        """
        import boto3
        from moto import mock_aws

        from core.repositories import container_repo
        from core.repositories.container_repo import ProvisionSuperseded

        with mock_aws():
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            ddb.create_table(
                TableName="test-containers",
                KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            monkeypatch.setattr("core.dynamodb._table_prefix", "test-")
            monkeypatch.setattr("core.dynamodb._dynamodb_resource", ddb)
            monkeypatch.setattr("core.repositories.container_repo.update_container_leased", _REAL_LEASED)
            monkeypatch.setattr("core.repositories.container_repo.takeover_provision", _REAL_TAKEOVER)
            monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", _REAL_GET_BY_OWNER)

            claim = await container_repo.try_claim_provision("user_fast", claim_id="claim-loser")
            # The row ALREADY points at this run's hash, so persist will
            # short-circuit — the branch the swap's lease clause cannot reach.
            await container_repo.swap_gate_token_hash(
                "user_fast", new_hash="hash_same", superseded=[], expected_prior_hash=None
            )

            state: dict = {}

            async def takeover_then_persist(owner_id, token_hash, **kw):
                # A deliberate door takes the lease over AFTER the upsert (and
                # after where the checkpoint used to sit), then the REAL persist
                # runs and writes nothing.
                state["winner"] = await container_repo.takeover_provision(owner_id)
                return await _REAL_PERSIST_HASH(owner_id, token_hash, **kw)

            monkeypatch.setattr("core.billing.bedrock_gate_tokens.persist_gate_token_hash", takeover_then_persist)
            access_point = MagicMock(return_value="fsap-x")
            task_def = MagicMock(return_value="arn:task-def")
            monkeypatch.setattr(manager, "_create_access_point", access_point)
            monkeypatch.setattr(manager, "_register_task_definition", task_def)
            manager._ecs.create_service = MagicMock()

            with pytest.raises(ProvisionSuperseded):
                await manager.create_user_service(
                    "user_fast", "gw-token", claim_id=claim, bedrock_gate_token_hash="hash_same"
                )

            assert state["winner"] != claim
            access_point.assert_not_called()
            task_def.assert_not_called()
            manager._ecs.create_service.assert_not_called()
            # The fast path wrote nothing, so the pointer is untouched.
            row = await container_repo.get_by_owner_id("user_fast")
            assert row["bedrock_gate_token_hash"] == "hash_same"
            assert row["provision_claim_id"] == state["winner"]

    @pytest.mark.asyncio
    async def test_no_takeover_runs_straight_through_to_create_service(self, manager, monkeypatch):
        """The guard against over-fencing: with the lease held throughout, the
        new checkpoint is a no-op and the provision reaches create_service."""
        monkeypatch.setattr("core.repositories.container_repo.upsert", AsyncMock())
        monkeypatch.setattr("core.repositories.container_repo.update_fields", AsyncMock())
        persist = AsyncMock()
        monkeypatch.setattr("core.billing.bedrock_gate_tokens.persist_gate_token_hash", persist)
        monkeypatch.setattr(manager, "_create_access_point", MagicMock(return_value="fsap-x"))
        monkeypatch.setattr(manager, "_register_task_definition", MagicMock(return_value="arn:task-def"))
        manager._ecs.create_service = MagicMock()

        await manager.create_user_service(
            "user_ok", "gw-token", claim_id="claim-held", bedrock_gate_token_hash="hash_ok"
        )

        persist.assert_awaited_once()
        manager._ecs.create_service.assert_called_once()

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

    # --- the row writes BETWEEN the checkpoints ------------------------------
    #
    # The checkpoints fence the moments they run; the substatus/artifact row
    # writes that follow them used to go through the unfenced `update_fields`
    # (conditioned only on the row existing), so a run superseded between two
    # checkpoints still stamped its own access_point_id / task_definition_arn
    # over the winner's row — a row describing a mixture of two provisions.

    @pytest.mark.asyncio
    async def test_takeover_at_the_access_point_write_cannot_stamp_the_winners_row(self, manager, monkeypatch):
        """Takeover lands after the pre-artifact checkpoint, while the loser's
        EFS access point is being created. The loser's efs_created write must
        refuse — the winner's access point stays on the row."""
        from core.repositories.container_repo import ProvisionSuperseded

        with _real_containers_table(monkeypatch) as container_repo:
            claim = await container_repo.try_claim_provision("user_efs", claim_id="claim-loser")
            state: dict = {}

            def create_ap_then_takeover(user_id, owner_type="personal"):
                async def winner():
                    won = await container_repo.takeover_provision(user_id)
                    # The winner gets as far as its OWN efs_created write.
                    await container_repo.update_container_leased(
                        user_id, won, {"access_point_id": "fsap-winner", "substatus": "efs_created"}
                    )
                    return won

                state["winner"] = _run_off_loop(winner)
                return "fsap-loser"

            monkeypatch.setattr(manager, "_create_access_point", create_ap_then_takeover)
            register = MagicMock(return_value="arn:loser-task-def")
            monkeypatch.setattr(manager, "_register_task_definition", register)
            manager._ecs.create_service = MagicMock()

            with pytest.raises(ProvisionSuperseded):
                await manager.create_user_service("user_efs", "gw-token", claim_id=claim)

            row = await container_repo.get_by_owner_id("user_efs")
            assert row["access_point_id"] == "fsap-winner"
            assert row["substatus"] == "efs_created"
            # The loser must not flip the row to error over the winner: the
            # superseded branch re-raises bare, ahead of the rollback handlers.
            assert row["status"] == "provisioning"
            assert row["provision_claim_id"] == state["winner"] != claim
            register.assert_not_called()
            manager._ecs.create_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_takeover_at_the_task_definition_write_cannot_stamp_the_winners_row(self, manager, monkeypatch):
        """Same fence, one step later: a takeover landing while the loser
        registers its task definition must not put that revision's ARN on the
        winner's row — the row would then name a task def the live service
        never runs."""
        from core.repositories.container_repo import ProvisionSuperseded

        with _real_containers_table(monkeypatch) as container_repo:
            claim = await container_repo.try_claim_provision("user_td", claim_id="claim-loser")
            state: dict = {}

            def register_then_takeover(*args, **kwargs):
                async def winner():
                    won = await container_repo.takeover_provision("user_td")
                    await container_repo.update_container_leased(
                        "user_td", won, {"task_definition_arn": "arn:winner", "substatus": "task_registered"}
                    )
                    return won

                state["winner"] = _run_off_loop(winner)
                return "arn:loser"

            monkeypatch.setattr(manager, "_create_access_point", MagicMock(return_value="fsap-loser"))
            monkeypatch.setattr(manager, "_register_task_definition", register_then_takeover)
            manager._ecs.create_service = MagicMock()

            with pytest.raises(ProvisionSuperseded):
                await manager.create_user_service("user_td", "gw-token", claim_id=claim)

            row = await container_repo.get_by_owner_id("user_td")
            assert row["task_definition_arn"] == "arn:winner"
            assert row["substatus"] == "task_registered"
            assert row["status"] == "provisioning"
            assert row["provision_claim_id"] == state["winner"] != claim
            # The efs write DID land — the loser still held the lease then.
            assert row["access_point_id"] == "fsap-loser"
            manager._ecs.create_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_fenced_provision_without_a_takeover_writes_both_artifacts(self, manager, monkeypatch):
        """The over-fencing guard: lease held throughout, so every write lands
        and the row ends up describing this run's artifacts."""
        with _real_containers_table(monkeypatch) as container_repo:
            claim = await container_repo.try_claim_provision("user_fenced", claim_id="claim-held")
            monkeypatch.setattr(manager, "_create_access_point", MagicMock(return_value="fsap-x"))
            monkeypatch.setattr(manager, "_register_task_definition", MagicMock(return_value="arn:task-def"))
            manager._ecs.create_service = MagicMock()

            await manager.create_user_service("user_fenced", "gw-token", claim_id=claim)

            row = await container_repo.get_by_owner_id("user_fenced")
            assert row["access_point_id"] == "fsap-x"
            assert row["task_definition_arn"] == "arn:task-def"
            assert row["substatus"] == "service_created"
            assert row["status"] == "provisioning"
            # Every fenced write refreshes the lease rather than releasing it.
            assert row["provision_claim_id"] == claim
            manager._ecs.create_service.assert_called_once()

    @pytest.mark.asyncio
    async def test_claimless_create_user_service_still_writes_its_row(self, manager, monkeypatch):
        """``routers/debug.py`` shape: no lease exists on the row at all, so
        the writes must stay unfenced. The lease primitives here are REAL — a
        write that tried to condition on a claim would find none and raise."""
        with _real_containers_table(monkeypatch) as container_repo:
            monkeypatch.setattr(manager, "_create_access_point", MagicMock(return_value="fsap-x"))
            monkeypatch.setattr(manager, "_register_task_definition", MagicMock(return_value="arn:task-def"))
            manager._ecs.create_service = MagicMock()

            await manager.create_user_service("user_debug", "gw-token")

            row = await container_repo.get_by_owner_id("user_debug")
            assert row["access_point_id"] == "fsap-x"
            assert row["task_definition_arn"] == "arn:task-def"
            assert row["substatus"] == "service_created"
            assert "provision_claim_id" not in row

    @pytest.mark.asyncio
    async def test_superseded_row_write_does_not_release_the_row_to_error(self, manager, monkeypatch):
        """Through the real entry point: a ProvisionSuperseded raised by a
        fenced row write must reach ``provision_user_container``'s superseded
        branch (metric + bare re-raise), NOT its error release — the row
        belongs to the winner now."""
        from core.repositories.container_repo import ProvisionSuperseded

        with _real_containers_table(monkeypatch) as container_repo:
            claim = await container_repo.try_claim_provision("user_rel", claim_id="claim-loser")
            # Bedrock provision, minus the control planes it talks to (the AIP
            # + role mint are stubbed by the autouse _mock_bedrock_mint).
            monkeypatch.setattr(
                "core.billing.bedrock_gate_tokens.mint_gate_token", AsyncMock(return_value="gate-token")
            )
            monkeypatch.setattr("core.billing.bedrock_gate_tokens.hash_token", MagicMock(return_value="gate-hash"))
            monkeypatch.setattr("core.billing.bedrock_gate_tokens.persist_gate_token_hash", AsyncMock())
            monkeypatch.setattr("core.billing.bedrock_gate_url.gate_url", MagicMock(return_value="https://gate"))
            monkeypatch.setattr(manager, "_service_exists", MagicMock(return_value=None))
            state: dict = {}

            def create_ap_then_takeover(user_id, owner_type="personal"):
                state["winner"] = _run_off_loop(lambda: container_repo.takeover_provision(user_id))
                return "fsap-loser"

            monkeypatch.setattr(manager, "_create_access_point", create_ap_then_takeover)
            monkeypatch.setattr(manager, "_register_task_definition", MagicMock(return_value="arn:loser"))
            manager._ecs.create_service = MagicMock()

            with pytest.raises(ProvisionSuperseded):
                await manager.provision_user_container("user_rel", provider_choice="bedrock_claude", claim_id=claim)

            row = await container_repo.get_by_owner_id("user_rel")
            assert row["status"] == "provisioning"
            assert "last_error" not in row
            assert row["provision_claim_id"] == state["winner"] != claim
            # Nothing of the loser's reached the row, and it stopped before the
            # service create rather than running on to a later checkpoint.
            assert "access_point_id" not in row
            manager._ecs.create_service.assert_not_called()
            container_repo.release_provision_to_error.assert_not_awaited()

    # --- the restart branch (service exists, desiredCount == 0) --------------
    #
    # This branch never calls create_user_service, so its writes are fenced by
    # _provision_user_container_inner's own claim. The task-def write is the
    # widest window of the lot: the checkpoint before the config write is
    # separated from it by the plan lookup, the task-def register and the ECS
    # update_service call.

    async def _seed_restart_row(self, container_repo, owner):
        """Claim the row and give it the shape the restart branch needs: an
        ACTIVE service at desiredCount 0, with an access point already on
        record so the branch re-registers rather than falling back to
        start_user_service."""
        claim = await container_repo.try_claim_provision(owner, claim_id="claim-loser")
        await container_repo.update_fields(
            owner,
            {
                "service_name": f"openclaw-{owner}",
                "gateway_token": "gw-existing",
                "hook_token": "hook-existing",
                "access_point_id": "fsap-existing",
                "task_definition_arn": "arn:old",
            },
        )
        return claim

    def _stub_restart_branch(self, manager, monkeypatch, *, register):
        monkeypatch.setattr(
            manager,
            "_service_exists",
            MagicMock(return_value={"status": "ACTIVE", "desiredCount": 0, "runningCount": 0}),
        )
        monkeypatch.setattr(manager, "write_user_configs", AsyncMock())
        monkeypatch.setattr(manager, "_size_for_owner", AsyncMock(return_value=("1024", "4096")))
        monkeypatch.setattr(manager, "_register_task_definition", register)
        monkeypatch.setattr(manager, "_deregister_task_definition", MagicMock())
        monkeypatch.setattr("core.billing.bedrock_gate_tokens.mint_gate_token", AsyncMock(return_value="gate-token"))
        monkeypatch.setattr("core.billing.bedrock_gate_tokens.hash_token", MagicMock(return_value="gate-hash"))
        monkeypatch.setattr("core.billing.bedrock_gate_tokens.persist_gate_token_hash", AsyncMock())
        monkeypatch.setattr("core.billing.bedrock_gate_url.gate_url", MagicMock(return_value="https://gate"))
        manager._ecs.update_service = MagicMock()

    @pytest.mark.asyncio
    async def test_takeover_while_re_registering_cannot_stamp_the_task_def_arn(self, manager, monkeypatch):
        """The restart branch's task-def write. A takeover landing between the
        pre-config checkpoint and this write must not leave the winner's row
        naming the loser's revision — the winner's service never runs it."""
        from core.repositories.container_repo import ProvisionSuperseded

        with _real_containers_table(monkeypatch) as container_repo:
            claim = await self._seed_restart_row(container_repo, "user_restart")
            state: dict = {}

            def register_then_takeover(*args, **kwargs):
                async def winner():
                    won = await container_repo.takeover_provision("user_restart")
                    await container_repo.update_container_leased(
                        "user_restart", won, {"task_definition_arn": "arn:winner"}
                    )
                    return won

                state["winner"] = _run_off_loop(winner)
                return "arn:loser"

            self._stub_restart_branch(manager, monkeypatch, register=register_then_takeover)

            with pytest.raises(ProvisionSuperseded):
                await manager.provision_user_container("user_restart", provider_choice="bedrock_claude", claim_id=claim)

            row = await container_repo.get_by_owner_id("user_restart")
            assert row["task_definition_arn"] == "arn:winner"
            assert row["provision_claim_id"] == state["winner"] != claim
            # The loser stopped AT the write, so it never went on to deregister
            # the revision the row used to name.
            manager._deregister_task_definition.assert_not_called()

    @pytest.mark.asyncio
    async def test_restart_branch_with_the_lease_held_writes_the_new_task_def_arn(self, manager, monkeypatch):
        """The over-fencing guard: no takeover, so the fenced write lands and
        the row names this run's revision."""
        with _real_containers_table(monkeypatch) as container_repo:
            claim = await self._seed_restart_row(container_repo, "user_restart_ok")
            self._stub_restart_branch(manager, monkeypatch, register=MagicMock(return_value="arn:new"))

            await manager.provision_user_container("user_restart_ok", provider_choice="bedrock_claude", claim_id=claim)

            row = await container_repo.get_by_owner_id("user_restart_ok")
            assert row["task_definition_arn"] == "arn:new"
            assert row["substatus"] == "restarting"
            assert row["status"] == "provisioning"
            # The fenced write refreshes the lease rather than releasing it.
            assert row["provision_claim_id"] == claim
            manager._ecs.update_service.assert_called_once()
            assert manager._ecs.update_service.call_args.kwargs["desiredCount"] == 1
            manager._deregister_task_definition.assert_called_once_with("arn:old")

    @pytest.mark.asyncio
    async def test_superseded_task_def_write_does_not_release_the_row_to_error(self, manager, monkeypatch):
        """The escape path for THIS site specifically. Unlike its siblings it
        sits inside a try whose `except Exception` rebrands anything it catches
        as an EcsManagerError — which would send provision_user_container down
        its classify-and-release path instead of its superseded branch."""
        from core.repositories.container_repo import ProvisionSuperseded

        with _real_containers_table(monkeypatch) as container_repo:
            claim = await self._seed_restart_row(container_repo, "user_restart_err")
            state: dict = {}

            def register_then_takeover(*args, **kwargs):
                state["winner"] = _run_off_loop(lambda: container_repo.takeover_provision("user_restart_err"))
                return "arn:loser"

            self._stub_restart_branch(manager, monkeypatch, register=register_then_takeover)

            with pytest.raises(ProvisionSuperseded):
                await manager.provision_user_container(
                    "user_restart_err", provider_choice="bedrock_claude", claim_id=claim
                )

            row = await container_repo.get_by_owner_id("user_restart_err")
            assert row["status"] == "provisioning"
            assert row["substatus"] != "provision_failed"
            assert "last_error" not in row
            assert row["provision_claim_id"] == state["winner"] != claim
            # Not merely a no-op release — the loser must never REACH it.
            container_repo.release_provision_to_error.assert_not_awaited()

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


class TestProvisionFailureClassification:
    """provision_user_container's own except block records the failure CAUSE
    (last_error + retryable) on the error transition. This is the release that
    actually lands on the common inner-failure path (it fires before
    dispatch_provision's, which then no-ops on the flipped status), and it is
    the ONLY release for the deliberate doors (conversion / recover / admin)
    that never pass through dispatch_provision — so classification must happen
    here or a deterministic 4xx would loop forever as retryable."""

    @pytest.mark.asyncio
    async def test_deterministic_inner_failure_records_not_retryable(self, manager, monkeypatch):
        """A 4xx PaperclipApiError from the inner provision is deterministic —
        the row's error transition records retryable=False + a cause, and the
        exception still propagates."""
        from core.services.paperclip_admin_client import PaperclipApiError

        monkeypatch.setattr(
            manager,
            "_provision_user_container_inner",
            AsyncMock(side_effect=PaperclipApiError("bad company slug", 400, {})),
        )
        release = AsyncMock()
        monkeypatch.setattr("core.repositories.container_repo.release_provision_to_error", release)

        with pytest.raises(PaperclipApiError):
            await manager.provision_user_container("user_1", provider_choice="bedrock_claude", claim_id="claim-x")

        assert release.await_args.args == ("user_1", "claim-x")
        assert release.await_args.kwargs["retryable"] is False
        assert release.await_args.kwargs["last_error"]

    @pytest.mark.asyncio
    async def test_transient_inner_failure_records_retryable(self, manager, monkeypatch):
        """An EcsManagerError (AWS Spot / throttle / capacity) is transient
        infra — retryable=True so the poll silently re-dispatches."""
        monkeypatch.setattr(
            manager,
            "_provision_user_container_inner",
            AsyncMock(side_effect=EcsManagerError("Failed to create ECS service: ThrottlingException", "user_1")),
        )
        release = AsyncMock()
        monkeypatch.setattr("core.repositories.container_repo.release_provision_to_error", release)

        with pytest.raises(EcsManagerError):
            await manager.provision_user_container("user_1", provider_choice="bedrock_claude", claim_id="claim-x")

        assert release.await_args.kwargs["retryable"] is True
        assert release.await_args.kwargs["last_error"]

    @pytest.mark.asyncio
    async def test_superseded_inner_failure_does_not_release(self, manager, monkeypatch):
        """A superseded run is caught by its own branch BEFORE the generic
        handler — it must NOT release to error (the row belongs to the
        superseding run). Guards that wiring the classifier into the generic
        except didn't disturb the ProvisionSuperseded path."""
        from core.repositories.container_repo import ProvisionSuperseded

        monkeypatch.setattr(
            manager,
            "_provision_user_container_inner",
            AsyncMock(side_effect=ProvisionSuperseded("taken over")),
        )
        release = AsyncMock()
        monkeypatch.setattr("core.repositories.container_repo.release_provision_to_error", release)

        with pytest.raises(ProvisionSuperseded):
            await manager.provision_user_container("user_1", provider_choice="bedrock_claude", claim_id="claim-x")

        release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_real_inner_ecs_failure_lands_error_with_cause(self, manager, mock_ecs_client, monkeypatch):
        """END-TO-END through the REAL inner path (the false-green the sibling
        tests above miss by mocking ``_provision_user_container_inner`` wholesale).

        A deterministic AWS failure raised INSIDE ``create_user_service``
        (create_service ClientError) must leave the row ``status="error"`` WITH
        ``retryable`` AND ``last_error`` populated — proving the eight scattered
        pre-emptive ``status="error"`` writes no longer pre-empt the outer
        claim-fenced ``release_provision_to_error``. Fails against pre-consolidation
        code: an inner site flips ``status="error"`` first, so the release's
        ``status = provisioning`` condition no-ops and the cause never lands."""

        class _FakeContainerStore:
            """Stateful container_repo stand-in that models the ONE behaviour the
            consolidation turns on: release_provision_to_error lands the error
            transition (+ retryable + last_error) only while the row is still
            ``provisioning`` under the held claim. A pre-emptive inner
            ``status="error"`` flip makes it no-op — exactly the T3 Critical."""

            def __init__(self):
                self.row = {
                    "owner_id": "user_1",
                    "id": "id-1",
                    "created_at": "t0",
                    "status": "provisioning",
                    "substatus": "claimed",
                    "provision_claim_id": "claim-x",
                }

            async def get_by_owner_id(self, owner_id):
                return dict(self.row) if self.row is not None else None

            async def upsert(self, owner_id, fields):
                self.row.update(fields)
                self.row["owner_id"] = owner_id
                return dict(self.row)

            async def update_fields(self, owner_id, fields):
                if self.row is None:
                    return None
                self.row.update(fields)
                return dict(self.row)

            async def update_status(self, owner_id, status, substatus=None):
                return await self.update_fields(owner_id, {"status": status, "substatus": substatus})

            async def update_container_leased(
                self, owner_id, claim_id, fields=None, *, generation=None, attempt_token=None
            ):
                from core.repositories.container_repo import ProvisionSuperseded

                if self.row is None or self.row.get("provision_claim_id") != claim_id:
                    raise ProvisionSuperseded("lease lost")
                if fields:
                    self.row.update(fields)

            async def release_provision_to_error(
                self, owner_id, claim_id, *, last_error=None, retryable=True, generation=None, attempt_token=None
            ):
                # Faithful mirror of the real conditional write: fencing token
                # AND status == "provisioning", else a silent no-op.
                if self.row is None or self.row.get("provision_claim_id") != claim_id:
                    return
                if self.row.get("status") != "provisioning":
                    return
                self.row["status"] = "error"
                self.row["substatus"] = "provision_failed"
                self.row["retryable"] = retryable
                if last_error is not None:
                    self.row["last_error"] = str(last_error)[:500]

        store = _FakeContainerStore()

        # No pre-existing service -> full-provisioning path -> create_user_service.
        mock_ecs_client.describe_services.return_value = {"services": []}
        # Deterministic AWS failure inside create_user_service's create_service.
        mock_ecs_client.create_service.side_effect = ClientError(
            {"Error": {"Code": "ClusterNotFoundException", "Message": "not found"}},
            "CreateService",
        )

        # Wire the store into BOTH bindings the provision path uses, and let the
        # REAL classifier run. Overrides the autouse _provision_lease_stub, which
        # replaces release/leased with inert AsyncMocks.
        monkeypatch.setattr("core.containers.ecs_manager.container_repo", store)
        monkeypatch.setattr("core.repositories.container_repo.update_container_leased", store.update_container_leased)
        monkeypatch.setattr(
            "core.repositories.container_repo.release_provision_to_error", store.release_provision_to_error
        )
        # byo_key path: only key_service.get_secret_arn needs stubbing (no gate
        # token machinery); the per-owner role shell mint is stubbed autouse.
        monkeypatch.setattr(
            "core.services.key_service.get_secret_arn",
            AsyncMock(return_value="arn:aws:secretsmanager:us-east-1:123:secret:isol8/dev/user-keys/user_1/openai-A"),
        )

        with pytest.raises(EcsManagerError, match="Failed to create ECS service"):
            await manager.provision_user_container(
                "user_1", provider_choice="byo_key", byo_provider="openai", claim_id="claim-x"
            )

        # The fenced release genuinely landed the terminal transition AND the cause.
        assert store.row["status"] == "error"
        assert store.row.get("retryable") is True, "retryable must land on the row (release owns the transition)"
        assert store.row.get("last_error"), "last_error cause must land on the row"
        assert "Failed to create ECS service" in store.row["last_error"]
