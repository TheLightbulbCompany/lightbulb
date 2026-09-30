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
        patch.object(svc, "_claim_free", AsyncMock()),
        patch.object(svc, "_release_free", AsyncMock()),
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


def test_start_with_cdk_placeholder_key_is_503(client):
    ps = _with(_start_patches(key="Xq9fK2mZp4Lw8RtV1nB7cD3hJ6yA0sGe"))  # random 32 chars, not sk-
    try:
        resp = client.post("/api/v1/calls", json={"agentId": "agent-1", "agentName": "Finance", "sdp": "x"})
        assert resp.status_code == 503 and resp.json()["detail"]["code"] == "voice_unavailable"
        svc.openai_live.create_session.assert_not_awaited()
    finally:
        _stop(ps)


def _cond_fail():
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


def test_second_free_start_while_one_open_is_409(client):
    ps = _with(_start_patches())
    _stop([p for p in ps if getattr(p, "attribute", "") == "_claim_free"])
    table = MagicMock()
    table.update_item.side_effect = _cond_fail()
    with patch.object(svc, "_table", return_value=table):
        resp = client.post("/api/v1/calls", json={"agentId": "agent-1", "agentName": "Finance", "sdp": "x"})
    try:
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "voice_busy"
        svc.openai_live.create_session.assert_not_awaited()
    finally:
        _stop([p for p in ps if getattr(p, "attribute", "") != "_claim_free"])


def test_free_start_failure_releases_claim(client):
    ps = _with(_start_patches())
    svc.openai_live.create_session.side_effect = svc.openai_live.LiveUpstreamError("400")
    try:
        assert client.post("/api/v1/calls", json={"agentId": "a", "agentName": "F", "sdp": "x"}).status_code == 502
        claimed = svc._claim_free.await_args.args
        svc._release_free.assert_awaited_once_with("org_a", claimed[1])
    finally:
        _stop(ps)


def test_free_put_call_failure_releases_claim(client):
    ps = _with(_start_patches())
    patch.object(svc.openai_live, "close_session", AsyncMock()).start()
    svc.put_call.side_effect = RuntimeError("ddb down")
    try:
        with pytest.raises(RuntimeError):
            client.post("/api/v1/calls", json={"agentId": "a", "agentName": "F", "sdp": "x"})
        svc._release_free.assert_awaited_once_with("org_a", svc._claim_free.await_args.args[1])
    finally:
        patch.stopall()
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
        patch.object(svc, "_stop_turn", AsyncMock()) as stop,
    ):
        resp = client.post("/api/v1/calls/call-1/turns", json={"utterance": "actually stop", "transcript": ""})
    assert resp.status_code == 200
    assert stop.call_args.args[0]["turnRunId"] == "old-run"  # scheduled in the background, not awaited
    assert runtime.await_args.args[1] == "/internal/calls/v1/runs"


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


def test_start_transport_error_releases_hold(client):
    _with(_start_patches(mode="credits", budget=300))
    patch.object(svc, "_hold", AsyncMock(return_value=30_000)).start()
    release = patch.object(svc, "release_hold", AsyncMock()).start()
    svc.openai_live.create_session.side_effect = svc.httpx.ReadTimeout("slow")
    try:
        assert client.post("/api/v1/calls", json={"agentId": "a", "agentName": "F", "sdp": "x"}).status_code == 502
        release.assert_awaited_once_with("org_a", 30_000)
    finally:
        patch.stopall()


def test_start_put_call_failure_releases_hold_and_closes_session(client):
    _with(_start_patches(mode="credits", budget=300))
    patch.object(svc, "_hold", AsyncMock(return_value=30_000)).start()
    release = patch.object(svc, "release_hold", AsyncMock()).start()
    close = patch.object(svc.openai_live, "close_session", AsyncMock()).start()
    svc.put_call.side_effect = RuntimeError("ddb down")
    try:
        with pytest.raises(RuntimeError):
            client.post("/api/v1/calls", json={"agentId": "a", "agentName": "F", "sdp": "x"})
        release.assert_awaited_once_with("org_a", 30_000)
        close.assert_awaited_once_with("live_1")
    finally:
        patch.stopall()


def test_turn_after_hangup_stops_new_run_and_404s(client):
    runtime = AsyncMock(return_value=MagicMock(status_code=202))
    with (
        patch.object(svc, "get_call", AsyncMock(return_value=dict(OPEN_CALL))),
        patch.object(svc, "set_turn", AsyncMock(side_effect=_cond_fail())),
        patch.object(svc, "_frame_channel", AsyncMock(return_value="/ws/ws-1/users/key-1")),
        patch.object(svc.container_repo, "get_by_owner_id", AsyncMock(return_value={"gateway_token": "gw"})),
        patch.object(svc.runtime_transport, "request", runtime),
        patch.object(svc, "_stop_turn", AsyncMock()) as stop,
    ):
        resp = client.post("/api/v1/calls/call-1/turns", json={"utterance": "x", "transcript": ""})
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "call_not_found"
    new_run = runtime.await_args.kwargs["json_body"]["identity"]["runId"]
    assert stop.call_args.args[0]["turnRunId"] == new_run


def test_turn_other_set_turn_error_reraises(client):
    from botocore.exceptions import ClientError

    boom = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
    with (
        patch.object(svc, "get_call", AsyncMock(return_value=dict(OPEN_CALL))),
        patch.object(svc, "set_turn", AsyncMock(side_effect=boom)),
        patch.object(svc, "_frame_channel", AsyncMock(return_value="/ws/ws-1/users/key-1")),
        patch.object(svc.container_repo, "get_by_owner_id", AsyncMock(return_value={"gateway_token": "gw"})),
        patch.object(svc.runtime_transport, "request", AsyncMock(return_value=MagicMock(status_code=202))),
        patch.object(svc, "_stop_turn", AsyncMock()) as stop,
        pytest.raises(ClientError),
    ):
        client.post("/api/v1/calls/call-1/turns", json={"utterance": "x", "transcript": ""})
    # only the superseded-run stop (none here: turnRunId is None) -- no stop for the new run
    stop.assert_called_once()
    assert stop.call_args.args[0]["turnRunId"] is None
