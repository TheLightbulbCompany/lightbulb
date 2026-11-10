"""An agent turn acts in the workspace with exactly the rights of the person who asked it (the turn
grant's requester), through the same service calls the browser makes."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.repositories import messaging_repo
from core.services import local_session_grants
from core.services.messaging import ApiProblem
from routers import messaging as router
from tests.unit.repositories.test_messaging_repo import FRIDAY, GAUGE, OWNER, USER_A, _room, table  # noqa: F401

ROOM = "22222222-2222-4222-8222-222222222222"
URL = "/api/v1/messaging/agent-act"
ADD = {
    "action": "add_member",
    "room_id": ROOM,
    "args": {"kind": "agent", "id": "atlas", "role": "member", "listen_mode": "active"},
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "k" * 32)
    app = FastAPI()
    app.include_router(router.router, prefix="/api/v1")
    app.add_exception_handler(ApiProblem, lambda _r, exc: JSONResponse(exc.body(), exc.status_code))
    return TestClient(app)


def _bearer(requester, reader=None):
    grant = local_session_grants.mint(
        "org_1", ROOM, "friday", "44444444-4444-4444-8444-444444444444", requester, reader
    )
    return {"Authorization": f"Bearer {grant}"}


@pytest.fixture
def seated(monkeypatch):
    """The grant's room, as require_caller reads it: the agent seated, no other org in it."""
    seated = {("agent", "friday"), ("human", "user_ana")}
    member = AsyncMock(side_effect=lambda room, kind, mid: {"kind": kind, "id": mid} if (kind, mid) in seated else None)
    member.seated = seated
    monkeypatch.setattr(messaging_repo, "get_room", AsyncMock(return_value={"roomId": ROOM, "ownerId": "org_1"}))
    monkeypatch.setattr(messaging_repo, "get_member", member)
    monkeypatch.setattr(messaging_repo, "has_foreign_members", AsyncMock(return_value=False))
    return member


def test_add_member_acts_as_the_requester(client, monkeypatch, seated):
    upsert = AsyncMock(return_value={"roomId": ROOM})
    monkeypatch.setattr(router.service, "upsert_member", upsert)
    response = client.post(URL, json=ADD, headers=_bearer("user_ana"))
    assert response.status_code == 200 and response.json() == {"roomId": ROOM}
    auth, room_id, request = upsert.await_args.args
    assert (auth.user_id, auth.org_id, room_id) == ("user_ana", "org_1", ROOM)
    assert (request.kind, request.id, request.role) == ("agent", "atlas", "member")


@pytest.mark.parametrize(("requester", "reader"), [(None, None), (None, "user_ana")])
def test_a_turn_nobody_signed_in_asked_acts_for_no_one(client, monkeypatch, seated, requester, reader):
    """A Meet's reader may read for the room, never act for it."""
    upsert = AsyncMock()
    monkeypatch.setattr(router.service, "upsert_member", upsert)
    response = client.post(URL, json=ADD, headers=_bearer(requester, reader))
    assert response.status_code == 403 and response.json()["code"] == "ROOM_FORBIDDEN"
    assert client.post(URL, json=ADD, headers={"Authorization": "Bearer forged"}).status_code == 403
    upsert.assert_not_awaited()


@pytest.mark.parametrize(
    ("left", "foreign"), [(("agent", "friday"), False), (("human", "user_ana"), False), (None, True)]
)
def test_an_agent_or_requester_no_longer_in_the_turns_room_acts_for_no_one(client, monkeypatch, seated, left, foreign):
    """Nor in a room another organization is in."""
    upsert = AsyncMock()
    monkeypatch.setattr(router.service, "upsert_member", upsert)
    seated.seated.discard(left)
    monkeypatch.setattr(messaging_repo, "has_foreign_members", AsyncMock(return_value=foreign))
    assert client.post(URL, json=ADD, headers=_bearer("user_ana")).status_code == 403
    upsert.assert_not_awaited()


def test_a_service_refusal_passes_through_unchanged(client, monkeypatch, seated):
    problem = ApiProblem("ROOM_FORBIDDEN", "Room owner or admin access required.")
    monkeypatch.setattr(router.service, "upsert_member", AsyncMock(side_effect=problem))
    response = client.post(URL, json=ADD, headers=_bearer("user_ana"))
    assert (response.status_code, response.json()) == (403, problem.body())


def test_an_unknown_action_or_bad_args_is_a_readable_4xx(client, monkeypatch, seated):
    upsert = AsyncMock()
    monkeypatch.setattr(router.service, "upsert_member", upsert)
    for action in ("delete_room", "leave_room"):  # leave acts as the person
        assert client.post(URL, json={**ADD, "action": action}, headers=_bearer("user_ana")).status_code == 422
    bad = client.post(URL, json={**ADD, "args": {"kind": "robot"}}, headers=_bearer("user_ana"))
    assert bad.status_code == 400 and "kind" in bad.json()["message"]
    missing = client.post(URL, json={"action": "pin", "room_id": ROOM, "args": {}}, headers=_bearer("user_ana"))
    assert missing.status_code == 400 and "message_id" in missing.json()["message"]
    upsert.assert_not_awaited()


@pytest.mark.parametrize(
    ("action", "args", "name", "called_with"),
    [
        ("remove_member", {"kind": "agent", "id": "atlas"}, "remove_member", (ROOM, "agent", "atlas")),
        ("pin", {"message_id": "m1"}, "set_pin", (ROOM, "m1", True)),
        ("unpin", {"message_id": "m1"}, "set_pin", (ROOM, "m1", False)),
    ],
)
def test_each_action_reaches_its_service_call(client, monkeypatch, seated, action, args, name, called_with):
    call = AsyncMock(return_value=None)
    monkeypatch.setattr(router.service, name, call)
    response = client.post(URL, json={"action": action, "room_id": ROOM, "args": args}, headers=_bearer("user_ana"))
    assert response.status_code == 200 and response.json() == {"ok": True}  # a None result still answers
    assert call.await_args.args[0].user_id == "user_ana" and call.await_args.args[1:] == called_with


def test_the_incident_an_agent_added_through_agent_act_gets_its_delivery_cursor(client, monkeypatch, table):
    """18 Oct: a hand-written member row skipped the cursor and silenced the agent's @mentions."""
    import asyncio

    room_id, _ = asyncio.run(_room())  # USER_A owns it; FRIDAY is seated
    roster = [{"id": FRIDAY, "name": "Friday"}, {"id": GAUGE, "name": "Gauge"}]
    monkeypatch.setattr(router.service, "load_agent_roster", AsyncMock(return_value=roster))
    monkeypatch.setattr(router.service, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(router.service, "_emit_member_added", lambda *a, **kw: None)
    publisher = type("P", (), {"publish_user": AsyncMock(), "publish_room": AsyncMock()})()
    monkeypatch.setattr(router.service, "get_live_event_publisher", lambda: publisher)
    grant = local_session_grants.mint(OWNER, room_id, FRIDAY, "44444444-4444-4444-8444-444444444444", USER_A)
    body = {
        "action": "add_member",
        "room_id": room_id,
        "args": {"kind": "agent", "id": GAUGE, "role": "member", "listenMode": "active"},
    }
    response = client.post(URL, json=body, headers={"Authorization": f"Bearer {grant}"})
    assert response.status_code == 200, response.json()
    assert asyncio.run(messaging_repo.get_agent_delivery(room_id, GAUGE)) is not None


@pytest.mark.parametrize("error", [KeyError("roomId"), ValueError("boom")])
def test_a_service_bug_is_not_blamed_on_the_agents_args(client, monkeypatch, seated, error):
    """Only reading args is the agent's mistake: a KeyError deep in the service stays a server error."""
    monkeypatch.setattr(router.service, "set_pin", AsyncMock(side_effect=error))
    metrics = []
    monkeypatch.setattr(router, "put_metric", lambda name, **kw: metrics.append(kw["dimensions"]["outcome"]))
    with pytest.raises(type(error)):
        client.post(
            URL, json={"action": "pin", "room_id": ROOM, "args": {"message_id": "m1"}}, headers=_bearer("user_ana")
        )
    assert metrics == ["error"]


def test_a_service_validation_error_is_not_blamed_on_the_agents_args(client, monkeypatch, seated):
    from pydantic import ValidationError

    from core.services.messaging import PatchRoomRequest

    try:
        PatchRoomRequest(bogus=1)
    except ValidationError as exc:
        inner = exc
    monkeypatch.setattr(router.service, "patch_room", AsyncMock(side_effect=inner))
    with pytest.raises(ValidationError):
        client.post(
            URL, json={"action": "update_room", "room_id": ROOM, "args": {"name": "x"}}, headers=_bearer("user_ana")
        )


@pytest.mark.parametrize("action", ["search_messages", "search_sessions"])
def test_retired_search_actions_are_rejected(client, seated, action):
    response = client.post(URL, json={"action": action, "args": {"q": "deploy"}}, headers=_bearer("user_ana"))
    assert response.status_code == 422
