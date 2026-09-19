import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.ops import ecs_ops


@pytest.fixture(autouse=True)
def _clear_caches():
    ecs_ops._name_cache.clear()
    ecs_ops._fleet_cache = None
    yield
    ecs_ops._name_cache.clear()
    ecs_ops._fleet_cache = None


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
    ):
        counts = await ecs_ops.fleet_counts_live()

    assert set(counts) == {"running", "provisioning", "stopped", "error", "unknown", "total"}
    assert counts["running"] == 1
    assert counts["total"] == 1


@pytest.mark.asyncio
async def test_fleet_grid_served_from_cache_when_fresh():
    ecs_ops._fleet_cache = (
        time.monotonic() + 60,  # not yet expired
        [{"owner_id": "org_a", "service_name": "openclaw-a-1", "status": "running"}],
    )
    with patch("core.services.ops.ecs_ops.container_repo.scan_all", AsyncMock()) as scan:
        grid = await ecs_ops.fleet_grid()
    assert grid[0]["owner_id"] == "org_a"
    scan.assert_not_called()  # fresh cache hit → no live ECS enumeration


@pytest.mark.asyncio
async def test_fleet_grid_ignores_expired_cache_and_rebuilds():
    ecs_ops._fleet_cache = (
        time.monotonic() - 1,  # already expired
        [{"owner_id": "org_stale", "service_name": "openclaw-stale-1", "status": "running"}],
    )
    fake_ecs = MagicMock()
    fake_ecs.describe_services.return_value = {"services": [_svc("openclaw-a-1", 1, 1, 0, "COMPLETED")]}
    rows = [{"owner_id": "org_a", "service_name": "openclaw-a-1", "status": "running"}]
    with (
        patch.object(ecs_ops, "_ecs", return_value=fake_ecs),
        patch("core.services.ops.ecs_ops.container_repo.scan_all", AsyncMock(return_value=rows)) as scan,
    ):
        grid = await ecs_ops.fleet_grid()
    scan.assert_called_once()  # expired cache ignored → live rebuild
    assert grid[0]["owner_id"] == "org_a"  # rebuilt, not the stale org_stale row


@pytest.mark.asyncio
async def test_grid_display_name_resolves_user_email_and_org_name():
    rows = [
        {"owner_id": "user_1", "service_name": "openclaw-u1-1", "status": "running"},
        {"owner_id": "org_1", "service_name": "openclaw-o1-1", "status": "running"},
    ]
    fake_ecs = MagicMock()
    fake_ecs.describe_services.return_value = {
        "services": [
            _svc("openclaw-u1-1", 1, 1, 0, "COMPLETED"),
            _svc("openclaw-o1-1", 1, 1, 0, "COMPLETED"),
        ]
    }
    with (
        patch.object(ecs_ops, "_ecs", return_value=fake_ecs),
        patch("core.services.ops.ecs_ops.container_repo.scan_all", AsyncMock(return_value=rows)),
        patch(
            "core.services.ops.ecs_ops.user_repo.get",
            AsyncMock(return_value={"user_id": "user_1", "email": "founder@isol8.co"}),
        ),
        patch(
            "core.services.ops.ecs_ops.clerk_admin.get_organization",
            AsyncMock(return_value={"id": "org_1", "name": "Acme Corp"}),
        ),
    ):
        grid = await ecs_ops.fleet_grid()

    by_owner = {g["owner_id"]: g for g in grid}
    assert by_owner["user_1"]["display_name"] == "founder@isol8.co"
    assert by_owner["org_1"]["display_name"] == "Acme Corp"


@pytest.mark.asyncio
async def test_grid_display_name_falls_back_to_clerk_then_none_on_failure():
    # DDB row without email → Clerk get_user; Clerk failing → display_name=None,
    # never an exception out of fleet_grid.
    rows = [{"owner_id": "user_gone", "service_name": "openclaw-ug-1", "status": "running"}]
    fake_ecs = MagicMock()
    fake_ecs.describe_services.return_value = {"services": [_svc("openclaw-ug-1", 1, 1, 0, "COMPLETED")]}
    with (
        patch.object(ecs_ops, "_ecs", return_value=fake_ecs),
        patch("core.services.ops.ecs_ops.container_repo.scan_all", AsyncMock(return_value=rows)),
        patch("core.services.ops.ecs_ops.user_repo.get", AsyncMock(return_value=None)),
        patch(
            "core.services.ops.ecs_ops.clerk_admin.get_user",
            AsyncMock(side_effect=RuntimeError("clerk down")),
        ),
    ):
        grid = await ecs_ops.fleet_grid()

    assert grid[0]["display_name"] is None
    # The failure is cached so the next refresh does not re-hit Clerk.
    assert ecs_ops._name_cache["user_gone"][1] is None


@pytest.mark.asyncio
async def test_display_names_served_from_cache_without_refetch():
    ecs_ops._name_cache["org_cached"] = (time.monotonic(), "Cached Org")
    with (
        patch("core.services.ops.ecs_ops.clerk_admin.get_organization", AsyncMock()) as get_org,
        patch("core.services.ops.ecs_ops.user_repo.get", AsyncMock()) as get_user_row,
    ):
        names = await ecs_ops._display_names(["org_cached"])
    assert names == {"org_cached": "Cached Org"}
    get_org.assert_not_called()
    get_user_row.assert_not_called()


@pytest.mark.asyncio
async def test_indexed_service_not_described_classifies_unknown():
    # A DDB-indexed service absent from describe_services' services{} is a
    # describe gap (deleted / in failures[]), not real state — it must classify
    # "unknown", never "stopped" (which _classify would infer from desired=0).
    rows = [{"owner_id": "org_gap", "service_name": "openclaw-gap-1", "status": "active"}]
    fake_ecs = MagicMock()
    fake_ecs.describe_services.return_value = {"services": []}  # not returned
    with (
        patch.object(ecs_ops, "_ecs", return_value=fake_ecs),
        patch("core.services.ops.ecs_ops.container_repo.scan_all", AsyncMock(return_value=rows)),
    ):
        grid = await ecs_ops.fleet_grid()
    assert grid[0]["status"] == "unknown"
