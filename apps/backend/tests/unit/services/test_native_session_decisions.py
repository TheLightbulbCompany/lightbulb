"""The owner's own Mac answers a join or pick-up request, or flips sharing (session sharing stays
on the backend). Restored from the deleted native_session_tools minus the agent-ask branch."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.services import native_session_decisions as tools
from core.services.machine_auth import MachineIdentity
from core.services.messaging import ApiProblem
from routers import messaging as router


def test_the_route_takes_only_a_paired_mac():
    app = FastAPI()
    app.include_router(router.router, prefix="/api/v1")
    body = {"sessionId": str(uuid.uuid4()), "generation": "g1", "decision": "pause"}
    with TestClient(app) as client:
        assert client.post("/api/v1/messaging/native-session-decisions", json=body).status_code == 403


def _decision_device():
    return MachineIdentity(
        user_id="user_a",
        org_id="org_a",
        owner_id="org_a",
        kind="node",
        auth_kind="device",
        client_id="openclaw-macos",
        device_id="mac_a",
    )


def _decision_row(**over):
    return {
        "ownerId": "org_a",
        "sessionId": "11111111-1111-4111-8111-111111111111",
        "humanUserId": "user_a",
        "deviceId": "mac_a",
        "generation": "g1",
        "connectionId": "connection_a",
        **over,
    }


def _connected(monkeypatch, device_id="mac_a"):
    from core.services import local_session_relay

    relay = SimpleNamespace(connect=AsyncMock(return_value=({"device_id": device_id, "user_id": "user_a"}, None)))
    monkeypatch.setattr(local_session_relay, "get_local_session_relay", lambda: relay)


_SID = uuid.UUID("11111111-1111-4111-8111-111111111111")


@pytest.mark.parametrize(
    "row",
    [
        None,
        _decision_row(humanUserId="user_b"),
        _decision_row(deviceId="mac_b"),
        _decision_row(generation="g0"),
    ],
)
async def test_decision_refuses_anything_but_this_macs_current_session(monkeypatch, row):
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=row))
    _connected(monkeypatch)
    body = tools.NativeDecisionRequest(session_id=_SID, generation="g1", decision="pause")
    with pytest.raises(ApiProblem):
        await tools.decide(_decision_device(), body)


async def test_an_ended_run_takes_its_owners_sharing_switch_and_nothing_else(monkeypatch):
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=_decision_row(endedAt=1)))
    _connected(monkeypatch, device_id="mac_other")  # the run's own connection is gone
    set_sharing = AsyncMock(return_value="paused")
    monkeypatch.setattr(tools.local_sessions, "set_sharing", set_sharing)
    from core.services import local_session_relay

    monkeypatch.setattr(local_session_relay, "publish_resources_changed", AsyncMock())
    pause = tools.NativeDecisionRequest(session_id=_SID, generation="g1", decision="pause")
    assert await tools.decide(_decision_device(), pause) == "paused"
    assert set_sharing.await_args.args[1:] == (str(_SID), False)
    # Shared again before its conversation is resumed: the resume's register keeps what the row says.
    unpause = tools.NativeDecisionRequest(session_id=_SID, generation="g1", decision="unpause")
    await tools.decide(_decision_device(), unpause)
    assert set_sharing.await_args.args[1:] == (str(_SID), True)
    allow = tools.NativeDecisionRequest(
        session_id=_SID, generation="g1", decision="allow", user_id="user_z", request_id="r1"
    )
    with pytest.raises(ApiProblem):
        await tools.decide(_decision_device(), allow)


async def test_decision_refuses_a_stale_connection(monkeypatch):
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=_decision_row()))
    _connected(monkeypatch, device_id="mac_other")
    body = tools.NativeDecisionRequest(session_id=_SID, generation="g1", decision="pause")
    with pytest.raises(ApiProblem):
        await tools.decide(_decision_device(), body)


async def test_allow_and_pause_run_as_the_owner(monkeypatch):
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=_decision_row()))
    _connected(monkeypatch)
    decide_join = AsyncMock(return_value="joined")
    set_sharing = AsyncMock(return_value="paused")
    monkeypatch.setattr(tools.local_sessions, "decide_join", decide_join)
    monkeypatch.setattr(tools.local_sessions, "set_sharing", set_sharing)
    from core.services import local_session_relay

    publish = AsyncMock()
    monkeypatch.setattr(local_session_relay, "publish_resources_changed", publish)
    allow = tools.NativeDecisionRequest(
        session_id=_SID, generation="g1", decision="allow", user_id="user_z", request_id="r1"
    )
    assert await tools.decide(_decision_device(), allow) == "joined"
    auth = decide_join.await_args.args[0]
    assert (auth.user_id, auth.org_id) == ("user_a", "org_a")
    assert decide_join.await_args.args[1:4] == (str(_SID), "user_z", "allow")
    assert decide_join.await_args.kwargs == {"request_id": "r1"}
    pause = tools.NativeDecisionRequest(session_id=_SID, generation="g1", decision="pause")
    assert await tools.decide(_decision_device(), pause) == "paused"
    assert set_sharing.await_args.args[1:] == (str(_SID), False)
    assert set_sharing.await_args.kwargs == {"generation": "g1"}
    publish.assert_awaited_once_with("org_a", "user_a")


@pytest.mark.parametrize(
    "fields",
    [
        {"decision": "allow"},
        {"decision": "decline", "user_id": "user_z"},
        {"decision": "pause", "user_id": "user_z", "request_id": "r1"},
    ],
)
def test_decision_body_names_a_request_exactly_when_it_answers_one(fields):
    with pytest.raises(ValueError):
        tools.NativeDecisionRequest(session_id=uuid.uuid4(), generation="g1", **fields)


@pytest.mark.asyncio
async def test_set_sharing_refuses_a_run_the_caller_did_not_see(monkeypatch):
    sessions = tools.local_sessions
    row = {"sessionId": str(_SID), "humanUserId": "user_a", "generation": "g2"}
    monkeypatch.setattr(sessions, "authorize_session", AsyncMock(return_value=(row, {})))
    put = AsyncMock()
    monkeypatch.setattr(sessions.repo, "put_session", put)
    with pytest.raises(ApiProblem):
        await sessions.set_sharing(
            tools.AuthContext(user_id="user_a", org_id="org_a"), str(_SID), False, generation="g1"
        )
    put.assert_not_awaited()


@pytest.mark.parametrize(
    ("request_id", "routed"),
    [("rp", "pickup"), ("rj", "join"), ("unknown", "join")],
)
async def test_the_mac_answers_each_request_by_its_own_id(monkeypatch, request_id, routed):
    """One person with a join and a pick-up request pending; an older desktop sends no kind."""
    row = _decision_row(
        joinRequests={"user_z": {"state": "pending", "requestId": "rj"}},
        pickupRequests={"user_z": {"state": "pending", "requestId": "rp"}},
    )
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=row))
    _connected(monkeypatch)
    doors = {"join": AsyncMock(return_value="join"), "pickup": AsyncMock(return_value="pickup")}
    monkeypatch.setattr(tools.local_sessions, "decide_join", doors["join"])
    monkeypatch.setattr(tools.local_sessions, "decide_pickup_request", doors["pickup"])
    body = tools.NativeDecisionRequest(
        session_id=_SID, generation="g1", decision="allow", user_id="user_z", request_id=request_id
    )
    assert await tools.decide(_decision_device(), body) == routed
    door = doors[routed]
    assert door.await_args.args[1:] == (str(_SID), "user_z", "allow")
    assert door.await_args.kwargs == {"request_id": request_id}
    assert not doors["pickup" if routed == "join" else "join"].await_count
