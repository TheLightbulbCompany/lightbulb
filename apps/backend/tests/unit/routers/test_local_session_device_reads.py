"""A paired Mac reads what its enrolled human may read, and nothing else."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.services import machine_auth
from core.services.messaging import ApiProblem
from routers import messaging as router

ROOM = "22222222-2222-4222-8222-222222222222"
REQUEST = "33333333-3333-4333-8333-333333333333"
DEVICE = SimpleNamespace(user_id="user_mac", org_id="org_1", owner_id="org_1", device_id="dev_1")
ROUTES = [
    ("/api/v1/local-sessions/device/rooms?query=gen", "rooms_for"),
    (f"/api/v1/local-sessions/device/rooms/{ROOM}/messages?after_seq=3", "messages_for"),
    ("/api/v1/local-sessions/device/sessions", "sessions_for"),
    (f"/api/v1/local-sessions/device/receipt?room_id={ROOM}&request_id={REQUEST}", "receipt_for"),
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(machine_auth, "verify_upgrade", AsyncMock(return_value=DEVICE))
    app = FastAPI()
    app.include_router(router.router, prefix="/api/v1")
    app.add_exception_handler(ApiProblem, lambda _r, exc: JSONResponse(exc.body(), exc.status_code))
    return TestClient(app)


@pytest.mark.parametrize(("path", "reader"), ROUTES)
def test_enabled_enrollment_reads_as_the_human(client, monkeypatch, path, reader):
    monkeypatch.setattr(router.local_sessions, "_enrolled", AsyncMock(return_value={"enabled": True}))
    read = AsyncMock(return_value={"ok": reader})
    monkeypatch.setattr(router.hosted_mcp, reader, read)
    response = client.get(path, headers={"Authorization": "Bearer device"})
    assert response.status_code == 200 and response.json() == {"ok": reader}
    auth = read.await_args.args[0]
    assert (auth.user_id, auth.org_id) == ("user_mac", "org_1")
    router.local_sessions._enrolled.assert_awaited_once_with("org_1", "user_mac", "dev_1")


@pytest.mark.parametrize(("path", "reader"), ROUTES)
def test_disabled_or_revoked_device_is_refused(client, monkeypatch, path, reader):
    read = AsyncMock()
    monkeypatch.setattr(router.hosted_mcp, reader, read)
    monkeypatch.setattr(
        router.local_sessions,
        "_enrolled",
        AsyncMock(side_effect=ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session sharing is disabled.")),
    )
    assert client.get(path, headers={"Authorization": "Bearer device"}).status_code == 403
    machine_auth.verify_upgrade.return_value = None  # unpaired: the pairing row is gone
    assert client.get(path, headers={"Authorization": "Bearer device"}).status_code == 403
    read.assert_not_awaited()


def test_device_human_guards_no_write_route():
    def deps(dependant):
        for dep in dependant.dependencies:
            yield dep.call
            yield from deps(dep)

    guarded = [route for route in router.router.routes if router.device_human in set(deps(route.dependant))]
    assert len(guarded) == len(ROUTES)
    assert all(route.methods == {"GET"} for route in guarded)
