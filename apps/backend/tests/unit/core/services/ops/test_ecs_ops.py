from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.ops import ecs_ops


def _svc(name, desired, running, pending, rollout):
    return {
        "serviceName": name,
        "desiredCount": desired,
        "runningCount": running,
        "pendingCount": pending,
        "deployments": [{"status": "PRIMARY", "rolloutState": rollout}],
    }


@pytest.mark.asyncio
async def test_fleet_grid_maps_health():
    rows = [
        {"owner_id": "org_a", "service_name": "openclaw-a-1", "status": "running"},
        {"owner_id": "org_b", "service_name": "openclaw-b-2", "status": "error"},
    ]
    fake_ecs = MagicMock()
    fake_ecs.describe_services.return_value = {
        "services": [
            _svc("openclaw-a-1", 1, 1, 0, "COMPLETED"),
            _svc("openclaw-b-2", 1, 0, 0, "FAILED"),
        ]
    }

    with (
        patch.object(ecs_ops, "_ecs", return_value=fake_ecs),
        patch("core.services.ops.ecs_ops.container_repo.scan_all", AsyncMock(return_value=rows)),
        patch("core.services.ops.ecs_ops.ops_cache_repo.get", AsyncMock(return_value=None)),
        patch("core.services.ops.ecs_ops.ops_cache_repo.put", AsyncMock()),
    ):
        grid = await ecs_ops.fleet_grid()

    by_owner = {g["owner_id"]: g for g in grid}
    assert by_owner["org_a"]["status"] == "running"
    assert by_owner["org_b"]["status"] == "error"


@pytest.mark.asyncio
async def test_fleet_counts_live_shape():
    rows = [
        {"owner_id": "org_a", "service_name": "openclaw-a-1", "status": "running"},
    ]
    fake_ecs = MagicMock()
    fake_ecs.describe_services.return_value = {"services": [_svc("openclaw-a-1", 1, 1, 0, "COMPLETED")]}
    with (
        patch.object(ecs_ops, "_ecs", return_value=fake_ecs),
        patch("core.services.ops.ecs_ops.container_repo.scan_all", AsyncMock(return_value=rows)),
        patch("core.services.ops.ecs_ops.ops_cache_repo.get", AsyncMock(return_value=None)),
        patch("core.services.ops.ecs_ops.ops_cache_repo.put", AsyncMock()),
    ):
        counts = await ecs_ops.fleet_counts_live()

    assert set(counts) == {"running", "provisioning", "stopped", "error", "unknown", "total"}
    assert counts["running"] == 1
    assert counts["total"] == 1


@pytest.mark.asyncio
async def test_fleet_grid_served_from_cache_when_fresh():
    cached = {"value": [{"owner_id": "org_a", "service_name": "openclaw-a-1", "status": "running"}]}
    with (
        patch("core.services.ops.ecs_ops.ops_cache_repo.get", AsyncMock(return_value=cached)),
        patch("core.services.ops.ecs_ops.container_repo.scan_all", AsyncMock()) as scan,
    ):
        grid = await ecs_ops.fleet_grid()
    assert grid[0]["owner_id"] == "org_a"
    scan.assert_not_called()  # cache hit → no live ECS enumeration
