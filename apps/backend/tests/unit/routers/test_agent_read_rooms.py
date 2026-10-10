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


def _grant(requester):
    return local_session_grants.mint("org_1", ROOM, "friday", "44444444-4444-4444-8444-444444444444", requester)


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
