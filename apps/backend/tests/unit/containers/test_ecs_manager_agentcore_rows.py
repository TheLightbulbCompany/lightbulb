"""A workspace on AgentCore has no ECS task. The three ECS chokepoints every wake/park door
funnels through must say so, or the first message to a moved workspace starts the old task next
to the microVM — two gateways on one EFS access point, which corrupts the litestream replica set
silently (reproduced 2026-09-16). Rollout scaffolding: deleted with the ECS path."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.containers.ecs_manager import EcsManager
from core.services import runtime_transport

AGENTCORE_ROW = {
    "owner_id": "org_abc",
    "service_name": "openclaw-org_abc-deadbeef",
    "status": "running",
    "runtime_host": "agentcore",
    "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
    "gateway_token": "tok",
}


@pytest.fixture
def manager():
    with patch("core.containers.ecs_manager.settings") as s:
        s.AWS_REGION = "us-east-1"
        s.ENVIRONMENT = "dev"
        s.ECS_CLUSTER_ARN = "arn:aws:ecs:us-east-1:1:cluster/test"
        s.ECS_SUBNETS = "subnet-a"
        s.ECS_SECURITY_GROUP_ID = "sg-1"
        s.CLOUD_MAP_SERVICE_ARN = "arn:aws:servicediscovery:us-east-1:1:service/srv-test"
        s.CLOUD_MAP_NAMESPACE_NAME = "lightbulb-dev.local"
        s.CLOUD_MAP_SERVICE_NAME = "openclaw-containers"
        s.EFS_FILE_SYSTEM_ID = "fs-1"
        with patch("core.containers.ecs_manager.boto3") as b:
            b.client.return_value = MagicMock()
            yield EcsManager()


@pytest.fixture
def agentcore_row(monkeypatch):
    monkeypatch.setattr(
        "core.containers.ecs_manager.container_repo.get_by_owner_id", AsyncMock(return_value=dict(AGENTCORE_ROW))
    )


@pytest.mark.asyncio
async def test_resolve_reports_the_sentinel_address_without_touching_cloud_map(manager, agentcore_row):
    manager.discover_ip = MagicMock(side_effect=AssertionError("must not discover"))
    container, ip = await manager.resolve_running_container("org_abc")
    assert container["runtime_host"] == "agentcore"
    assert ip == runtime_transport.AGENTCORE_ADDRESS
    assert ip  # truthy: every "no ip -> wake / 503" door stays shut


@pytest.mark.asyncio
async def test_resolve_ignores_the_row_status_for_an_agentcore_workspace(manager, monkeypatch):
    # Even a stale ``stopped`` cannot make a moved workspace look like a parked ECS box.
    monkeypatch.setattr(
        "core.containers.ecs_manager.container_repo.get_by_owner_id",
        AsyncMock(return_value={**AGENTCORE_ROW, "status": "stopped"}),
    )
    container, ip = await manager.resolve_running_container("org_abc")
    assert container is not None and ip == runtime_transport.AGENTCORE_ADDRESS


@pytest.mark.asyncio
async def test_start_is_a_no_op(manager, agentcore_row):
    manager._assert_start_allowed = AsyncMock(side_effect=AssertionError("guards must not run"))
    await manager.start_user_service("org_abc")
    manager._ecs.update_service.assert_not_called()


@pytest.mark.asyncio
async def test_stop_is_a_no_op_and_never_writes_stopped(manager, agentcore_row, monkeypatch):
    update_status = AsyncMock(side_effect=AssertionError("must not write status"))
    monkeypatch.setattr("core.containers.ecs_manager.container_repo.update_status", update_status)
    await manager.stop_user_service("org_abc", reason="trial_idle")
    manager._ecs.update_service.assert_not_called()


@pytest.mark.asyncio
async def test_an_ecs_row_still_takes_the_ecs_path(manager, monkeypatch):
    # The default: no runtime_host means today's fleet, which still resolves through Cloud Map.
    monkeypatch.setattr(
        "core.containers.ecs_manager.container_repo.get_by_owner_id",
        AsyncMock(return_value={**AGENTCORE_ROW, "runtime_host": None, "agentcore_runtime_arn": None}),
    )
    manager.discover_ip = MagicMock(return_value="10.0.0.9")
    _, ip = await manager.resolve_running_container("org_abc")
    assert ip == "10.0.0.9"
