from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from core.services import desktop_execution as service
from routers import desktop_execution as routes


AUTH = {
    "ownerId": "user_1",
    "memberId": "user_1",
    "runId": "run_1",
    "agentId": "agent_1",
    "family": "room",
    "roomId": "room_1",
}
HEADERS = {
    "Authorization": "Bearer hook",
    "X-Desktop-Owner": "user_1",
    "X-Desktop-Run": "run_1",
    "X-Desktop-Agent": "agent_1",
}


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/desktop-execution")
    return app


async def test_route_does_not_trust_hook_token_alone_or_member_headers(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        with patch.object(routes.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook"})):
            response = await client.get(
                "/api/v1/desktop-execution/devices", headers=HEADERS | {"Authorization": "Bearer wrong"}
            )
            assert response.status_code == 401
        with (
            patch.object(routes.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook"})),
            patch.object(service, "authorize_run", AsyncMock(side_effect=service.DesktopError("run_forbidden", 403))),
        ):
            response = await client.get(
                "/api/v1/desktop-execution/devices", headers=HEADERS | {"X-Desktop-Member": "other"}
            )
            assert response.status_code == 403


async def test_browser_upload_public_boundary_and_receipt_artifact_auth(app):
    import asyncio
    import base64
    import hashlib
    import uuid
    from tests.unit.services.test_desktop_browser import BrowserHarness, browser, TAB

    h = BrowserHarness()
    tab = (await h.run("tabs"))["result"]["tabs"][0]
    snap = (await h.run("snapshot", target=TAB, browserGeneration=tab["browserGeneration"]))["result"]
    data = b"abcd" * 32768
    request = browser(
        "upload",
        target=TAB,
        browserGeneration=snap["browserGeneration"],
        args={
            "ref": "e1",
            "files": [
                {
                    "name": "hello.bin",
                    "contentBase64": base64.b64encode(data).decode(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ],
        },
    ) | {"requestId": str(uuid.uuid4())}
    with (
        patch.object(service, "_operations", h.store),
        patch.object(routes.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook"})),
        patch.object(service, "authorize_run", AsyncMock(return_value=AUTH)),
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/desktop-execution/operations",
                json=request,
                headers=HEADERS | {"X-Desktop-Admission": h.store.admit(AUTH, h.target)},
            )
            assert response.status_code == 200, response.text
            await asyncio.gather(*h.store.tasks.values())
            assert (await h.store.get(AUTH, response.json()["id"]))["state"] == "succeeded"
            h.media = data
            tab = (await h.run("tabs"))["result"]["tabs"][0]
            receipt = await h.run("screenshot", target=TAB, browserGeneration=tab["browserGeneration"])
            url = f"/api/v1/desktop-execution/operations/{receipt['id']}/artifacts/0?offset=0"
            chunk = await client.get(url, headers=HEADERS)
            assert chunk.status_code == 200, chunk.text
            assert base64.b64decode(chunk.json()["base64"]) == data
            assert (await client.get(url, headers=HEADERS | {"Authorization": "Bearer wrong"})).status_code == 401


async def test_status_cancel_survive_gate_and_ended_run(app):
    checks = []

    async def authorize(owner, run, agent, family, *, active):
        checks.append(active)
        return AUTH

    with (
        patch.object(routes.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook"})),
        patch.object(service, "authorize_run", authorize),
        patch.object(service, "get_desktop_receipt", AsyncMock(return_value={"state": "unknown"})),
        patch.object(service, "cancel_desktop", AsyncMock(return_value={"state": "cancel_requested"})),
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (
                await client.get("/api/v1/desktop-execution/operations/receipt", headers=HEADERS)
            ).status_code == 200
            assert (await client.post("/api/v1/desktop-execution/operations/receipt/cancel", headers=HEADERS)).json()[
                "state"
            ] == "cancel_requested"
    assert checks == [False, False]


async def test_internal_requires_secret_even_when_legacy_internal_enforcement_off(app):
    with (
        patch.object(routes.settings, "WS_INTERNAL_SECRET", "internal"),
        patch.object(routes.settings, "WS_ENFORCE_INTERNAL_SECRET", False),
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/desktop-execution/internal", json={"action": "cancel", "auth": AUTH, "receiptId": "forged"}
            )
            assert response.status_code == 403


async def test_real_room_authority_comes_from_human_trigger_and_current_membership():
    from core.repositories import messaging_repo

    run = {
        "ownerId": "user_1",
        "runId": "run_1",
        "agentId": "agent_1",
        "roomId": "room_1",
        "triggerMessageId": "message_1",
        "status": "started",
        "eventKind": "user_request",
    }
    with (
        patch.object(messaging_repo, "get_run", AsyncMock(return_value=run)),
        patch.object(messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "user_1"})),
        patch.object(messaging_repo, "get_member", AsyncMock(return_value={"id": "user_1"})),
        patch.object(messaging_repo, "has_foreign_members", AsyncMock(return_value=False)),
        patch.object(
            messaging_repo, "get_message_by_id", AsyncMock(return_value={"author": {"kind": "human", "id": "user_1"}})
        ) as message,
    ):
        assert await service.authorize_run("user_1", "run_1", "agent_1", "room", active=True) == AUTH
        message.return_value = {"author": {"kind": "agent", "id": "user_1"}}
        with pytest.raises(service.DesktopError, match="run_member_authority_unavailable"):
            await service.authorize_run("user_1", "run_1", "agent_1", "room", active=True)
        message.return_value = {"author": {"kind": "human", "id": "user_1"}}
        run["status"] = "ended"
        assert await service.authorize_run("user_1", "run_1", "agent_1", "room", active=False) == AUTH
        with pytest.raises(service.DesktopError, match="run_not_active"):
            await service.authorize_run("user_1", "run_1", "agent_1", "room", active=True)


async def test_paperclip_rejects_forged_actual_run_and_discloses_missing_member_authority():
    from types import SimpleNamespace
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services import paperclip_admin_client, paperclip_admin_session

    admin = SimpleNamespace(
        _http=None,
        get_heartbeat_run=AsyncMock(
            return_value={"id": "run_1", "companyId": "company_1", "agentId": "other", "status": "running"}
        ),
        get_agent=AsyncMock(return_value={"companyId": "company_1"}),
    )
    with (
        patch.object(
            PaperclipRepo, "resolve_owner_row", AsyncMock(return_value=SimpleNamespace(company_id="company_1"))
        ),
        patch.object(paperclip_admin_client, "shared_admin_client", lambda: admin),
        patch.object(paperclip_admin_session, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
    ):
        with pytest.raises(service.DesktopError, match="run_forbidden"):
            await service.authorize_run("user_1", "run_1", "agent_1", "paperclip", active=True)
        admin.get_heartbeat_run.return_value["agentId"] = "agent_1"
        with pytest.raises(service.DesktopError, match="paperclip_member_authority_unavailable"):
            await service.authorize_run("user_1", "run_1", "agent_1", "paperclip", active=True)


async def test_public_submit_rejects_bypass_fields_and_internal_caller_addresses(app):
    with (
        patch.object(routes.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook"})),
        patch.object(service, "authorize_run", AsyncMock(return_value=AUTH)),
        patch.object(routes.settings, "WS_INTERNAL_SECRET", "internal"),
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/desktop-execution/operations",
                json={"approved": True, "command": "system.run"},
                headers=HEADERS,
            )
            assert response.status_code == 400
            response = await client.post(
                "/api/v1/desktop-execution/internal",
                json={"action": "cancel", "auth": AUTH, "receiptId": "x", "taskAddress": "http://attacker"},
                headers={"x-lightbulb-ws-internal": "internal"},
            )
            assert response.status_code == 400
            response = await client.post("/api/v1/desktop-execution/operations", content=b"x" * 16385, headers=HEADERS)
            assert response.status_code == 413
