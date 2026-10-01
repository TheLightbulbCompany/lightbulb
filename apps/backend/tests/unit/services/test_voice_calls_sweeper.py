from unittest.mock import AsyncMock, patch

import pytest

from core.services import voice_calls as vc


@pytest.mark.asyncio
async def test_sweep_closes_only_past_deadline_and_stale_closing():
    rows = {
        "call_open": [{"callId": "late", "deadline": 100}, {"callId": "live", "deadline": 10_000}],
        "call_closing": [
            {"callId": "stuck", "deadline": 100, "closingAt": 1},
            {"callId": "busy", "deadline": 100, "closingAt": 900},
        ],
    }
    with (
        patch.object(vc.container_repo, "get_by_status", AsyncMock(side_effect=lambda s: rows[s])),
        patch.object(vc, "close_call", AsyncMock(return_value=True)) as close,
    ):
        assert await vc.sweep_calls_once(now=1000) == 2
    assert sorted(c.args[0] for c in close.await_args_list) == ["late", "stuck"]
