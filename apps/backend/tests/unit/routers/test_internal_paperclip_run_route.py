"""Hook-token gate + wiring for the Paperclip `http`-adapter run-trigger door
(mirrors test_messaging_run_events.py)."""

from unittest.mock import AsyncMock, patch

import httpx

from core.services import runtime_transport
from core.services.openclaw_work import _AGENTCORE_INGRESS_TIMEOUT_S

OWNER, TOKEN, AGENT, RUN = "org_1", "hook-token-for-a", "agent_ceo", "run_123"
GATEWAY_TOKEN = "gateway-token-for-a"
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
        AsyncMock(
            return_value={
                "owner_id": OWNER,
                "hook_token": TOKEN,
                "gateway_token": GATEWAY_TOKEN,
                "runtime_host": "agentcore",
            }
        ),
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
    # The caller's own hook token (TOKEN) authenticates INTO this route; the outbound call to
    # the bridge's inbound door carries the container's gateway token instead (final-review C2).
    assert captured["headers"] == {"Authorization": f"Bearer {GATEWAY_TOKEN}"}
    # AgentCore holds the invoke through a cold microVM start -- same ~90s budget as the
    # canonical ingress caller (openclaw_work._post), not a short ECS-era timeout.
    assert captured["timeout_s"] == _AGENTCORE_INGRESS_TIMEOUT_S == 90.0


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


def test_the_real_paperclip_context_record_is_accepted_not_rejected(client):
    """Regression for the live 422. Paperclip's `http` adapter sends
    ``context = parseObject(run.contextSnapshot)`` -- its OWN free-form run record, not the
    three keys our docs named. Typed with ``ContractModel``'s ``extra="forbid"``, the first
    real run on a migrated workspace was rejected three times and the agent never woke, while
    room chat (which rides the WORK bus, not the adapter) looked fine. Extras on a payload
    Paperclip owns must be ignored; only the ids we route on stay strict."""
    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["json_body"] = json_body
        return runtime_transport.RuntimeResponse(202, "{}", {})

    body = {
        "runId": RUN,
        "agentId": AGENT,
        "context": {
            "taskId": "task_9",
            "wakeReason": "New comment on your task",
            "projectId": "proj_1",
            "executionWorkspaceId": "ws_1",
            "paperclipWorkspace": {"id": "ws_1", "path": "/work"},
            "paperclipEnvironment": {"kind": "local"},
            "paperclipSessionHandoffMarkdown": "# handoff",
        },
    }
    with _container(), patch("routers.messaging.runtime_transport.request", fake_request):
        r = client.post(f"/api/v1/internal/paperclip/runs/{OWNER}", json=body, headers=H)

    assert r.status_code == 202
    assert captured["json_body"]["message"] == {"text": "New comment on your task"}


def test_a_non_string_wake_reason_does_not_500(client):
    """``context`` is Paperclip's record, so its VALUES are not ours to assume either."""
    captured = {}

    async def fake_request(
        container, path, *, ip=None, method="POST", json_body=None, content=None, headers=None, timeout_s=30.0
    ):
        captured["json_body"] = json_body
        return runtime_transport.RuntimeResponse(202, "{}", {})

    body = {"runId": RUN, "agentId": AGENT, "context": {"wakeReason": {"kind": "comment"}, "taskId": 7}}
    with _container(), patch("routers.messaging.runtime_transport.request", fake_request):
        r = client.post(f"/api/v1/internal/paperclip/runs/{OWNER}", json=body, headers=H)

    assert r.status_code == 202
    assert isinstance(captured["json_body"]["message"]["text"], str)


def test_malformed_body_missing_run_id_or_agent_id_is_422(client):
    """The body is now a typed ContractModel (PaperclipRunRequest), not a hand-parsed dict --
    a malformed request must 422 (FastAPI's native validation response), never reach the
    transport call, and never 500."""
    with _container(), patch("routers.messaging.runtime_transport.request") as req:
        r = client.post(f"/api/v1/internal/paperclip/runs/{OWNER}", json={"companyId": "c"}, headers=H)
    assert r.status_code == 422
    req.assert_not_called()


def test_cold_start_timeout_maps_to_a_retryable_503(client):
    """AgentCore can hold the invoke through a full cold microVM start; if it still times out
    (or the transport otherwise fails), the caller sees a retryable RUNTIME_UNAVAILABLE, the
    same shape abort_run/steer_run already use for a runtime that can't be reached -- never a
    bare 500."""

    async def fake_request(*a, **k):
        raise httpx.ReadTimeout("timed out waiting for the runtime")

    with _container(), patch("routers.messaging.runtime_transport.request", fake_request):
        r = client.post(f"/api/v1/internal/paperclip/runs/{OWNER}", json=BODY, headers=H)

    assert r.status_code == 503
    payload = r.json()
    assert payload["code"] == "RUNTIME_UNAVAILABLE"
    assert payload["retryable"] is True
