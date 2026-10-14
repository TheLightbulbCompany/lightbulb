"""Inbound Paperclip plugin-event receiver: auth, decision table, fan-out."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.webhook_dedup import WebhookDedupResult
from core.services import activity
from routers import paperclip_events
from routers.paperclip_events import approval_push_copy, decide_push, push_data

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
        assert body == "Couldn't finish: boom"

    def test_run_failed_says_a_known_code_in_plain_words(self):
        _, body = decide_push(_event("agent.run.failed", {"errorCode": "process_lost", "error": "Process lost"}))
        assert body == "Couldn't finish: the workspace restarted mid-run"

    def test_run_failed_generic_without_error(self):
        _, body = decide_push(_event("agent.run.failed", {}))
        assert body == "Couldn't finish: the run hit an error"

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
        assert data["approvalId"] == "ent-1"  # the phone opens Activity focused on it
        assert "agentId" not in data

    def test_omits_missing_fields(self):
        data = push_data({"eventType": "approval.created", "payload": {}})
        assert data == {"source": "teams", "eventType": "approval.created"}

    def test_approval_id_only_for_approval_events(self):
        assert "approvalId" not in push_data(_event("agent.run.failed"))


def _approval(type_: str, payload: dict, agent: str | None = "pa-1") -> dict:
    return {
        "id": "ap-1",
        "companyId": "co-1",
        "type": type_,
        "status": "pending",
        "requestedByAgentId": agent,
        "payload": payload,
        "createdAt": "2026-09-25T01:47:00Z",
    }


def _copy(approval: dict) -> tuple[str, str]:
    viewer = MagicMock(paperclip_user_id=None, user_id=None)
    return approval_push_copy(activity.render_approval(approval, agent_names={"pa-1": "Growth"}, ctx=viewer))


class TestApprovalPushCopy:
    def test_names_the_agent_and_the_title(self):
        title, body = _copy(
            _approval(
                "request_board_approval", {"title": "Pilot X auto-replies", "summary": "Reply to mentions for a week"}
            )
        )
        assert title == "Growth · needs your OK"
        assert body == "Needs your approval: Pilot X auto-replies"

    def test_a_title_that_already_names_the_ask_is_kept(self):
        title, body = _copy(_approval("hire_agent", {"name": "Designer"}))
        assert (title, body) == ("Growth · needs your OK", "Wants to hire Designer")

    def test_untitled_board_approval(self):
        _, body = _copy(_approval("request_board_approval", {}))
        assert body == "Needs your approval"

    def test_budget_override_gains_the_agent(self):
        _, body = _copy(_approval("budget_override_required", {"scopeName": "Ads"}))
        assert body == "Needs your approval: Budget override needed for Ads"


class TestRunCopy:
    @pytest.mark.asyncio
    async def test_names_the_agent_the_task_and_why(self, monkeypatch):
        admin = MagicMock()
        admin.list_agents = AsyncMock(return_value={"agents": [{"id": "pa-1", "name": "Pulse"}]})
        admin.get_issue = AsyncMock(
            return_value={"companyId": "co-1", "identifier": "ISOA-400", "title": "Draft the launch posts"}
        )
        monkeypatch.setattr(paperclip_events, "PaperclipAdminClient", lambda **_: admin)
        monkeypatch.setattr(paperclip_events, "get_admin_session_cookie", AsyncMock(return_value="c"))
        payload = {"agentId": "pa-1", "issueId": "iss-1", "errorCode": "timeout"}
        failed = await paperclip_events._run_copy(_event("agent.run.failed", payload), "co-1")
        assert failed == ("Pulse · ISOA-400", "Couldn't finish: it ran out of time")
        done = await paperclip_events._run_copy(_event("agent.run.finished", payload), "co-1")
        assert done == ("Pulse · ISOA-400", "Finished: Draft the launch posts")
        # Another company's issue is never named.
        admin.get_issue.return_value = {"companyId": "co-2", "identifier": "X-1"}
        assert (await paperclip_events._run_copy(_event("agent.run.failed", payload), "co-1"))[0] == "Pulse"


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
        # Paperclip unreachable: the generic line still goes out.
        patch("routers.paperclip_events._approval_copy", new=AsyncMock(return_value=None)),
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
        patch("routers.paperclip_events._run_copy", new=AsyncMock(return_value=None)),
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


@pytest.mark.asyncio
async def test_run_failed_metric_counts_even_when_push_is_suppressed(async_client, secret_set):
    """The alarm metric is emitted before the cooldown/dedupe that silences the push."""
    metric = MagicMock()
    with (
        patch(
            "routers.paperclip_events.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.ALREADY_SEEN),
        ),
        patch("routers.paperclip_events.put_metric", metric),
    ):
        known = await async_client.post(
            "/api/v1/webhooks/paperclip-events",
            json=_event("agent.run.failed", {"errorCode": "setup_failed", "agentId": "pa-1"}),
            headers=_AUTH,
        )
        unknown = await async_client.post(
            "/api/v1/webhooks/paperclip-events",
            json=_event("agent.run.failed", {"errorCode": "brand_new_code"}, eventId="evt-2"),
            headers=_AUTH,
        )
        missing = await async_client.post(
            "/api/v1/webhooks/paperclip-events",
            json=_event("agent.run.failed", {}, eventId="evt-3"),
            headers=_AUTH,
        )
    for resp in (known, unknown, missing):
        assert resp.json()["pushed"] is False
    assert [c.kwargs["dimensions"]["error_code"] for c in metric.call_args_list] == [
        "setup_failed",
        "other",
        "other",
    ]
    assert all(c.args == ("paperclip.run.failed",) for c in metric.call_args_list)


@pytest.mark.asyncio
async def test_run_failed_hands_the_event_to_escalation(async_client, secret_set):
    """The router only schedules; paperclip_escalation.schedule filters codes and issueId."""
    event = _event("agent.run.failed", {"errorCode": "setup_failed", "agentId": "pa-1", "issueId": "iss-1"})
    with (
        patch("routers.paperclip_events.paperclip_escalation.schedule") as schedule,
        patch(
            "routers.paperclip_events.record_event_or_skip",
            new=AsyncMock(return_value=WebhookDedupResult.ALREADY_SEEN),
        ),
        patch("routers.paperclip_events.put_metric"),
    ):
        resp = await async_client.post("/api/v1/webhooks/paperclip-events", json=event, headers=_AUTH)
    assert resp.status_code == 200
    schedule.assert_called_once_with(event)


@pytest.mark.asyncio
async def test_approval_push_names_agent_and_title(async_client, secret_set):
    sent = AsyncMock()
    admin = MagicMock()
    admin.get_approval = AsyncMock(
        return_value=_approval("request_board_approval", {"title": "Pilot X auto-replies"}) | {"id": "ent-1"}
    )
    admin.list_agents = AsyncMock(return_value=[{"id": "pa-1", "name": "Growth"}])
    with (
        patch(
            "routers.paperclip_events.PaperclipRepo.get_user_ids_for_company", new=AsyncMock(return_value=["user_A"])
        ),
        patch("routers.paperclip_events.send_push_to_user", sent),
        patch("routers.paperclip_events.record_event_or_skip", new=AsyncMock(return_value=WebhookDedupResult.RECORDED)),
        patch("routers.paperclip_events.put_metric"),
        patch("routers.paperclip_events.get_admin_session_cookie", new=AsyncMock(return_value="c=1")),
        patch("routers.paperclip_events.PaperclipAdminClient", return_value=admin),
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/paperclip-events",
            json=_event("approval.created", {"type": "request_board_approval"}),
            headers=_AUTH,
        )
    assert resp.status_code == 200
    _, title, body, data = sent.await_args.args
    assert (title, body) == ("Growth · needs your OK", "Needs your approval: Pilot X auto-replies")
    assert data["approvalId"] == "ent-1"


@pytest.mark.asyncio
async def test_approval_from_another_company_falls_back(async_client, secret_set):
    sent = AsyncMock()
    admin = MagicMock()
    admin.get_approval = AsyncMock(
        return_value=_approval("request_board_approval", {"title": "x"}) | {"companyId": "co-2"}
    )
    admin.list_agents = AsyncMock(return_value=[])
    with (
        patch(
            "routers.paperclip_events.PaperclipRepo.get_user_ids_for_company", new=AsyncMock(return_value=["user_A"])
        ),
        patch("routers.paperclip_events.send_push_to_user", sent),
        patch("routers.paperclip_events.record_event_or_skip", new=AsyncMock(return_value=WebhookDedupResult.RECORDED)),
        patch("routers.paperclip_events.put_metric"),
        patch("routers.paperclip_events.get_admin_session_cookie", new=AsyncMock(return_value="c=1")),
        patch("routers.paperclip_events.PaperclipAdminClient", return_value=admin),
    ):
        await async_client.post(
            "/api/v1/webhooks/paperclip-events",
            json=_event("approval.created", {"type": "request_board_approval"}),
            headers=_AUTH,
        )
    assert sent.await_args.args[1] == "Awaiting your approval"


@pytest.mark.asyncio
async def test_interaction_push_names_agent_and_task_and_focuses_activity(async_client, secret_set):
    sent = AsyncMock()
    admin = MagicMock()
    admin.get_issue = AsyncMock(
        return_value={"id": "ent-1", "companyId": "co-1", "identifier": "LB-7", "title": "Launch email"}
    )
    admin.list_issue_interactions = AsyncMock(
        return_value=[
            {
                "id": "int-1",
                "companyId": "co-1",
                "issueId": "ent-1",
                "kind": "request_confirmation",
                "status": "pending",
                "createdByAgentId": "pa-1",
                "payload": {"version": 1, "prompt": "Send it to 2,000 people?"},
                "createdAt": "2026-09-25T01:47:00Z",
            }
        ]
    )
    admin.list_agents = AsyncMock(return_value=[{"id": "pa-1", "name": "Growth"}])
    with (
        patch(
            "routers.paperclip_events.PaperclipRepo.get_user_ids_for_company", new=AsyncMock(return_value=["user_A"])
        ),
        patch("routers.paperclip_events.send_push_to_user", sent),
        patch("routers.paperclip_events.record_event_or_skip", new=AsyncMock(return_value=WebhookDedupResult.RECORDED)),
        patch("routers.paperclip_events.put_metric"),
        patch("routers.paperclip_events.get_admin_session_cookie", new=AsyncMock(return_value="c=1")),
        patch("routers.paperclip_events.PaperclipAdminClient", return_value=admin),
    ):
        resp = await async_client.post(
            "/api/v1/webhooks/paperclip-events",
            json=_event(
                "issue.thread_interaction_created",
                {"interactionId": "int-1", "interactionKind": "request_confirmation"},
            ),
            headers=_AUTH,
        )
    assert resp.status_code == 200
    _, title, body, data = sent.await_args.args
    assert title == "Growth · LB-7"
    assert body == "Needs your confirmation: Launch email"
    assert data["interactionId"] == "int-1" and data["issueId"] == "ent-1"
