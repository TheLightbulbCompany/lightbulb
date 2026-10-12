"""The Mac browser lease routes: hook token + active run authority, error detail, internal forward."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.repositories import container_repo
from core.services import desktop_execution, mac_browser
from core.services.desktop_execution import DesktopError

OWNER = "org_1"
ROOM = {"ownerId": OWNER, "memberId": "user_1", "runId": "r1", "agentId": "a1", "family": "room", "roomId": "rm1"}
HOOK = {"Authorization": "Bearer hook-good"}
RUN = {"runId": "r1", "agentId": "a1", "family": "room"}


@pytest.fixture
def api(monkeypatch):
    from routers import container, desktop_execution as desktop_router

    state = {"ended": False, "authorized": [], "allow_session": []}

    async def get_row(owner, consistent=False):
        return {"hook_token": "hook-good"}

    async def authorize_run(owner, run_id, agent_id, family, *, active, allow_session=False):
        state["authorized"].append(active)
        state["allow_session"].append(allow_session)
        if state["ended"] and active:
            raise DesktopError("run_not_active", 403)
        return ROOM

    monkeypatch.setattr(container_repo, "get_by_owner_id", get_row)
    monkeypatch.setattr(desktop_execution, "authorize_run", authorize_run)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "s3cret")
    for name in ("acquire", "release", "synced"):
        monkeypatch.setattr(mac_browser, name, AsyncMock(return_value={"name": name}))
    app = FastAPI()
    app.include_router(container.browser_internal_router, prefix="/api/v1/internal/browser")
    app.include_router(desktop_router.router, prefix="/api/v1/desktop-execution")
    with TestClient(app) as client:
        yield client, state


def _post(client, action, body, headers=HOOK):
    return client.post(f"/api/v1/internal/browser/{OWNER}/mac/{action}", json=body, headers=headers)


def test_mac_acquire_requires_hook_token(api):
    client, state = api
    assert _post(client, "acquire", RUN, headers={}).status_code == 401
    assert _post(client, "acquire", RUN, headers={"Authorization": "Bearer wrong"}).status_code == 401
    mac_browser.acquire.assert_not_awaited()
    assert state["authorized"] == []  # the hook token alone never authorizes a runId


def test_mac_acquire_authorizes_run_active(api):
    client, state = api
    response = _post(client, "acquire", RUN | {"deviceId": "mac1"})
    assert response.status_code == 200 and response.json() == {"name": "acquire"}
    assert response.headers["cache-control"] == "no-store"
    assert state["authorized"] == [True]
    mac_browser.acquire.assert_awaited_once_with(ROOM, "mac1", budget_s=25.0)
    _post(client, "acquire", RUN | {"budgetS": 12.5})
    assert mac_browser.acquire.await_args.kwargs == {"budget_s": 12.5}  # the bridge's remaining time
    for bad in (0, 26, "x"):
        assert _post(client, "acquire", RUN | {"budgetS": bad}).status_code == 422
    state["ended"] = True
    response = _post(client, "acquire", RUN)
    assert response.status_code == 403 and response.json()["detail"]["code"] == "run_not_active"
    assert response.json()["detail"]["message"]


def test_mac_release_requires_active_run_but_synced_lands_after_run_end(api):
    """A sync may finish after run.ended (Stop, a slow save): refusing its report would leave the
    claim live for CLAIM_SECONDS. The syncId fence is its guard, so synced skips `active`."""
    client, state = api
    state["ended"] = True
    release = _post(client, "release", RUN | {"deviceId": "mac1", "generation": "g1"})
    assert release.status_code == 200 and release.json() == {"sync": None}
    mac_browser.release.assert_not_awaited()
    synced = _post(client, "synced", RUN | {"deviceId": "mac1", "syncId": "s1", "ok": True})
    assert synced.status_code == 200 and synced.json() == {"name": "synced"}
    assert state["authorized"] == [True, False]
    mac_browser.synced.assert_awaited_once_with(ROOM, "mac1", "s1", True)


def test_mac_acquire_starting_is_202(api):
    client, _ = api
    mac_browser.acquire.return_value = mac_browser.STARTING
    response = _post(client, "acquire", RUN)
    assert response.status_code == 202 and response.json() == {"status": "starting"}


def test_mac_acquire_error_detail_has_code_and_message(api):
    client, _ = api
    mac_browser.acquire.side_effect = mac_browser._err("no_mac")
    response = _post(client, "acquire", RUN)
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "no_mac", "message": mac_browser.MESSAGES["no_mac"]}
    mac_browser.acquire.side_effect = DesktopError("desktop_peer_unavailable", 503)  # no message of its own
    detail = _post(client, "acquire", RUN).json()["detail"]
    assert detail == {"code": "desktop_peer_unavailable", "message": mac_browser.GENERIC_MESSAGE}
    mac_browser.acquire.side_effect = RuntimeError("https://secret-url")
    response = _post(client, "acquire", RUN)
    assert response.status_code == 503 and "secret" not in response.text


def _internal(client, body, secret="s3cret"):
    return client.post(
        "/api/v1/desktop-execution/internal", json=body, headers={"x-lightbulb-ws-internal": secret} if secret else {}
    )


def test_internal_mac_acquire_reauthorizes_active(api):
    client, state = api
    body = {"action": "mac_acquire", "auth": ROOM, "request": {"deviceId": "mac1"}}
    response = _internal(client, body)
    assert response.status_code == 200 and response.json() == {"name": "acquire"}
    assert state["authorized"] == [True]
    assert state["allow_session"] == [True]  # the /internal route authorizes Mac actions with allow_session
    mac_browser.acquire.assert_awaited_once_with(ROOM, "mac1", forwarded=True, budget_s=25)
    _internal(client, body | {"request": {"deviceId": "mac1", "budgetS": 19.5}})
    assert mac_browser.acquire.await_args.kwargs == {"forwarded": True, "budget_s": 19.5}
    for bad in (0, 26, True, "9"):
        assert _internal(client, body | {"request": {"deviceId": "mac1", "budgetS": bad}}).status_code == 400
    assert (
        _internal(client, body | {"action": "mac_release", "request": {"deviceId": "mac1", "budgetS": 5}}).status_code
        == 400
    )
    assert set(state["authorized"]) == {True}
    _internal(client, body | {"action": "mac_release"})
    assert state["authorized"][-1] is True
    mac_browser.release.assert_awaited_once_with(ROOM, "mac1", "", forwarded=True)
    mac_browser.acquire.side_effect = mac_browser._err("chrome_missing")
    response = _internal(client, body)
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "chrome_missing", "message": mac_browser.MESSAGES["chrome_missing"]}
    assert _internal(client, body | {"request": {"deviceId": "../x"}}).status_code == 400


def test_internal_mac_actions_require_internal_secret(api):
    client, state = api
    body = {"action": "mac_acquire", "auth": ROOM, "request": {"deviceId": "mac1"}}
    assert _internal(client, body, secret=None).status_code == 403
    assert _internal(client, body, secret="wrong").status_code == 403
    mac_browser.acquire.assert_not_awaited()
    assert state["authorized"] == []
