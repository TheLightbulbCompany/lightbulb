"""Tests for hook_token provisioning + env injection in EcsManager.

Two surfaces under test:

1. ``_build_per_user_environment`` — the small helper that layers
   ``OPENCLAW_HOOK_TOKEN`` onto whatever per-user env entries the
   caller already supplied (e.g. CDK base entries, BYOK Secrets
   wiring metadata). Pure function, easy to unit-test directly.

2. ``provision_user_container`` — the full-provisioning path
   generates a fresh hook token alongside the gateway token and
   persists it via the ``container_repo.upsert`` call inside
   ``create_user_service`` (atomic with row creation, so brand-new
   rows land with the token set). Reuses the existing harness from
   ``test_ecs_manager.py`` (mock_ecs_client, mock_efs_client, manager
   fixtures) for the AWS stub layer.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError  # noqa: F401  -- imported by harness

from core.containers.ecs_manager import EcsManager


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_ecs_manager.py — kept local so this test file is
# self-contained and doesn't take an import-from dependency on a sibling
# test module).
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ecs_client():
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
    client.describe_services.return_value = {"services": []}
    client.list_tasks.return_value = {"taskArns": []}
    client.describe_tasks.return_value = {"tasks": []}
    client.deregister_task_definition.return_value = {}
    return client


@pytest.fixture
def mock_efs_client():
    client = MagicMock()
    client.create_access_point.return_value = {"AccessPointId": "fsap-user123"}
    client.delete_access_point.return_value = {}
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
        s.EFS_FILE_SYSTEM_ID = "fs-test123"
        yield s


@pytest.fixture
def manager(mock_settings, mock_ecs_client, mock_efs_client):
    with patch("core.containers.ecs_manager.boto3") as mock_boto3:

        def client_factory(service, **kwargs):
            if service == "ecs":
                return mock_ecs_client
            elif service == "efs":
                return mock_efs_client
            return MagicMock()

        mock_boto3.client.side_effect = client_factory
        mgr = EcsManager()
    return mgr


def _make_container_dict(
    user_id="user_test_123",
    service_name="openclaw-user_test_123-f4ae64abb2db",
    gateway_token="tok-abc",
    status="provisioning",
):
    return {
        "owner_id": user_id,
        "service_name": service_name,
        "gateway_token": gateway_token,
        "status": status,
    }


# ---------------------------------------------------------------------------
# _build_per_user_environment — pure helper
# ---------------------------------------------------------------------------


def test_environment_for_task_includes_openclaw_hook_token():
    """When per-user env entries are layered, OPENCLAW_HOOK_TOKEN is among them."""
    from core.containers import ecs_manager

    env = ecs_manager._build_per_user_environment(
        hook_token="abc-token",
        existing=[],
    )

    names = {e["name"] for e in env}
    assert "OPENCLAW_HOOK_TOKEN" in names
    token_entry = next(e for e in env if e["name"] == "OPENCLAW_HOOK_TOKEN")
    assert token_entry["value"] == "abc-token"


def test_environment_for_task_preserves_caller_supplied_entries():
    """Helper must not clobber caller-supplied entries that CDK base /
    pre-stage flows may have layered in (e.g. legacy CODEX_HOME)."""
    from core.containers import ecs_manager

    env = ecs_manager._build_per_user_environment(
        hook_token="abc-token",
        existing=[
            {"name": "CODEX_HOME", "value": "/mnt/efs/users/u_1/.codex"},
            {"name": "FOO", "value": "bar"},
        ],
    )

    names = {e["name"] for e in env}
    assert {"OPENCLAW_HOOK_TOKEN", "CODEX_HOME", "FOO"}.issubset(names)


def test_environment_for_task_overrides_stale_hook_token():
    """If a caller-supplied list already has an OPENCLAW_HOOK_TOKEN entry
    (e.g., backfill reading the old task-def), the new value wins."""
    from core.containers import ecs_manager

    env = ecs_manager._build_per_user_environment(
        hook_token="new-token",
        existing=[{"name": "OPENCLAW_HOOK_TOKEN", "value": "old-token"}],
    )
    token_entry = next(e for e in env if e["name"] == "OPENCLAW_HOOK_TOKEN")
    assert token_entry["value"] == "new-token"
    # Only one entry — overrides should de-dupe.
    assert sum(1 for e in env if e["name"] == "OPENCLAW_HOOK_TOKEN") == 1


# ---------------------------------------------------------------------------
# Provision flow — generates and persists hook_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_generates_hook_token_and_persists_it(manager, mock_ecs_client, mock_efs_client):
    """provision_user_container generates a non-empty hook_token, persists
    it onto the containers row via the upsert inside create_user_service
    (atomic with row creation, so first-time provisioning lands the token),
    and injects the SAME token as OPENCLAW_HOOK_TOKEN on the per-user task
    definition.

    Regression guard: an earlier shape called container_repo.set_hook_token
    BEFORE create_user_service's upsert. Because update_fields short-circuits
    on a missing row, that call was a silent no-op for first-time provisioning
    — the env var landed on the task def but the row never had hook_token
    set. Pinning the assertion to the upsert kwargs ensures any future
    refactor that re-introduces "persist before row exists" ordering fails
    here.
    """
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

        # Persistence: the upsert that creates/updates the containers row
        # must carry hook_token + hook_token_rotated_at as fields, so a
        # brand-new row lands with the token set in the same write that
        # creates it.
        assert mock_repo.upsert.await_count >= 1
        upsert_args = mock_repo.upsert.await_args
        upsert_user_id = upsert_args[0][0]
        upsert_fields = upsert_args[0][1]
        assert upsert_user_id == "user_test_123"
        assert "hook_token" in upsert_fields, (
            "Upsert must persist hook_token alongside row creation; "
            "any path that defers persistence to a post-upsert helper "
            "no-ops for first-time provisioning."
        )
        token = upsert_fields["hook_token"]
        assert isinstance(token, str)
        assert len(token) >= 32
        assert "hook_token_rotated_at" in upsert_fields, (
            "Persist hook_token_rotated_at alongside the token so the rotation timestamp tracks the current value."
        )

        # Injection: the registered task def carries OPENCLAW_HOOK_TOKEN
        # with the SAME token value persisted to DDB. We don't want a drift
        # bug where DDB has one token and the running container another.
        reg_kwargs = mock_ecs_client.register_task_definition.call_args.kwargs
        envs = reg_kwargs["containerDefinitions"][0].get("environment") or []
        hook_entry = next((e for e in envs if e["name"] == "OPENCLAW_HOOK_TOKEN"), None)
        assert hook_entry is not None, "Per-user task def must carry OPENCLAW_HOOK_TOKEN env var"
        assert hook_entry["value"] == token, "Token persisted to DDB must match the env var on the running task"
