"""The Browser tab's routes: who may watch and drive a shared session's browser."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.auth import AuthContext, get_human_session
from core.services import task_address
from core.services.connection_service import ConnectionService
from core.services.messaging import ApiProblem
from routers import container
from routers import messaging as router

SID = "11111111-1111-4111-8111-111111111111"
ROW = {
    "ownerId": "org_1",
    "sessionId": SID,
    "humanUserId": "user_1",
    "deviceId": "mac1",
    "browserOpen": {"where": "mac", "until": 2**40},
}
CLICK = {"type": "down", "x": 10, "y": 20, "button": "left", "clicks": 1}
HERE = "http://10.0.0.1:8000"


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(router.router, prefix="/api/v1")
    app.add_exception_handler(ApiProblem, lambda _r, exc: JSONResponse(exc.body(), exc.status_code))
    app.dependency_overrides[get_human_session] = lambda: AuthContext(user_id="user_2", org_id="org_1")

    async def desktop_task_address(self, owner, device):
        return HERE

    monkeypatch.setattr(ConnectionService, "desktop_task_address", desktop_task_address)
    monkeypatch.setattr(task_address, "own_address", lambda: HERE)
    monkeypatch.setattr(router.session_browser.session_screen, "_still_a_member", AsyncMock())
    return TestClient(app)


def test_a_person_who_may_not_type_cannot_drive_the_browser(client, monkeypatch):
    monkeypatch.setattr(
        router.local_sessions,
        "authorize_session",
        AsyncMock(side_effect=ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can do that.")),
    )
    delivered = AsyncMock()
    monkeypatch.setattr(container, "deliver_mac_input", delivered)
    response = client.post(f"/api/v1/messaging/sessions/{SID}/browser/input", json={"events": [CLICK]})
    assert response.status_code == 403
    delivered.assert_not_awaited()
    assert router.local_sessions.authorize_session.await_args.kwargs == {"owner_only": True}


def test_input_reaches_the_session_run_on_the_task_holding_the_mac(client, monkeypatch):
    monkeypatch.setattr(router.local_sessions, "authorize_session", AsyncMock(return_value=(dict(ROW), {})))
    delivered = AsyncMock()
    monkeypatch.setattr(container, "deliver_mac_input", delivered)
    assert client.post(f"/api/v1/messaging/sessions/{SID}/browser/input", json={"events": [CLICK]}).status_code == 204
    delivered.assert_awaited_once_with(
        "org_1",
        "user_2",
        f"session-{SID}",
        [{"type": "down", "x": 10.0, "y": 20.0, "button": "left", "clicks": 1}],
        viewer_owner=None,
    )


def test_a_closed_browser_takes_no_input_and_no_watch(client, monkeypatch):
    monkeypatch.setattr(
        router.local_sessions,
        "authorize_session",
        AsyncMock(return_value=({**ROW, "browserOpen": {"where": "mac", "until": 1}}, {})),
    )
    assert client.post(f"/api/v1/messaging/sessions/{SID}/browser/input", json={"events": [CLICK]}).status_code == 409
    assert client.post(f"/api/v1/messaging/sessions/{SID}/browser/viewing").status_code == 409


def test_a_refusal_where_the_mac_is_speaks_of_the_session_not_a_room(client, monkeypatch):
    monkeypatch.setattr(router.local_sessions, "authorize_session", AsyncMock(return_value=(dict(ROW), {})))
    monkeypatch.setattr(container.mac_live, "input", AsyncMock(side_effect=PermissionError("run")))
    response = client.post(f"/api/v1/messaging/sessions/{SID}/browser/input", json={"events": [CLICK]})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "session_forbidden"
