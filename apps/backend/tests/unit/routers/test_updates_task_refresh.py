from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers import updates


@pytest.mark.asyncio
async def test_admin_fleet_task_refresh_schedules_durable_rollout():
    request = updates.AdminUpdateRequest(
        owner_id="all",
        update_type=updates.TASK_DEFINITION_REFRESH_KIND,
        description="Config-lock protocol rollout",
        changes={},
    )

    with patch.object(
        updates,
        "queue_fleet_task_definition_refresh",
        new=AsyncMock(return_value=(12, 3)),
    ) as queue:
        result = await updates.admin_create_update(request, MagicMock())

    assert result == {"status": "scheduled", "queued": 12, "skipped": 3}
    queue.assert_awaited_once_with("Config-lock protocol rollout")
