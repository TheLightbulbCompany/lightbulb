import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
from core.auth import AuthContext, get_current_user  # noqa: E402
from main import app  # noqa: E402
from routers import calls as mod  # noqa: E402

svc = mod.voice_calls


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_a", org_id="org_a", org_role="org:member"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _start_patches(*, mode="free", budget=600, key="sk-test"):
    return [
        patch.object(svc.settings, "OPENAI_API_KEY", key),
        patch.object(
            svc.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"subscription_status": "active", "plan": "base"}),
        ),
        patch.object(svc.voice_billing, "billing_mode", return_value=mode),
        patch.object(svc.voice_billing, "budget_seconds", AsyncMock(return_value=budget)),
        patch.object(svc, "_company_id", AsyncMock(return_value="co-1")),
        patch.object(svc, "_verify_agent", AsyncMock()),
        patch.object(svc, "_frame_channel", AsyncMock(return_value="/ws/ws-1/users/key-1")),
        patch.object(svc.openai_live, "create_session", AsyncMock(return_value=("live_1", "answer\r\n"))),
        patch.object(svc, "put_call", AsyncMock()),
        patch.object(svc, "_prewake", MagicMock()),
    ]


def _with(patches):
    for p in patches:
        p.start()
    return patches


def _stop(patches):
    for p in patches:
        p.stop()


def test_start_returns_answer_and_deadline(client):
    ps = _with(_start_patches())
    try:
        resp = client.post("/api/v1/calls", json={"agentId": "agent-1", "agentName": "Finance", "sdp": "offer\r\n"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["sdp"] == "answer\r\n" and body["callId"]
        call = svc.put_call.await_args.args[0]
        assert call["deadline"] - call["startedAt"] == 600
        assert call["billing"] == "free" and call["status"] == "call_open"
    finally:
        _stop(ps)


def test_start_without_key_is_503(client):
    ps = _with(_start_patches(key=""))
    try:
        assert (
            client.post("/api/v1/calls", json={"agentId": "agent-1", "agentName": "Finance", "sdp": "x"}).json()[
                "detail"
            ]["code"]
            == "voice_unavailable"
        )
    finally:
        _stop(ps)


def test_start_under_a_minute_is_402(client):
    ps = _with(_start_patches(budget=59))
    try:
        resp = client.post("/api/v1/calls", json={"agentId": "agent-1", "agentName": "Finance", "sdp": "x"})
        assert resp.status_code == 402
        assert resp.json()["detail"] == {"code": "voice_limit", "reason": "free_minutes_used"}
    finally:
        _stop(ps)


def test_start_inactive_plan_is_402(client):
    ps = _with(_start_patches(mode=None))
    try:
        assert (
            client.post("/api/v1/calls", json={"agentId": "agent-1", "agentName": "Finance", "sdp": "x"}).json()[
                "detail"
            ]["reason"]
            == "plan_inactive"
        )
    finally:
        _stop(ps)


def test_start_credits_holds_and_releases_on_upstream_failure(client):
    _with(_start_patches(mode="credits", budget=300))
    hold = patch.object(svc, "_hold", AsyncMock(return_value=30_000)).start()
    release = patch.object(svc, "release_hold", AsyncMock()).start()
    svc.openai_live.create_session.side_effect = svc.openai_live.LiveUpstreamError("400")
    try:
        assert (
            client.post("/api/v1/calls", json={"agentId": "agent-1", "agentName": "Finance", "sdp": "x"}).status_code
            == 502
        )
        hold.assert_awaited_once_with("org_a", 300)
        release.assert_awaited_once_with("org_a", 30_000)
    finally:
        patch.stopall()


OPEN_CALL = {
    "callId": "call-1",
    "ownerId": "org_a",
    "userId": "user_a",
    "agentId": "agent-1",
    "companyId": "co-1",
    "status": "call_open",
    "turnRunId": None,
    "deadline": 4102444800,
}


def test_turn_starts_a_call_scoped_run(client):
    runtime = AsyncMock(return_value=MagicMock(status_code=202))
    with (
        patch.object(svc, "get_call", AsyncMock(return_value=dict(OPEN_CALL))),
        patch.object(svc, "set_turn", AsyncMock()),
        patch.object(svc, "_frame_channel", AsyncMock(return_value="/ws/ws-1/users/key-1")),
        patch.object(
            svc.container_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"gateway_token": "gw", "agentcore_runtime_arn": "arn"}),
        ),
        patch.object(svc.runtime_transport, "request", runtime),
    ):
        resp = client.post("/api/v1/calls/call-1/turns", json={"utterance": "check payouts", "transcript": "User: hi"})
    assert resp.status_code == 200
    body = runtime.await_args.kwargs["json_body"]
    assert runtime.await_args.args[1] == "/internal/calls/v1/runs"
    assert body["identity"]["sessionId"] == "agent:agent-1:lightbulb:call:call-1"
    assert body["identity"]["callId"] == "call-1"
    assert body["request"]["session_id"] == body["identity"]["sessionId"]
    assert body["request"]["frames"]["channel"] == "/ws/ws-1/users/key-1"
    assert "check payouts" in body["request"]["input"]


def test_turn_supersedes_previous_run(client):
    runtime = AsyncMock(return_value=MagicMock(status_code=202))
    with (
        patch.object(svc, "get_call", AsyncMock(return_value={**OPEN_CALL, "turnRunId": "old-run"})),
        patch.object(svc, "set_turn", AsyncMock()),
        patch.object(svc, "_frame_channel", AsyncMock(return_value="/ws/ws-1/users/key-1")),
        patch.object(
            svc.container_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"gateway_token": "gw", "agentcore_runtime_arn": "arn"}),
        ),
        patch.object(svc.runtime_transport, "request", runtime),
    ):
        client.post("/api/v1/calls/call-1/turns", json={"utterance": "actually stop", "transcript": ""})
    first = runtime.await_args_list[0]
    assert first.args[1] == "/internal/calls/v1/runs/old-run/stop"


def test_turn_rejects_other_user(client):
    with patch.object(svc, "get_call", AsyncMock(return_value={**OPEN_CALL, "userId": "user_b"})):
        assert client.post("/api/v1/calls/call-1/turns", json={"utterance": "x", "transcript": ""}).status_code == 404


def test_end_closes(client):
    with (
        patch.object(svc, "get_call", AsyncMock(return_value=dict(OPEN_CALL))),
        patch.object(mod.voice_calls, "close_call", AsyncMock(return_value=True)) as close,
    ):
        assert client.post("/api/v1/calls/call-1/end").json() == {"ended": True}
    close.assert_awaited_once_with("call-1")
