"""Inbound Paperclip plugin-event receiver: auth, decision table, fan-out."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.webhook_dedup import WebhookDedupResult
from routers.paperclip_events import decide_push, push_data

_SECRET = "test-webhook-secret"
_AUTH = {"authorization": f"Bearer {_SECRET}"}


def _event(event_type: str, payload: dict | None = None, **overrides) -> dict:
    return {
        "eventId": overrides.get("eventId", "evt-1"),
        "eventType": event_type,
        "companyId": overrides.get("companyId", "co-1"),
        "entityId": overrides.get("entityId", "ent-1"),
        "occurredAt": "2026-07-31T00:00:00Z",
        "payload": payload if payload is not None else {},
    }


class TestDecidePush:
    def test_thread_interaction_names_what_is_being_asked(self):
        """The one event that means work is STOPPED, not finished."""
        bodies = set()
        for kind in (
            "request_confirmation",
            "request_checkbox_confirmation",
            "ask_user_questions",
            "suggest_tasks",
        ):
            title, body = decide_push(_event("issue.thread_interaction_created", {"interactionKind": kind}))
            assert title == "A task needs you"
            bodies.add(body)
        # Distinct copy per kind -- a shared string would defeat the point.
        assert len(bodies) == 4

    def test_thread_interaction_unknown_kind_still_pushes(self):
        _, body = decide_push(_event("issue.thread_interaction_created", {"interactionKind": "future_kind"}))
        assert body == "A task is paused waiting for your input"

    def test_thread_interaction_addressed_to_an_agent_is_silent(self):
        assert (
            decide_push(
                _event(
                    "issue.thread_interaction_created",
                    {"interactionKind": "request_confirmation", "addresseeAgentId": "agent-9"},
                )
            )
            is None
        )

    def test_approval_body_names_the_approval_type(self):
        _, body = decide_push(_event("approval.created", {"type": "hire_agent"}))
        assert body == "An agent wants to hire a new agent"
        # Unknown type falls back to the create/resubmit wording, never to None.
        _, body = decide_push(_event("approval.resubmitted", {"type": "future_type"}))
        assert "resubmitted" in body

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

    def test_non_dict_payload_does_not_raise(self):
        # A truthy non-dict payload must degrade, not AttributeError → 500.
        assert decide_push({"eventType": "agent.run.finished", "payload": "boom"}) is None
        title, _ = decide_push({"eventType": "agent.run.failed", "payload": ["x"]})
        assert title == "Team run failed"


class TestPushData:
    def test_carries_teams_identifiers_but_never_agent_id(self):
        # data.agentId would deep-link the mobile app to a nonexistent gateway
        # chat (Paperclip agent ids are not gateway agent ids) — must be absent.
        data = push_data(_event("approval.created", {"agentId": "pa-1", "runId": "run-9", "issueId": "iss-3"}))
        assert data["source"] == "teams"
        assert data["eventType"] == "approval.created"
        assert data["entityId"] == "ent-1"
        assert data["companyId"] == "co-1"
        assert data["runId"] == "run-9"
        assert data["issueId"] == "iss-3"
        assert "agentId" not in data

    def test_omits_missing_fields(self):
        data = push_data({"eventType": "approval.created", "payload": {}})
        assert data == {"source": "teams", "eventType": "approval.created"}


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
async def test_non_ascii_auth_header_is_401_not_500(async_client, secret_set):
    # Starlette decodes header bytes latin-1; a str compare_digest would
    # TypeError on non-ASCII. Public endpoint — must 401, never 500. Sent as
    # raw latin-1 bytes because httpx refuses to ascii-encode a str header.
    resp = await async_client.post(
        "/api/v1/webhooks/paperclip-events",
        json=_event("approval.created"),
        headers={b"authorization": "Bearer café".encode("latin-1")},
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
    first = sent.await_args_list[0].args
    assert first[1] == "Awaiting your approval"
    assert first[3]["entityId"] == "ent-1"


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
async def test_audience_failure_rolls_back_dedup_row(async_client, secret_set):
    # The eventId dedup exists for plugin-restart redelivery; a post-record
    # failure must delete the row so the redelivery can retry (metronome
    # webhook pattern), not ack a lost notification.
    deleted = AsyncMock()
    with (
        patch(
            "routers.paperclip_events.PaperclipRepo.get_user_ids_for_company",
            new=AsyncMock(side_effect=RuntimeError("ddb throttle")),
        ),
        patch(
            "routers.paperclip_events.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch("routers.paperclip_events.delete_event", deleted),
    ):
        # The test ASGI transport re-raises unhandled app exceptions instead
        # of rendering a 500 — the raise itself is the "route 500s" signal.
        with pytest.raises(RuntimeError, match="ddb throttle"):
            await async_client.post("/api/v1/webhooks/paperclip-events", json=_event("approval.created"), headers=_AUTH)
    deleted.assert_awaited_once_with("pcplugin_evt-1")


@pytest.mark.asyncio
async def test_run_failure_cooldown_suppresses_repeats(async_client, secret_set):
    # First failure records the cooldown key; the second (fresh eventId, same
    # company+agent+bucket) hits ALREADY_SEEN on the cooldown and stays silent.
    sent = AsyncMock()
    dedup = AsyncMock(
        side_effect=[
            WebhookDedupResult.RECORDED,  # cooldown key, event 1
            WebhookDedupResult.RECORDED,  # eventId key, event 1
            WebhookDedupResult.ALREADY_SEEN,  # cooldown key, event 2
        ]
    )
    with (
        patch(
            "routers.paperclip_events.PaperclipRepo.get_user_ids_for_company",
            new=AsyncMock(return_value=["user_A"]),
        ),
        patch("routers.paperclip_events.send_push_to_user", sent),
        patch("routers.paperclip_events.record_event_or_skip", dedup),
        patch("routers.paperclip_events.put_metric"),
    ):
        first = await async_client.post(
            "/api/v1/webhooks/paperclip-events",
            json=_event("agent.run.failed", {"error": "boom", "agentId": "pa-1"}, eventId="evt-f1"),
            headers=_AUTH,
        )
        second = await async_client.post(
            "/api/v1/webhooks/paperclip-events",
            json=_event("agent.run.failed", {"error": "boom", "agentId": "pa-1"}, eventId="evt-f2"),
            headers=_AUTH,
        )
    assert first.json()["pushed"] is True
    assert second.json()["pushed"] is False
    assert sent.await_count == 1
    assert dedup.await_count == 3
    cooldown_key = dedup.await_args_list[0].args[0]
    assert cooldown_key.startswith("pcplugin_failcool_co-1_pa-1_")


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
