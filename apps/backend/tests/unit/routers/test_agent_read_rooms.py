"""An agent turn reads rooms as the person who asked it (the turn grant's requester), and as no one
when nobody signed in asked."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.services import local_session_grants
from core.services.messaging import ApiProblem
from routers import messaging as router

ROOM = "22222222-2222-4222-8222-222222222222"
ROUTES = [
    ("/api/v1/messaging/agent-read/rooms?query=launch", "rooms_for"),
    (f"/api/v1/messaging/agent-read/messages?room_id={ROOM}&after_seq=3", "messages_for"),
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "k" * 32)
    app = FastAPI()
    app.include_router(router.router, prefix="/api/v1")
    app.add_exception_handler(ApiProblem, lambda _r, exc: JSONResponse(exc.body(), exc.status_code))
    return TestClient(app)


def _grant(requester, reader=None):
    return local_session_grants.mint("org_1", ROOM, "friday", "44444444-4444-4444-8444-444444444444", requester, reader)


@pytest.fixture(autouse=True)
def agent_in_room(monkeypatch):
    """The grant's room, as require_caller reads it: the agent seated, no other org in it."""
    from core.repositories import messaging_repo

    seated = {("agent", "friday"), ("human", "user_ana")}
    member = AsyncMock(side_effect=lambda room, kind, mid: {"kind": kind, "id": mid} if (kind, mid) in seated else None)
    member.seated = seated
    monkeypatch.setattr(messaging_repo, "get_room", AsyncMock(return_value={"roomId": ROOM, "ownerId": "org_1"}))
    monkeypatch.setattr(messaging_repo, "get_member", member)
    monkeypatch.setattr(messaging_repo, "has_foreign_members", AsyncMock(return_value=False))
    return member


@pytest.mark.parametrize(("path", "reader"), ROUTES)
def test_a_turn_reads_with_its_requesters_access(client, monkeypatch, path, reader):
    read = AsyncMock(return_value={"ok": reader})
    monkeypatch.setattr(router.hosted_mcp, reader, read)
    response = client.get(path, headers={"Authorization": f"Bearer {_grant('user_ana')}"})
    assert response.status_code == 200 and response.json() == {"ok": reader}
    auth = read.await_args.args[0]
    assert (auth.user_id, auth.org_id) == ("user_ana", "org_1")  # the MCP's own membership rules apply


@pytest.mark.parametrize(("path", "reader"), ROUTES)
def test_a_turn_nobody_signed_in_asked_reads_nothing(client, monkeypatch, path, reader):
    """A Meet speaker, a voice run another speaker steered, a routine: the grant names nobody."""
    read = AsyncMock()
    monkeypatch.setattr(router.hosted_mcp, reader, read)
    response = client.get(path, headers={"Authorization": f"Bearer {_grant(None)}"})
    assert response.status_code == 403 and response.json()["code"] == "ROOM_FORBIDDEN"
    assert client.get(path, headers={"Authorization": "Bearer forged"}).status_code == 403
    assert client.get(path).status_code == 403
    read.assert_not_awaited()


def test_agent_reads_are_read_only():
    def deps(dependant):
        for dep in dependant.dependencies:
            yield dep.call
            yield from deps(dep)

    guarded = [route for route in router.router.routes if router.agent_reader in set(deps(route.dependant))]
    assert len(guarded) == len(ROUTES) and all(route.methods == {"GET"} for route in guarded)


@pytest.mark.parametrize(("path", "reader"), ROUTES)
def test_an_agent_removed_from_the_turns_room_reads_nothing(client, monkeypatch, agent_in_room, path, reader):
    """Codex #3997: an unexpired grant is not enough; the agent must still be in the turn's room."""
    read = AsyncMock()
    monkeypatch.setattr(router.hosted_mcp, reader, read)
    agent_in_room.seated.discard(("agent", "friday"))
    assert client.get(path, headers={"Authorization": f"Bearer {_grant('user_ana')}"}).status_code == 403
    read.assert_not_awaited()


@pytest.mark.parametrize(("path", "reader"), ROUTES)
def test_a_requester_who_left_the_turns_room_lends_nothing(client, monkeypatch, agent_in_room, path, reader):
    """Codex #3997: the person who asked has since left the room; the turn no longer acts for them."""
    read = AsyncMock()
    monkeypatch.setattr(router.hosted_mcp, reader, read)
    agent_in_room.seated.discard(("human", "user_ana"))
    assert client.get(path, headers={"Authorization": f"Bearer {_grant('user_ana')}"}).status_code == 403
    read.assert_not_awaited()


@pytest.mark.parametrize(("path", "reader"), ROUTES)
def test_a_turn_in_a_deleted_room_reads_nothing(client, monkeypatch, path, reader):
    """Codex #3997: deleting a room tombstones it but keeps its members; its running turn reads nothing."""
    from core.repositories import messaging_repo

    read = AsyncMock()
    monkeypatch.setattr(router.hosted_mcp, reader, read)
    monkeypatch.setattr(
        messaging_repo,
        "get_room",
        AsyncMock(return_value={"roomId": ROOM, "ownerId": "org_1", "deletedAt": "2026-10-09"}),
    )
    assert client.get(path, headers={"Authorization": f"Bearer {_grant('user_ana')}"}).status_code == 403
    read.assert_not_awaited()


@pytest.mark.parametrize(("path", "reader"), ROUTES)
def test_a_meet_turn_reads_as_the_inviter_and_never_carries_their_other_rights(client, monkeypatch, path, reader):
    """Owner ruling 2026-10-13: a Google Meet's turn reads as the member who invited the agents."""
    read = AsyncMock(return_value={"rooms": []} if reader == "rooms_for" else {"ok": reader})
    monkeypatch.setattr(router.hosted_mcp, reader, read)
    monkeypatch.setattr(router.service, "get_room", AsyncMock(return_value=object()))
    monkeypatch.setattr(router.hosted_mcp, "workspace_wide", lambda room, people: True)
    monkeypatch.setattr(router.hosted_mcp, "workspace_people", AsyncMock(return_value=frozenset({"user_ana"})))
    grant = _grant(None, "user_ana")
    response = client.get(path, headers={"Authorization": f"Bearer {grant}"})
    assert response.status_code == 200 and read.await_args.args[0].user_id == "user_ana"
    auth = local_session_grants.verify(grant)
    assert (auth.requester_id, auth.reader_id) == (None, "user_ana")  # the Mac and sessions see nobody


@pytest.mark.parametrize(("path", "reader"), ROUTES)
def test_an_inviter_who_left_the_room_reads_nothing(client, monkeypatch, path, reader, agent_in_room):
    agent_in_room.seated.discard(("human", "user_ana"))
    read = AsyncMock()
    monkeypatch.setattr(router.hosted_mcp, reader, read)
    response = client.get(path, headers={"Authorization": f"Bearer {_grant(None, 'user_ana')}"})
    assert response.status_code == 403 and response.json()["code"] == "ROOM_FORBIDDEN"
    read.assert_not_awaited()


def test_a_meet_turn_reads_only_channels_the_whole_workspace_is_in(client, monkeypatch):
    """Owner ruling 2026-10-13: everyone on a Meet hears the answer, so reading as the inviter covers
    only what any teammate could read: channels every person in the workspace is in. Every room is
    private to its members, so a channel with a narrower membership is as private as a DM. Enforced by
    the backend, filtered before the limit and the total."""
    from types import SimpleNamespace as NS

    def room(rid, kind, name, people):
        return NS(
            room_id=rid,
            kind=kind,
            name=name,
            members=[NS(id=p, kind="human", display_name=p, mention_handle=None) for p in people],
        )

    team = frozenset({"user_ana", "user_bo"})
    listed = [
        room("d", "dm", "launch dm", ["user_ana", "user_bo"]),  # everyone, but a DM
        room("f", "channel", "launch-fundraise", ["user_ana"]),  # a narrower channel
        room("c", "channel", "launch", ["user_ana", "user_bo"]),
    ]
    monkeypatch.setattr(router.hosted_mcp.messaging, "list_rooms", AsyncMock(return_value=NS(rooms=listed)))
    monkeypatch.setattr(router.hosted_mcp, "workspace_people", AsyncMock(return_value=team))
    reader = {"Authorization": f"Bearer {_grant(None, 'user_ana')}"}
    found = client.get("/api/v1/messaging/agent-read/rooms?query=launch&limit=1", headers=reader).json()
    assert [r["roomId"] for r in found["rooms"]] == ["c"] and found["total"] == 1
    asker = client.get(
        "/api/v1/messaging/agent-read/rooms?query=launch", headers={"Authorization": f"Bearer {_grant('user_ana')}"}
    )
    assert asker.json()["total"] == 3  # a signed-in asker keeps everything they can open
    read = AsyncMock(return_value={"messages": []})
    monkeypatch.setattr(router.hosted_mcp, "messages_for", read)
    for r, status in ((listed[0], 403), (listed[1], 403), (listed[2], 200)):
        monkeypatch.setattr(router.service, "get_room", AsyncMock(return_value=r))
        got = client.get(f"/api/v1/messaging/agent-read/messages?room_id={ROOM}", headers=reader)
        assert got.status_code == status, r.room_id
    assert read.await_count == 1
    monkeypatch.setattr(router.hosted_mcp, "workspace_people", AsyncMock(return_value=frozenset()))  # Clerk down
    assert client.get("/api/v1/messaging/agent-read/rooms?query=launch", headers=reader).json()["total"] == 0
