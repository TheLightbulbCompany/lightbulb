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


async def test_native_approval_wrapper_has_room_for_maximum_command_summary(app):
    # A valid near-16KiB normalized command plus its authenticated prompt wrapper
    # is larger than the public command budget, yet comfortably below 32KiB.
    body = {
        "params": {"operation": {"command": "system.run", "params": {"argv": ["a" * 3980] * 4, "cwd": "/tmp"}}},
        "binding": "b" * 512,
    }
    with (
        patch.object(routes.settings, "WS_INTERNAL_SECRET", "internal"),
        patch.object(service, "handle_approval_peer", AsyncMock(return_value={"approved": False})),
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/desktop-execution/internal/approval",
                json=body,
                headers={"x-lightbulb-ws-internal": "internal"},
            )
            assert response.status_code == 200
            body["padding"] = "x" * 32768
            response = await client.post(
                "/api/v1/desktop-execution/internal/approval",
                json=body,
                headers={"x-lightbulb-ws-internal": "internal"},
            )
            assert response.status_code == 413


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


async def test_public_admission_uses_canonical_run_and_strict_selection(app):
    selection = {"deviceId": "device_1", "expectedConnectionGeneration": "gen_1", "command": "device.info"}
    with (
        patch.object(routes.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook"})),
        patch.object(service, "authorize_run", AsyncMock(return_value=AUTH)) as authorize,
        patch.object(service, "admit_device", AsyncMock(return_value={"admissionToken": "server-capability"})) as admit,
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/desktop-execution/admission", headers=HEADERS, json=selection)
            assert response.status_code == 200
            assert response.json() == {"admissionToken": "server-capability"}
            authorize.assert_awaited_with("user_1", "run_1", "agent_1", "room", active=True)
            admit.assert_awaited_once_with(AUTH, selection)
            for extra in ({"memberId": "other"}, {"approved": True}, {"command": "system.execApprovals.set"}):
                response = await client.post(
                    "/api/v1/desktop-execution/admission", headers=HEADERS, json=selection | extra
                )
                assert response.status_code == 400
            assert admit.await_count == 1


async def test_file_write_public_binary_boundary_and_internal_policy_secret(app):
    import base64
    import hashlib
    from tests.unit.services.test_desktop_resources import Files
    from tests.unit.services.test_desktop_execution import REQUEST

    h = Files()
    data = b"binary\x00" * 20000
    digest = hashlib.sha256(data).hexdigest()
    h.preflight["binding"] = {"kind": "write", "anchorPath": "/approved", "anchorDevice": "1", "anchorInode": "2"}
    h.reply = {"ok": True, "path": "/approved/file.bin", "size": len(data), "sha256": digest, "overwritten": False}
    request = REQUEST | {
        "command": "file.write",
        "params": {"path": "/approved/file.bin", "contentBase64": base64.b64encode(data).decode(), "sha256": digest},
    }
    with (
        patch.object(service, "_operations", h.store),
        patch.object(routes.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook"})),
        patch.object(service, "authorize_run", AsyncMock(return_value=AUTH)),
        patch.object(routes.settings, "WS_INTERNAL_SECRET", "internal"),
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/desktop-execution/operations",
                json=request,
                headers=HEADERS | {"X-Desktop-Admission": h.token()},
            )
            assert response.status_code == 200, response.text
            await h.finish()
            assert (await h.store.get(AUTH, response.json()["id"]))["state"] == "succeeded"
            assert (
                await client.post("/api/v1/desktop-execution/internal/file-policy", json={}, headers=HEADERS)
            ).status_code == 403


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
        # A plain channel message Jev routed to this agent carries the same authority.
        run["eventKind"] = "room_event"
        assert await service.authorize_run("user_1", "run_1", "agent_1", "room", active=True) == AUTH
        run["eventKind"] = "heartbeat"
        with pytest.raises(service.DesktopError, match="run_member_authority_unavailable"):
            await service.authorize_run("user_1", "run_1", "agent_1", "room", active=True)
        run["eventKind"] = "user_request"
        run["status"] = "ended"
        assert await service.authorize_run("user_1", "run_1", "agent_1", "room", active=False) == AUTH
        with pytest.raises(service.DesktopError, match="run_not_active"):
            await service.authorize_run("user_1", "run_1", "agent_1", "room", active=True)


async def test_a_spoken_runs_authority_is_its_verified_requester_and_nobody_else():
    """A voice run (family "call") reaches the requester's own Mac only: the VOICERUN row's
    requesterUserId, None for a Meet speaker or once another speaker steered the run."""
    from core.repositories import messaging_repo

    voice_run = {
        "runId": "run_1",
        "agentId": "agent_1",
        "roomId": "room_1",
        "speakerUserId": "user_1",
        "requesterUserId": "user_1",
    }
    with (
        patch.object(messaging_repo, "get_voice_run", AsyncMock(return_value=voice_run)),
        patch.object(messaging_repo, "get_run", AsyncMock(return_value=None)),
        patch.object(messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "user_1"})),
        patch.object(messaging_repo, "get_member", AsyncMock(return_value={"id": "user_1"})),
        patch.object(messaging_repo, "has_foreign_members", AsyncMock(return_value=False)),
    ):
        assert await service.authorize_run("user_1", "run_1", "agent_1", "call", active=True) == {
            **AUTH,
            "family": "call",
        }
        voice_run["requesterUserId"] = None  # a Meet speaker, or a steer by someone else
        with pytest.raises(service.DesktopError, match="run_member_authority_unavailable"):
            await service.authorize_run("user_1", "run_1", "agent_1", "call", active=True)
        voice_run.pop("requesterUserId")  # a row written before the field existed
        with pytest.raises(service.DesktopError, match="run_member_authority_unavailable"):
            await service.authorize_run("user_1", "run_1", "agent_1", "call", active=True)
        voice_run.update(requesterUserId="user_1", agentId="agent_2")  # another agent's run
        with pytest.raises(service.DesktopError, match="run_forbidden"):
            await service.authorize_run("user_1", "run_1", "agent_1", "call", active=True)


async def test_a_paperclip_run_is_no_longer_a_run_family():
    with pytest.raises(service.DesktopError, match="unsupported_run_family"):
        await service.authorize_run("org_1", "run_1", "agent_1", "paperclip", active=True)
    assert not service.valid_auth(
        {"ownerId": "o", "companyId": "c", "runId": "r", "agentId": "a", "family": "paperclip"}
    )


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


SESSION_RUN = "session-11111111-1111-4111-8111-111111111111"


async def test_a_hook_token_cannot_claim_the_session_family(app):
    session_auth = AUTH | {"runId": SESSION_RUN, "agentId": "session", "family": "session", "roomId": ""}
    with (
        patch.object(routes.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook"})),
        patch.object(routes.settings, "WS_INTERNAL_SECRET", "internal"),
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v1/desktop-execution/devices",
                headers=HEADERS
                | {"X-Desktop-Run": SESSION_RUN, "X-Desktop-Agent": "session", "X-Desktop-Run-Family": "session"},
            )
            assert response.status_code == 403
            response = await client.post(
                "/api/v1/desktop-execution/internal",
                headers={"x-lightbulb-ws-internal": "internal"},
                json={"action": "cancel", "auth": session_auth, "receiptId": "r"},
            )
            assert response.status_code == 403


async def test_only_a_forwarded_mac_lease_carries_a_session_run(app):
    from core.repositories import local_sessions_repo
    from core.services import mac_browser

    session_auth = AUTH | {"runId": SESSION_RUN, "agentId": "session", "family": "session", "roomId": ""}
    row = {"ownerId": "user_1", "humanUserId": "user_1"}
    acquire = AsyncMock(return_value={"status": "starting"})
    with (
        patch.object(routes.settings, "WS_INTERNAL_SECRET", "internal"),
        patch.object(local_sessions_repo, "get_session", AsyncMock(return_value=row)),
        patch.object(mac_browser, "acquire", acquire),
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/desktop-execution/internal",
                headers={"x-lightbulb-ws-internal": "internal"},
                json={"action": "mac_acquire", "auth": session_auth, "request": {"deviceId": "mac1"}},
            )
            assert response.status_code == 200 and response.json() == {"status": "starting"}
            assert acquire.await_args.args[0] == session_auth
            response = await client.post(
                "/api/v1/desktop-execution/internal",
                headers={"x-lightbulb-ws-internal": "internal"},
                json={"action": "status", "auth": session_auth, "receiptId": "r"},
            )
            assert response.status_code == 403


TASK_ID = "11111111-1111-4111-8111-111111111111"
TASK_HEADERS = {
    "Authorization": "Bearer hook",
    "X-Desktop-Owner": "org_1",
    "X-Desktop-Run": "run_1",
    "X-Desktop-Agent": "agent_1",
    "X-Desktop-Run-Family": "task",
    "X-Desktop-Task": TASK_ID,
    "X-Desktop-Run-Secret": "secret",
}
TASK_AUTH = {
    "ownerId": "org_1",
    "memberId": "user_1",
    "runId": "run_1",
    "agentId": "agent_1",
    "family": "task",
    "roomId": TASK_ID,  # the task family's home: its task
}


@pytest.fixture
def task_rows():
    """A Worker run: task, its current attempt and its trigger, with user_1 as the acting human."""
    from core.repositories import tasks_repo
    from core.services import clerk_admin

    rows = {
        "task": {"taskId": TASK_ID, "attemptCount": 1, "ownerUserId": "user_1", "triggerId": "chat:m1"},
        "attempt": {"runId": "run_1", "agentId": "agent_1", "runSecret": "secret", "status": "running", "n": 1},
        "trigger": {"triggerId": "chat:m1", "source": "chat", "actorUserId": "user_1", "ref": {}},
    }

    async def get_task(ws, task_id):
        return dict(rows["task"]) if (ws, task_id) == ("org_1", TASK_ID) else None

    async def get_attempt(ws, task_id, n):
        return dict(rows["attempt"]) if (ws, task_id, n) == ("org_1", TASK_ID, 1) else None

    async def get_trigger(ws, trigger_id):
        return dict(rows["trigger"]) if trigger_id == rows["task"].get("triggerId") else None

    with (
        patch.object(tasks_repo, "get_task", get_task),
        patch.object(tasks_repo, "get_attempt", get_attempt),
        patch.object(tasks_repo, "get_trigger", get_trigger),
        patch.object(clerk_admin, "list_organization_members", AsyncMock(return_value=[{"user_id": "user_1"}])),
    ):
        yield rows


async def test_a_task_run_lists_and_admits_its_requesters_paired_mac(app, task_rows):
    selection = {"deviceId": "device_1", "expectedConnectionGeneration": "gen_1", "command": "system.run"}
    with (
        patch.object(routes.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": "hook"})),
        patch.object(service, "discover_devices", AsyncMock(return_value={"devices": []})) as discover,
        patch.object(service, "admit_device", AsyncMock(return_value={"admissionToken": "t"})) as admit,
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/v1/desktop-execution/devices", headers=TASK_HEADERS)).status_code == 200
            discover.assert_awaited_once_with(TASK_AUTH)
            response = await client.post("/api/v1/desktop-execution/admission", headers=TASK_HEADERS, json=selection)
            assert response.status_code == 200
            admit.assert_awaited_once_with(TASK_AUTH, selection)
            assert service.valid_auth(TASK_AUTH)
            response = await client.get(
                "/api/v1/desktop-execution/devices", headers=TASK_HEADERS | {"X-Desktop-Run-Secret": "wrong"}
            )
            assert response.status_code == 403 and response.json()["detail"]["code"] == "run_forbidden"


async def test_the_backend_rechecks_a_task_auth_without_its_secret(task_rows):
    assert await service.authorize_run("org_1", "run_1", "agent_1", "task", active=True, task_id=TASK_ID) == TASK_AUTH
    await service._reauthorize(TASK_AUTH)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"attempt": {"status": "ended"}}, "run_not_active"),
        ({"attempt": {"runId": "run_2"}}, "run_forbidden"),
        ({"attempt": {"agentId": "agent_2"}}, "run_forbidden"),
        ({"attempt": {"runSecret": "other"}}, "run_forbidden"),
        ({"task": {"attemptCount": 2}}, "run_forbidden"),  # a later attempt owns the task now
        ({"trigger": {"actorUserId": None}}, "run_member_authority_unavailable"),  # no acting human
        ({"task": {"ownerUserId": "org_1"}, "trigger": {"actorUserId": "org_1"}}, "run_member_authority_unavailable"),
        ({"task": {"ownerUserId": "user_2"}, "trigger": {"actorUserId": "user_2"}}, "run_forbidden"),  # not a member
        ({"trigger": {"ref": {"origin": "phone_contact"}}}, "run_member_authority_unavailable"),
    ],
)
async def test_a_task_run_without_a_live_attempt_or_acting_member_gets_no_mac(task_rows, change, code):
    for row, fields in change.items():
        task_rows[row].update(fields)
    with pytest.raises(service.DesktopError) as err:
        await service.authorize_run(
            "org_1", "run_1", "agent_1", "task", active=True, task_id=TASK_ID, run_secret="secret"
        )
    assert err.value.code == code


async def test_a_task_run_with_no_task_named_is_refused(task_rows):
    for task_id in ("", "../x"):
        with pytest.raises(service.DesktopError, match="run_forbidden"):
            await service.authorize_run("org_1", "run_1", "agent_1", "task", active=True, task_id=task_id)


@pytest.mark.parametrize(
    "source,ref",
    [
        ("chat", {}),
        ("manual", {}),  # Run now: the person who pressed it
        ("a2a", {}),  # a member's own A2A ask (membership is checked below it)
        ("voice", {"origin": "owner_voice"}),  # #5010 owner mode: a seated member asked aloud in their DM
    ],
)
async def test_a_task_a_person_asked_for_reaches_their_mac(task_rows, source, ref):
    task_rows["trigger"].update({"source": source, "ref": ref})
    auth = await service.authorize_run(
        "org_1", "run_1", "agent_1", "task", active=True, task_id=TASK_ID, run_secret="secret"
    )
    assert auth == TASK_AUTH


@pytest.mark.parametrize(
    "source,ref",
    [
        ("schedule", {}),
        ("webhook", {}),
        ("mail", {}),
        ("text", {}),  # SMS and iMessage
        ("idea", {}),
        ("watch", {}),
        ("task_result", {}),
        ("job_result", {}),
        ("network", {}),
        ("answer", {}),
        ("ask", {}),
        ("voice", {}),  # a Meet speaker: no owner-mode origin
        ("voice", {"origin": "phone_contact"}),
        ("voice", {"origin": "meet_guest"}),
    ],
)
async def test_a_task_nobody_asked_for_gets_no_mac(task_rows, source, ref):
    # The owner as actor in a personal workspace is exactly how routines and inbound mail look.
    task_rows["trigger"].update({"source": source, "ref": ref})
    with pytest.raises(service.DesktopError) as err:
        await service.authorize_run(
            "org_1", "run_1", "agent_1", "task", active=True, task_id=TASK_ID, run_secret="secret"
        )
    assert err.value.code == "run_member_authority_unavailable"


async def test_a_personal_workspace_routine_fired_as_its_owner_gets_no_mac(task_rows):
    from core.repositories import tasks_repo

    task_rows["task"]["ownerUserId"] = "user_1"
    task_rows["trigger"].update({"source": "schedule", "actorUserId": "user_1"})
    task = {**task_rows["task"]}
    with (
        patch.object(tasks_repo, "get_task", AsyncMock(return_value=task)),
        patch.object(tasks_repo, "get_attempt", AsyncMock(return_value=task_rows["attempt"])),
        pytest.raises(service.DesktopError, match="run_member_authority_unavailable"),
    ):
        await service.authorize_run(
            "user_1", "run_1", "agent_1", "task", active=True, task_id=TASK_ID, run_secret="secret"
        )
