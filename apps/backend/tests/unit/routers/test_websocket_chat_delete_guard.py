# apps/backend/tests/unit/routers/test_websocket_chat_delete_guard.py
from unittest.mock import AsyncMock, patch

import pytest

from routers import websocket_chat as wc


@pytest.mark.asyncio
async def test_seeded_delete_is_rejected_before_forward():
    sent = {}

    def send_message(connection_id, payload):
        sent["payload"] = payload

    with (
        patch.object(wc, "is_seeded_agent", new=AsyncMock(return_value=True)),
        patch.object(wc, "management_api") as mgmt,
    ):
        mgmt.send_message.side_effect = send_message
        consumed = await wc._guard_seeded_delete(
            owner_id="o1",
            method="agents.delete",
            params={"agentId": "friday-uuid"},
            connection_id="conn1",
            req_id="r1",
        )
    assert consumed is True
    assert sent["payload"]["ok"] is False
    assert "can't be deleted" in sent["payload"]["error"]["message"].lower()


@pytest.mark.asyncio
async def test_non_seeded_delete_is_not_consumed():
    with patch.object(wc, "is_seeded_agent", new=AsyncMock(return_value=False)):
        consumed = await wc._guard_seeded_delete(
            owner_id="o1",
            method="agents.delete",
            params={"agentId": "bob-uuid"},
            connection_id="conn1",
            req_id="r1",
        )
    assert consumed is False


@pytest.mark.asyncio
async def test_non_delete_method_is_not_consumed():
    consumed = await wc._guard_seeded_delete(
        owner_id="o1",
        method="agents.update",
        params={"agentId": "x"},
        connection_id="c",
        req_id="r",
    )
    assert consumed is False
