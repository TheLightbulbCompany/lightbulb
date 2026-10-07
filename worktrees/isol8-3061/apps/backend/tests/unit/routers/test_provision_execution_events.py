"""POST /internal/provisioning/execution-events: the EventBridge API destination for the
provision state machine's status changes. A refetch nudge to the owner's clients."""

import json
from unittest.mock import AsyncMock, patch

import pytest

URL = "/api/v1/internal/provisioning/execution-events"


def _event(input_=None, status="SUCCEEDED"):
    return {
        "detail-type": "Step Functions Execution Status Change",
        "source": "aws.states",
        "detail": {"status": status, "name": "provision-org_a-1234abcd", "input": input_},
    }


@pytest.fixture
def publish():
    with patch("core.services.status_events.publish_status_changed_now", new_callable=AsyncMock) as p:
        yield p


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "s3cret")


@pytest.mark.asyncio
async def test_publishes_the_nudge_for_the_executions_owner(async_client, publish):
    r = await async_client.post(
        URL, json=_event(json.dumps({"owner_id": "org_a"})), headers={"x-lightbulb-ws-internal": "s3cret"}
    )
    assert r.status_code == 204
    publish.assert_awaited_once_with("org_a")


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"x-lightbulb-ws-internal": "wrong"}])
async def test_refuses_without_the_shared_secret(async_client, publish, headers):
    r = await async_client.post(URL, json=_event(json.dumps({"owner_id": "org_a"})), headers=headers)
    assert r.status_code == 403
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_refuses_when_the_secret_is_unconfigured(async_client, publish, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "")
    r = await async_client.post(
        URL, json=_event(json.dumps({"owner_id": "org_a"})), headers={"x-lightbulb-ws-internal": ""}
    )
    assert r.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("input_", [None, "not json", json.dumps({"owner_id": ""}), json.dumps(["x"])])
async def test_an_unreadable_input_is_accepted_and_ignored(async_client, publish, input_):
    r = await async_client.post(URL, json=_event(input_), headers={"x-lightbulb-ws-internal": "s3cret"})
    assert r.status_code == 204
    publish.assert_not_awaited()
