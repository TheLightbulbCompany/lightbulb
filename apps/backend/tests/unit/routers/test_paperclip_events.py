"""Inbound Paperclip plugin-event receiver: auth, decision table, fan-out."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.webhook_dedup import WebhookDedupResult
from routers.paperclip_events import decide_push

_SECRET = "test-webhook-secret"
_AUTH = {"authorization": f"Bearer {_SECRET}"}


def _event(event_type: str, payload: dict | None = None, **overrides) -> dict:
    return {
        "eventId": overrides.get("eventId", "evt-1"),
        "eventType": event_type,
        "companyId": overrides.get("companyId", "co-1"),
        "occurredAt": "2026-07-31T00:00:00Z",
        "payload": payload or {},
    }


class TestDecidePush:
    def test_approval_created(self):
        title, body = decide_push(_event("approval.created"))
        assert title == "Awaiting your approval"
        assert "submitted" in body

    def test_approval_resubmitted(self):
        title, body = decide_push(_event("approval.resubmitted"))
        assert title == "Awaiting your approval"
        assert "resubmitted" in body

    def test_run_failed_carries_error(self):
        title, body = decide_push(_event("agent.run.failed", {"error": "boom"}))
        assert title == "Team run failed"
        assert body == "boom"

    def test_run_failed_generic_without_error(self):
        _, body = decide_push(_event("agent.run.failed", {}))
        assert body == "A team agent run failed"

    def test_run_finished_pushes_for_assignment_and_on_demand(self):
        for source in ("assignment", "on_demand"):
            assert decide_push(_event("agent.run.finished", {"invocationSource": source})) is not None

    def test_run_finished_silent_for_timer_and_automation(self):
        for source in ("timer", "automation", None):
            assert decide_push(_event("agent.run.finished", {"invocationSource": source})) is None

    def test_unknown_and_decided_events_silent(self):
        assert decide_push(_event("approval.decided")) is None
        assert decide_push(_event("agent.run.started")) is None
        assert decide_push({"eventType": None}) is None


@pytest.fixture()
def secret_set(monkeypatch):
    monkeypatch.setattr("routers.paperclip_events.settings.PAPERCLIP_EVENTS_WEBHOOK_SECRET", _SECRET)


@pytest.mark.asyncio
async def test_disabled_without_secret(async_client, monkeypatch):
    monkeypatch.setattr("routers.paperclip_events.settings.PAPERCLIP_EVENTS_WEBHOOK_SECRET", "")
    resp = await async_client.post("/api/v1/webhooks/paperclip-events", json=_event("approval.created"), headers=_AUTH)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rejects_bad_token(async_client, secret_set):
    resp = await async_client.post(
        "/api/v1/webhooks/paperclip-events",
        json=_event("approval.created"),
        headers={"authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_rejects_missing_ids(async_client, secret_set):
    resp = await async_client.post(
        "/api/v1/webhooks/paperclip-events",
        json={"eventType": "approval.created"},
        headers=_AUTH,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_approval_created_pushes_to_all_company_members(async_client, secret_set):
    sent = AsyncMock()
    with (
        patch(
            "routers.paperclip_events.PaperclipRepo.get_user_ids_for_company",
            new=AsyncMock(return_value=["user_A", "user_B"]),
        ),
        patch("routers.paperclip_events.send_push_to_user", sent),
        patch(
            "routers.paperclip_events.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.paperclip_events.put_metric"),
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/paperclip-events", json=_event("approval.created"), headers=_AUTH
        )
    assert resp.status_code == 200
    assert resp.json()["pushed"] is True
    assert sent.await_count == 2
    audiences = {call.args[0] for call in sent.await_args_list}
    assert audiences == {"user_A", "user_B"}
    assert sent.await_args_list[0].args[1] == "Awaiting your approval"


@pytest.mark.asyncio
async def test_duplicate_event_skipped(async_client, secret_set):
    sent = AsyncMock()
    with (
        patch(
            "routers.paperclip_events.PaperclipRepo.get_user_ids_for_company",
            new=AsyncMock(return_value=["user_A"]),
        ),
        patch("routers.paperclip_events.send_push_to_user", sent),
        patch(
            "routers.paperclip_events.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.ALREADY_SEEN),
        ),
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/paperclip-events", json=_event("approval.created"), headers=_AUTH
        )
    assert resp.status_code == 200
    assert resp.json()["pushed"] is False
    sent.assert_not_called()


@pytest.mark.asyncio
async def test_non_push_event_acks_without_dedup_write(async_client, secret_set):
    dedup = AsyncMock()
    with patch("routers.paperclip_events.record_event_or_skip", dedup):
        resp = await async_client.post(
            "/api/v1/webhooks/paperclip-events",
            json=_event("agent.run.finished", {"invocationSource": "timer"}),
            headers=_AUTH,
        )
    assert resp.status_code == 200
    assert resp.json()["pushed"] is False
    dedup.assert_not_called()


@pytest.mark.asyncio
async def test_one_member_push_failure_does_not_block_rest(async_client, secret_set):
    sent = AsyncMock(side_effect=[RuntimeError("expo down"), None])
    with (
        patch(
            "routers.paperclip_events.PaperclipRepo.get_user_ids_for_company",
            new=AsyncMock(return_value=["user_A", "user_B"]),
        ),
        patch("routers.paperclip_events.send_push_to_user", sent),
        patch(
            "routers.paperclip_events.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.paperclip_events.put_metric"),
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/paperclip-events",
            json=_event("agent.run.failed", {"error": "boom"}),
            headers=_AUTH,
        )
    assert resp.status_code == 200
    assert resp.json()["pushed"] is True
    assert sent.await_count == 2


@pytest.mark.asyncio
async def test_no_audience_emits_metric(async_client, secret_set):
    metric = MagicMock()
    with (
        patch(
            "routers.paperclip_events.PaperclipRepo.get_user_ids_for_company",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "routers.paperclip_events.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.paperclip_events.put_metric", metric),
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/paperclip-events", json=_event("approval.created"), headers=_AUTH
        )
    assert resp.status_code == 200
    assert resp.json()["pushed"] is False
    metric.assert_called_once_with("paperclip_events.no_audience")
