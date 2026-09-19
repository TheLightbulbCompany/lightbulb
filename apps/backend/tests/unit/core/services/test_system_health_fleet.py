from unittest.mock import AsyncMock, patch

import pytest

from core.services import system_health


@pytest.mark.asyncio
async def test_fleet_counts_uses_live_ecs():
    with patch(
        "core.services.system_health.fleet_ops.fleet_counts_live",
        AsyncMock(return_value={"running": 2, "provisioning": 0, "stopped": 1, "error": 0, "unknown": 0, "total": 3}),
    ):
        counts = await system_health._fleet_counts()
    assert counts["running"] == 2
    assert counts["total"] == 3
