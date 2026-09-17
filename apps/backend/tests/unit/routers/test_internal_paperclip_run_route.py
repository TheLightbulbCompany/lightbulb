"""Hook-token gate + wiring for the Paperclip `http`-adapter run-trigger door
(mirrors test_messaging_run_events.py)."""

from unittest.mock import AsyncMock, patch

from core.services import runtime_transport

OWNER, TOKEN, AGENT, RUN = "org_1", "hook-token-for-a", "agent_ceo", "run_123"
H = {"Authorization": f"Bearer {TOKEN}"}
BODY = {
    "runId": RUN,
    "agentId": AGENT,
    "companyId": "company_1",
    "context": {"taskId": "task_9", "wakeReason": "New comment on your task"},
}


def _container():
    return patch(
        "routers.messaging.container_repo.get_by_owner_id",
        AsyncMock(return_value={"owner_id": OWNER, "hook_token": TOKEN, "runtime_host": "agentcore"}),
    )


def test_missing_bearer_is_401(client):
    assert client.post(f"/api/v1/internal/paperclip/runs/{OWNER}", json=BODY).status_code == 401


def test_wrong_token_is_401(client):
    with _container():
        r = client.post(
            f"/api/v1/internal/paperclip/runs/{OWNER}", json=BODY, headers={"Authorization": "Bearer WRONG"}
        )
    assert r.status_code == 401


def test_valid_token_forwards_a_paperclip_run_event_to_the_bridge(client):
    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["container"] = container
        captured["path"] = path
        captured["json_body"] = json_body
        captured["headers"] = headers
        captured["timeout_s"] = timeout_s
        return runtime_transport.RuntimeResponse(
            202,
            '{"eventId": "run_123", "runId": "run_123", "status": "accepted"}',
            {"content-type": "application/json"},
        )

    with _container(), patch("routers.messaging.runtime_transport.request", fake_request):
        r = client.post(f"/api/v1/internal/paperclip/runs/{OWNER}", json=BODY, headers=H)

    assert r.status_code == 202
    assert r.json() == {"eventId": "run_123", "runId": "run_123", "status": "accepted"}
    assert captured["path"] == f"/plugins/lightbulb-channel/inbound/{AGENT}"
    assert captured["json_body"] == {
        "version": 1,
        "eventId": RUN,
        "eventKind": "paperclip_run",
        "targetAgentId": AGENT,
        "sessionKey": f"agent:{AGENT}:paperclip:{RUN}",
        "message": {"text": "New comment on your task"},
    }
    assert captured["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert captured["timeout_s"] == 10.0


def test_falls_back_to_a_generic_text_when_no_wake_reason_or_task_is_given(client):
    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["json_body"] = json_body
        return runtime_transport.RuntimeResponse(202, "{}", {})

    body = {"runId": RUN, "agentId": AGENT, "companyId": "company_1", "context": {}}
    with _container(), patch("routers.messaging.runtime_transport.request", fake_request):
        r = client.post(f"/api/v1/internal/paperclip/runs/{OWNER}", json=body, headers=H)

    assert r.status_code == 202
    assert captured["json_body"]["message"] == {"text": "Paperclip triggered a run."}


def test_missing_run_id_or_agent_id_is_400(client):
    with _container():
        r = client.post(f"/api/v1/internal/paperclip/runs/{OWNER}", json={"companyId": "c"}, headers=H)
    assert r.status_code == 400
