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
    hold = patch.object(svc, "_hold", AsyncMock(return_value=(30_000, 300))).start()
    release = patch.object(svc, "release_hold", AsyncMock()).start()
    svc.openai_live.create_session.side_effect = svc.openai_live.LiveUpstreamError("400")
    try:
        assert (
            client.post("/api/v1/calls", json={"agentId": "agent-1", "agentName": "Finance", "sdp": "x"}).status_code
            == 502
        )
        hold.assert_awaited_once_with("org_a")
        release.assert_awaited_once_with("org_a", 30_000)
    finally:
        patch.stopall()


def test_start_unexpected_error_releases_hold_and_500s(client):
    _with(_start_patches(mode="credits", budget=300))
    patch.object(svc, "_hold", AsyncMock(return_value=(30_000, 300))).start()
    release = patch.object(svc, "release_hold", AsyncMock()).start()
    svc.openai_live.create_session.side_effect = RuntimeError("boom")
    try:
        resp = TestClient(app, raise_server_exceptions=False).post(
            "/api/v1/calls", json={"agentId": "a", "agentName": "F", "sdp": "x"}
        )
        assert resp.status_code == 500
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


SEQ = [1]


def _turn_env(*, get_calls, claim_side=None, runtime=None):
    """Patches for start_turn; get_calls are the successive get_call results (first = _own_open_call)."""
    order = []
    table = MagicMock()

    def upd(**kw):
        order.append("claim" if "ReturnValues" in kw else "release")
        if claim_side is not None and "ReturnValues" in kw:
            eff = claim_side.pop(0)
            if isinstance(eff, Exception):
                raise eff
            return {"Attributes": eff}
        return {}

    table.update_item.side_effect = upd
    rt = runtime or AsyncMock(return_value=MagicMock(status_code=202))

    async def rt_wrapped(*a, **k):
        order.append("dispatch")
        return await rt(*a, **k)

    stop = AsyncMock()
    patches = [
        patch.object(svc, "get_call", AsyncMock(side_effect=get_calls)),
        patch.object(svc, "_table", return_value=table),
        patch.object(svc, "_frame_channel", AsyncMock(return_value="/ws/ws-1/users/key-1")),
        patch.object(
            svc.container_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"gateway_token": "gw", "agentcore_runtime_arn": "arn"}),
        ),
        patch.object(svc.runtime_transport, "request", rt_wrapped),
        patch.object(svc, "_stop_turn", stop),
    ]
    return patches, order, table, rt, stop


def _run_turn(client, patches):
    for p in patches:
        p.start()
    try:
        return client.post(
            "/api/v1/calls/call-1/turns", json={"seq": SEQ[0], "utterance": "check payouts", "transcript": "User: hi"}
        )
    finally:
        patch.stopall()


def _turn_id_from(table):
    return table.update_item.call_args_list[0].kwargs["ExpressionAttributeValues"][":new"]


def test_turn_claims_before_dispatch_and_scopes_the_run(client):
    c = dict(OPEN_CALL)
    patches, order, table, rt, stop = _turn_env(get_calls=[c, None], claim_side=[{}])
    # verify step re-reads the row: it must show our run id
    patches[0] = patch.object(
        svc,
        "get_call",
        AsyncMock(side_effect=lambda _id: {**c, "turnRunId": _turn_id_from(table)} if order else c),
    )
    resp = _run_turn(client, patches)
    assert resp.status_code == 200
    assert order == ["claim", "dispatch"]
    body = rt.await_args.kwargs["json_body"]
    assert body["identity"]["sessionId"] == "agent:agent-1:lightbulb:call:call-1"
    assert body["request"]["frames"]["channel"] == "/ws/ws-1/users/key-1"
    assert "check payouts" in body["request"]["input"]
    assert resp.json()["turnId"] == body["identity"]["runId"] == _turn_id_from(table)
    cond = table.update_item.call_args.kwargs["ConditionExpression"]
    assert "turnSeq < :seq" in cond and table.update_item.call_args.kwargs["ExpressionAttributeValues"][":seq"] == 1


def test_turn_stops_previous_run_in_background(client):
    c = {**OPEN_CALL, "turnRunId": "old-run"}
    patches, order, table, rt, stop = _turn_env(get_calls=[c], claim_side=[{"turnRunId": "old-run"}])
    patches[0] = patch.object(
        svc, "get_call", AsyncMock(side_effect=lambda _id: {**c, "turnRunId": _turn_id_from(table)} if order else c)
    )
    resp = _run_turn(client, patches)
    assert resp.status_code == 200
    assert stop.call_args.args[0]["turnRunId"] == "old-run"
    assert table.update_item.call_args.kwargs["ExpressionAttributeValues"][":seq"] == SEQ[0]
    assert table.update_item.call_args.kwargs["ReturnValues"] == "ALL_OLD"


def test_turn_lower_or_equal_seq_is_409_and_nothing_dispatched(client):
    c = {**OPEN_CALL, "turnRunId": "newer", "turnSeq": 5}
    patches, order, table, rt, stop = _turn_env(get_calls=[c, c], claim_side=[_cond_fail()])
    resp = _run_turn(client, patches)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "turn_conflict"
    assert order == ["claim"]
    stop.assert_not_called()


def test_turn_claim_lost_with_call_closed_is_404_and_nothing_dispatched(client):
    patches, order, table, rt, stop = _turn_env(
        get_calls=[dict(OPEN_CALL), {**OPEN_CALL, "status": "call_closing"}], claim_side=[_cond_fail()]
    )
    resp = _run_turn(client, patches)
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "call_not_found"
    assert "dispatch" not in order


def test_turn_superseded_while_dispatching_stops_own_run_and_409s(client):
    c = dict(OPEN_CALL)
    patches, order, table, rt, stop = _turn_env(get_calls=[c, {**c, "turnRunId": "newer-run"}], claim_side=[{}])
    resp = _run_turn(client, patches)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "turn_conflict"
    assert stop.call_args.args[0]["turnRunId"] == rt.await_args.kwargs["json_body"]["identity"]["runId"]


def test_turn_after_hangup_stops_new_run_and_404s(client):
    c = dict(OPEN_CALL)
    patches, order, table, rt, stop = _turn_env(get_calls=[c, {**c, "status": "call_closing"}], claim_side=[{}])
    resp = _run_turn(client, patches)
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "call_not_found"
    assert stop.call_args.args[0]["turnRunId"] == rt.await_args.kwargs["json_body"]["identity"]["runId"]


def test_turn_non_conditional_claim_error_is_503_and_nothing_dispatched(client):
    from botocore.exceptions import ClientError

    boom = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
    patches, order, table, rt, stop = _turn_env(get_calls=[dict(OPEN_CALL)], claim_side=[boom])
    resp = _run_turn(client, patches)
    assert resp.status_code == 503 and resp.json()["detail"]["code"] == "runtime_unavailable"
    assert "dispatch" not in order


def test_turn_dispatch_failure_keeps_claim_and_stops_run(client):
    rt = AsyncMock(return_value=MagicMock(status_code=500))
    patches, order, table, _, stop = _turn_env(get_calls=[dict(OPEN_CALL)], claim_side=[{}], runtime=rt)
    resp = _run_turn(client, patches)
    assert resp.status_code == 503
    assert order == ["claim", "dispatch"]  # nothing clears turnRunId
    assert stop.call_args.args[0]["turnRunId"] == rt.await_args.kwargs["json_body"]["identity"]["runId"]


def test_turn_rejects_other_user(client):
    with patch.object(svc, "get_call", AsyncMock(return_value={**OPEN_CALL, "userId": "user_b"})):
        assert (
            client.post(
                "/api/v1/calls/call-1/turns", json={"seq": SEQ[0], "utterance": "x", "transcript": ""}
            ).status_code
            == 404
        )


def test_end_closes(client):
    with (
        patch.object(svc, "get_call", AsyncMock(return_value=dict(OPEN_CALL))),
        patch.object(mod.voice_calls, "close_call", AsyncMock(return_value=True)) as close,
    ):
        assert client.post("/api/v1/calls/call-1/end").json() == {"ended": True}
    close.assert_awaited_once_with("call-1")


def test_start_transport_error_releases_hold(client):
    _with(_start_patches(mode="credits", budget=300))
    patch.object(svc, "_hold", AsyncMock(return_value=(30_000, 300))).start()
    release = patch.object(svc, "release_hold", AsyncMock()).start()
    svc.openai_live.create_session.side_effect = svc.httpx.ReadTimeout("slow")
    try:
        assert client.post("/api/v1/calls", json={"agentId": "a", "agentName": "F", "sdp": "x"}).status_code == 502
        release.assert_awaited_once_with("org_a", 30_000)
    finally:
        patch.stopall()


def test_start_put_call_failure_releases_hold_and_closes_session(client):
    _with(_start_patches(mode="credits", budget=300))
    patch.object(svc, "_hold", AsyncMock(return_value=(30_000, 300))).start()
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


def test_start_put_call_failure_with_release_failure_still_closes_session(client):
    _with(_start_patches(mode="credits", budget=300))
    patch.object(svc, "_hold", AsyncMock(return_value=(30_000, 300))).start()
    patch.object(svc, "release_hold", AsyncMock(side_effect=RuntimeError("throttle"))).start()
    close = patch.object(svc.openai_live, "close_session", AsyncMock()).start()
    svc.put_call.side_effect = ValueError("ddb down")
    try:
        with pytest.raises(ValueError, match="ddb down"):
            client.post("/api/v1/calls", json={"agentId": "a", "agentName": "F", "sdp": "x"})
        close.assert_awaited_once_with("live_1")
    finally:
        patch.stopall()


@pytest.mark.asyncio
async def test_start_cancelled_releases_hold_and_reraises():
    import asyncio

    _with(_start_patches(mode="credits", budget=300))
    patch.object(svc, "_hold", AsyncMock(return_value=(30_000, 300))).start()
    release = patch.object(svc, "release_hold", AsyncMock()).start()
    svc.openai_live.create_session.side_effect = asyncio.CancelledError()
    try:
        with pytest.raises(asyncio.CancelledError):
            await svc.start_call(owner_id="org_a", user_id="user_a", agent_id="a", agent_name="F", sdp="x")
        release.assert_awaited_once_with("org_a", 30_000)
    finally:
        patch.stopall()


@pytest.mark.asyncio
async def test_free_claim_expires_after_stale_window():
    table = MagicMock()
    with patch.object(svc, "_table", return_value=table):
        await svc._claim_free("org_a", "call-2", 100_000)
    kw = table.update_item.call_args.kwargs
    assert "openCallAt < :stale" in kw["ConditionExpression"]
    assert kw["ExpressionAttributeValues"][":stale"] == 100_000 - (svc.voice_billing.MAX_CALL_SECONDS + 600)
    assert kw["ExpressionAttributeValues"][":now"] == 100_000
    # a fresh claim makes DynamoDB fail the condition: still 409
    table.update_item.side_effect = _cond_fail()
    with patch.object(svc, "_table", return_value=table), pytest.raises(svc.HTTPException) as e:
        await svc._claim_free("org_a", "call-3", 100_001)
    assert e.value.status_code == 409


def test_start_credits_deadline_matches_held_budget(client):
    _with(_start_patches(mode="credits", budget=1800))
    patch.object(svc, "_hold", AsyncMock(return_value=(30_000, 240))).start()
    patch.object(svc.time, "time", return_value=1000).start()
    try:
        resp = client.post("/api/v1/calls", json={"agentId": "a", "agentName": "F", "sdp": "x"})
        assert resp.status_code == 200 and resp.json()["deadline"] == 1240
    finally:
        patch.stopall()


def test_start_credits_low_when_hold_finds_no_headroom(client):
    _with(_start_patches(mode="credits"))
    patch.object(svc, "_hold", AsyncMock(return_value=(0, 0))).start()
    try:
        resp = client.post("/api/v1/calls", json={"agentId": "a", "agentName": "F", "sdp": "x"})
        assert resp.status_code == 402 and resp.json()["detail"]["reason"] == "credits_low"
    finally:
        patch.stopall()


def test_start_put_call_cancelled_undoes_start(client):
    import asyncio

    _with(_start_patches(mode="credits", budget=300))
    patch.object(svc, "_hold", AsyncMock(return_value=(30_000, 300))).start()
    release = patch.object(svc, "release_hold", AsyncMock()).start()
    close = patch.object(svc.openai_live, "close_session", AsyncMock()).start()
    svc.put_call.side_effect = asyncio.CancelledError()
    try:
        with pytest.raises(BaseException):
            client.post("/api/v1/calls", json={"agentId": "a", "agentName": "F", "sdp": "x"})
        release.assert_awaited_once_with("org_a", 30_000)
        close.assert_awaited_once_with("live_1")
    finally:
        patch.stopall()
