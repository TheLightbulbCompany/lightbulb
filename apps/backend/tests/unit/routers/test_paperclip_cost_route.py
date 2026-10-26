"""POST /internal/paperclip/cost/{owner}: the settlement Lambda's door, shared-secret gated."""

from unittest.mock import AsyncMock, patch

import pytest

URL = "/api/v1/internal/paperclip/cost/org_a"
BODY = {
    "agentId": "ag1",
    "generationId": "gen-1",
    "costMicrocents": 600,
    "model": "m",
    "occurredAt": "2026-10-05T12:00:00+00:00",
}


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "s3cret")


@pytest.fixture
def record():
    with patch("core.services.paperclip_cost.record_cost", new_callable=AsyncMock, return_value=0) as p:
        yield p


@pytest.mark.parametrize("headers", [{}, {"x-lightbulb-ws-internal": "wrong"}])
async def test_refuses_without_the_shared_secret(async_client, record, headers):
    r = await async_client.post(URL, json=BODY, headers=headers)
    assert r.status_code == 401
    record.assert_not_awaited()


async def test_records_cost_for_the_owner(async_client, record):
    r = await async_client.post(URL, json=BODY, headers={"x-lightbulb-ws-internal": "s3cret"})
    assert r.status_code == 204
    record.assert_awaited_once_with(
        owner_id="org_a", agent_id="ag1", cost_microcents=600, model="m", occurred_at=BODY["occurredAt"]
    )
