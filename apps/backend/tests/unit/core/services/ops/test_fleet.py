"""The operator console's fleet grid, built from the container rows alone.

It used to enrich every row with a live ``describe_services`` (batched 10/call) and derive
health from desired/running/pending counts plus the rollout state. There are no per-user
services left to describe: the row the provisioner writes IS the state, so the health
classifier, the describe-gap "unknown" rule and the batching went with the ECS runtime. What
stays worth pinning is the parts that were never about ECS -- the two TTL caches, the bounded
name fan-out, and the one status rule that survives (a row with no runtime ARN is "unknown",
never "stopped").
"""

import time
from unittest.mock import AsyncMock, patch

import pytest

from core.services.ops import fleet

_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1"


@pytest.fixture(autouse=True)
def _clear_caches():
    fleet._name_cache.clear()
    fleet._fleet_cache = None
    yield
    fleet._name_cache.clear()
    fleet._fleet_cache = None


@pytest.mark.asyncio
async def test_grid_reports_each_rows_own_status():
    rows = [
        {"owner_id": "org_a", "agentcore_runtime_arn": _ARN, "status": "running"},
        {"owner_id": "org_b", "agentcore_runtime_arn": _ARN, "status": "error"},
    ]
    with patch("core.services.ops.fleet.container_repo.scan_all", AsyncMock(return_value=rows)):
        grid = await fleet.fleet_grid()

    by_owner = {g["owner_id"]: g for g in grid}
    assert by_owner["org_a"]["status"] == "running"
    assert by_owner["org_b"]["status"] == "error"
    from core.config import settings

    assert by_owner["org_a"]["runtime_arn"] == settings.HERMES_SHARED_RUNTIME_ARN


@pytest.mark.asyncio
async def test_a_row_with_no_runtime_is_unknown_never_stopped():
    """The one classification rule that survives: an unprovisioned row must not borrow
    "stopped" from an absent runtime -- an operator reading "stopped" would conclude the
    workspace was parked, not that it was never created."""
    rows = [{"owner_id": "org_new", "status": "provisioning"}]
    with patch("core.services.ops.fleet.container_repo.scan_all", AsyncMock(return_value=rows)):
        grid = await fleet.fleet_grid()

    assert grid[0]["status"] == "unknown"
    assert grid[0]["runtime_arn"] is None


@pytest.mark.asyncio
async def test_a_row_with_no_owner_id_is_dropped():
    """The containers table also holds reserved-key rows (pass admission, the lifecycle
    lease), which carry no owner_id and are not workspaces."""
    rows = [{"owner_id": "org_a", "agentcore_runtime_arn": _ARN, "status": "running"}, {"last_run_at": 1}]
    with patch("core.services.ops.fleet.container_repo.scan_all", AsyncMock(return_value=rows)):
        grid = await fleet.fleet_grid()

    assert [g["owner_id"] for g in grid] == ["org_a"]


@pytest.mark.asyncio
async def test_fleet_counts_live_shape():
    rows = [{"owner_id": "org_a", "agentcore_runtime_arn": _ARN, "status": "running"}]
    with patch("core.services.ops.fleet.container_repo.scan_all", AsyncMock(return_value=rows)):
        counts = await fleet.fleet_counts_live()

    assert set(counts) == {"running", "provisioning", "stopped", "error", "unknown", "total"}
    assert counts["running"] == 1
    assert counts["total"] == 1


@pytest.mark.asyncio
async def test_fleet_grid_served_from_cache_when_fresh():
    fleet._fleet_cache = (time.monotonic() + 60, [{"owner_id": "org_a", "status": "running"}])
    with patch("core.services.ops.fleet.container_repo.scan_all", AsyncMock()) as scan:
        grid = await fleet.fleet_grid()
    assert grid[0]["owner_id"] == "org_a"
    scan.assert_not_called()


@pytest.mark.asyncio
async def test_fleet_grid_ignores_expired_cache_and_rebuilds():
    fleet._fleet_cache = (time.monotonic() - 1, [{"owner_id": "org_stale", "status": "running"}])
    rows = [{"owner_id": "org_a", "agentcore_runtime_arn": _ARN, "status": "running"}]
    with patch("core.services.ops.fleet.container_repo.scan_all", AsyncMock(return_value=rows)) as scan:
        grid = await fleet.fleet_grid()
    scan.assert_called_once()
    assert grid[0]["owner_id"] == "org_a"  # rebuilt, not the stale org_stale row


@pytest.mark.asyncio
async def test_grid_display_name_resolves_user_email_and_org_name():
    rows = [
        {"owner_id": "user_1", "agentcore_runtime_arn": _ARN, "status": "running"},
        {"owner_id": "org_1", "agentcore_runtime_arn": _ARN, "status": "running"},
    ]
    with (
        patch("core.services.ops.fleet.container_repo.scan_all", AsyncMock(return_value=rows)),
        patch(
            "core.services.ops.fleet.user_repo.get",
            AsyncMock(return_value={"user_id": "user_1", "email": "founder@isol8.co"}),
        ),
        patch(
            "core.services.ops.fleet.clerk_admin.get_organization",
            AsyncMock(return_value={"id": "org_1", "name": "Acme Corp"}),
        ),
    ):
        grid = await fleet.fleet_grid()

    by_owner = {g["owner_id"]: g for g in grid}
    assert by_owner["user_1"]["display_name"] == "founder@isol8.co"
    assert by_owner["org_1"]["display_name"] == "Acme Corp"


@pytest.mark.asyncio
async def test_grid_display_name_falls_back_to_clerk_then_none_on_failure():
    # DDB row without email → Clerk get_user; Clerk failing → display_name=None,
    # never an exception out of fleet_grid.
    rows = [{"owner_id": "user_gone", "agentcore_runtime_arn": _ARN, "status": "running"}]
    with (
        patch("core.services.ops.fleet.container_repo.scan_all", AsyncMock(return_value=rows)),
        patch("core.services.ops.fleet.user_repo.get", AsyncMock(return_value=None)),
        patch("core.services.ops.fleet.clerk_admin.get_user", AsyncMock(side_effect=RuntimeError("clerk down"))),
    ):
        grid = await fleet.fleet_grid()

    assert grid[0]["display_name"] is None
    # The failure is cached so the next refresh does not re-hit Clerk.
    assert fleet._name_cache["user_gone"][1] is None


@pytest.mark.asyncio
async def test_display_names_served_from_cache_without_refetch():
    fleet._name_cache["org_cached"] = (time.monotonic(), "Cached Org")
    with (
        patch("core.services.ops.fleet.clerk_admin.get_organization", AsyncMock()) as get_org,
        patch("core.services.ops.fleet.user_repo.get", AsyncMock()) as get_user_row,
    ):
        names = await fleet._display_names(["org_cached"])
    assert names == {"org_cached": "Cached Org"}
    get_org.assert_not_called()
    get_user_row.assert_not_called()
