"""Paperclip run identity is not consent; only the paired native prompt is."""

import asyncio
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.services import desktop_execution as ex
from tests.unit.services.test_desktop_execution import REQUEST, TARGET, Harness

AUTH = {"ownerId": "org_1", "companyId": "company_1", "runId": "run_1", "agentId": "agent_1", "family": "paperclip"}


@pytest.fixture
def authority(monkeypatch):
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services import paperclip_admin_client, paperclip_admin_session, clerk_admin
    from core.repositories import container_repo

    owner = SimpleNamespace(user_id="user_1", org_id="org_1", company_id="company_1", status="active")
    run = {"id": "run_1", "agentId": "agent_1", "companyId": "company_1", "status": "running"}
    agent = {"id": "agent_1", "companyId": "company_1"}
    admin = SimpleNamespace(
        _http=None, get_heartbeat_run=AsyncMock(return_value=run), get_agent=AsyncMock(return_value=agent)
    )
    monkeypatch.setattr(paperclip_admin_client, "shared_admin_client", lambda: admin)
    monkeypatch.setattr(paperclip_admin_session, "get_admin_session_cookie", AsyncMock(return_value="private"))
    monkeypatch.setattr(PaperclipRepo, "resolve_owner_row", AsyncMock(return_value=owner))
    get = AsyncMock(return_value=owner)
    monkeypatch.setattr(PaperclipRepo, "get", get)
    members = [{"user_id": "user_1"}]
    monkeypatch.setattr(clerk_admin, "list_organization_members", AsyncMock(return_value=members))
    pairs = {
        "device_1": {"userId": "user_1", "clientId": "node-host", "pairingGroupId": "pair_1"},
        "native": {"userId": "user_1", "clientId": "openclaw-macos", "pairingGroupId": "pair_1"},
    }
    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value={"paired_devices": pairs}))
    return SimpleNamespace(owner=owner, run=run, agent=agent, pairs=pairs, members=members, get=get)


@pytest.mark.parametrize(
    "context",
    [
        {},
        {"responsibleUserId": "bot"},
        {"requestedByActorType": "user", "contextSnapshot": {"actorId": "user_other"}},
        {"invocationSource": "timer", "contextSnapshot": {"retry": True, "coalesced": True}},
    ],
)
async def test_run_authorization_never_invents_member_from_actor_context(authority, context):
    authority.run.update(context)
    auth = await ex.authorize_run("org_1", "run_1", "agent_1", "paperclip", active=True)
    assert auth == AUTH
    assert "memberId" not in auth


@pytest.mark.parametrize(
    "field,value", [("status", "succeeded"), ("companyId", "foreign"), ("agentId", "foreign"), ("id", "foreign")]
)
async def test_wrong_or_completed_run_is_denied(authority, field, value):
    authority.run[field] = value
    with pytest.raises(ex.DesktopError):
        await ex.authorize_run("org_1", "run_1", "agent_1", "paperclip", active=True)


async def test_selected_native_pair_derives_current_member(authority, monkeypatch):
    from core.services import connection_service

    resolve = AsyncMock(return_value=TARGET | {"ownerId": "org_1"})
    monkeypatch.setattr(connection_service, "resolve_desktop_target", resolve)
    await ex._resolve(AUTH, REQUEST)
    assert resolve.call_args.args[:4] == ("org_1", "user_1", "device_1", "gen_1")
    assert authority.get.call_args.kwargs == {"consistent_read": True}


@pytest.mark.parametrize("bad", ["member", "pair", "clerk", "company", "inactive", "missing"])
async def test_selected_member_denials(authority, monkeypatch, bad):
    from core.services import connection_service

    resolve = AsyncMock(return_value=TARGET)
    monkeypatch.setattr(connection_service, "resolve_desktop_target", resolve)
    if bad == "member":
        authority.pairs["native"]["userId"] = "user_other"
    elif bad == "pair":
        authority.pairs["native"]["pairingGroupId"] = "stale"
    elif bad == "clerk":
        authority.members.clear()
    elif bad == "company":
        authority.owner.company_id = "foreign"
    elif bad == "inactive":
        authority.owner.status = "deleted"
    else:
        authority.get.return_value = None
    with pytest.raises(ex.DesktopError):
        await ex._resolve(AUTH, REQUEST)
    assert not resolve.called


class ConsentHarness(Harness):
    def __init__(self):
        super().__init__()
        self.target["ownerId"] = "org_1"
        self.native = self.target | {"deviceId": "native", "generation": "native-gen"}
        self.store.companion = self.companion
        self.prompt = asyncio.Event()
        self.decision = True
        self.binding_mismatch = False

    async def companion(self, auth, target, command=ex.APPROVAL_COMMAND):
        if self.native is None:
            raise ex.DesktopError("desktop_generation_lost")
        return deepcopy(self.native)

    def token(self):
        return self.store.admit(AUTH, self.target)

    async def dispatch(self, target, request, invoke_id):
        self.effects.append((request, invoke_id))
        if request["command"] == "desktop.operation.approve.v1":
            assert any(
                r.get("pendingInvoke") and r["pendingInvoke"][1] == invoke_id for r in self.store.records.values()
            )
            self.prompt.set()
            await self.answer.wait()
            return {
                "ok": True,
                "payload": {
                    "approved": self.decision,
                    "binding": "bad" if self.binding_mismatch else request["params"]["binding"],
                },
            }
        if request["command"] == "system.run.prepare":
            return {
                "ok": True,
                "payload": {
                    "plan": {"argv": ["echo", "hello"], "cwd": "/tmp", "agentId": "agent_1", "sessionKey": "run_1"},
                    "execPolicy": {"security": "allowlist", "ask": "always"},
                },
            }
        if request["command"] == ex.APPROVAL_COMMAND:
            return {"ok": True, "payload": {"approved": self.decision, "binding": request["params"]["binding"]}}
        return {"ok": True, "payload": {"name": "Mac"}}


async def test_native_consent_broker_accepts_wrapped_maximum_request_but_keeps_command_bounds():
    from core.services.node_broker import NodeCapacity, NodeSession

    h = ConsentHarness()
    request = ex.validate_request(
        REQUEST
        | {
            "command": "system.run",
            "params": {"argv": ["echo", "a" * 4096, "b" * 4096, "c" * 4096, "d" * 3700], "cwd": "/tmp"},
        }
    )
    assert 16_000 < len(ex._json(request)) <= 16_384
    await h.store.submit(AUTH, h.token(), request)
    await h.finish()
    params = h.effects[0][0]["params"]
    assert 16_384 < len(ex._json(params)) < 32_768
    session = NodeSession("native", {ex.CONSENT_COMMAND, "device.info", ex.APPROVAL_COMMAND})
    invoke = asyncio.create_task(session.invoke(ex.CONSENT_COMMAND, params, 5000, invoke_id="consent"))
    outgoing = asyncio.create_task(session.send.get())
    try:
        done, _ = await asyncio.wait({invoke, outgoing}, timeout=1, return_when=asyncio.FIRST_COMPLETED)
        if invoke in done:
            await invoke  # expose pre-enqueue rejection instead of hiding it behind a receive timeout
        frame = await asyncio.wait_for(outgoing, 1)
        assert frame["payload"]["command"] == ex.CONSENT_COMMAND
        session.resolve(
            {
                "type": "req",
                "id": "response",
                "method": "node.invoke.result",
                "params": {
                    "id": "consent",
                    "nodeId": "native",
                    "ok": True,
                    "payload": {"approved": False, "binding": params["binding"]},
                },
            }
        )
        assert not (await invoke)["payload"]["approved"]
    finally:
        invoke.cancel()
        outgoing.cancel()
        await asyncio.gather(invoke, outgoing, return_exceptions=True)
    for command, oversized in (
        (ex.CONSENT_COMMAND, {"padding": "x" * 32_768}),
        ("device.info", params),
        (ex.APPROVAL_COMMAND, params),
    ):
        with pytest.raises(NodeCapacity):
            await session.invoke(command, oversized, 100)
    assert not session.pending and session.send.empty()


@pytest.mark.parametrize("bad", ["cli_revoke", "native_revoke", "pair", "membership", "run"])
async def test_second_shell_prompt_is_cancelled_when_authority_is_revoked(bad):
    class ShellWait(ConsentHarness):
        def __init__(self):
            super().__init__()
            self.shell_prompt = asyncio.Event()
            self.invalid = False

        async def authorize(self, auth):
            if self.invalid:
                raise ex.DesktopError("run_not_active")

        async def dispatch(self, target, request, invoke_id):
            if request["command"] == ex.APPROVAL_COMMAND:
                self.effects.append((request, invoke_id))
                self.shell_prompt.set()
                await asyncio.Event().wait()  # no human response; invalidation must dismiss it
            return await super().dispatch(target, request, invoke_id)

    h = ShellWait()
    h.answer.set()
    receipt = await h.store.submit(
        AUTH, h.token(), REQUEST | {"command": "system.run", "params": {"argv": ["echo", "hello"], "cwd": "/tmp"}}
    )
    await asyncio.wait_for(h.shell_prompt.wait(), 1)
    assert h.store.records[receipt["id"]]["state"] == "pending_approval"
    if bad == "cli_revoke":
        h.target = None
    elif bad == "native_revoke":
        h.native = None
    elif bad == "pair":
        h.target = h.target | {"pairingGroupId": "changed"}
    elif bad == "membership":

        async def removed_member(*_args):
            raise ex.DesktopError("desktop_forbidden")

        h.store.resolve = removed_member
    else:
        h.invalid = True
    await asyncio.wait_for(asyncio.gather(*h.store.tasks.values()), 3)
    assert h.store.records[receipt["id"]]["state"] == "rejected"
    assert h.cancels == [h.effects[-1][1]]
    assert h.cancels[0].endswith("-approval")
    assert [r["command"] for r, _ in h.effects] == [ex.CONSENT_COMMAND, "system.run.prepare", ex.APPROVAL_COMMAND]


async def test_paperclip_exact_consent_dispatches_once_and_retry_cannot_inherit():
    h = ConsentHarness()
    receipt = await h.store.submit(AUTH, h.token(), REQUEST)
    await asyncio.wait_for(h.prompt.wait(), 1)
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "pending_approval"
    assert len(h.effects) == 1
    params = h.effects[0][0]["params"]
    assert params["company"] == "company_1"
    assert params["operation"]["command"] == "device.info"
    assert params["nativeDevice"] == "native"
    assert "memberId" not in repr(receipt)
    await h.finish()
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "succeeded"
    await h.store.submit(AUTH, h.token(), REQUEST)
    assert [r["command"] for r, _ in h.effects] == ["desktop.operation.approve.v1", "device.info"]
    await h.store.submit(AUTH, h.token(), REQUEST | {"requestId": "00000000-0000-4000-8000-000000000001"})
    await h.finish()
    assert len(h.effects) == 4  # a fresh request prompts again


@pytest.mark.parametrize(
    "bad", ["deny", "digest", "expired", "revoke", "native_disconnect", "pair", "member", "cancel"]
)
async def test_invalid_consent_never_dispatches_effect(bad):
    h = ConsentHarness()
    receipt = await h.store.submit(AUTH, h.token(), REQUEST)
    await asyncio.wait_for(h.prompt.wait(), 1)
    if bad == "deny":
        h.decision = False
    elif bad == "digest":
        h.binding_mismatch = True
    elif bad == "expired":
        h.now += 121
    elif bad == "revoke":
        h.target = None
    elif bad == "native_disconnect":
        h.native = None
    elif bad in {"pair", "member"}:
        h.target = h.target | {"pairingGroupId" if bad == "pair" else "memberId": "changed"}
    else:
        await h.store.cancel(AUTH, receipt["id"])
        assert h.cancels == [h.effects[0][1]]
    await h.finish()
    assert [r["command"] for r, _ in h.effects] == ["desktop.operation.approve.v1"]


async def test_paperclip_cancel_before_submit_never_prompts():
    h = ConsentHarness()
    token = h.token()
    await h.store.cancel(AUTH, token + "." + REQUEST["requestId"])
    receipt = await h.store.submit(AUTH, token, REQUEST)
    assert receipt["state"] == "rejected"
    assert not h.effects


async def test_revocation_dismisses_pending_consent_without_human_answer():
    h = ConsentHarness()
    receipt = await h.store.submit(AUTH, h.token(), REQUEST)
    await asyncio.wait_for(h.prompt.wait(), 1)
    h.target = None
    await asyncio.wait_for(asyncio.gather(*h.store.tasks.values()), 3)
    assert h.cancels == [h.effects[0][1]]
    assert h.store.records[receipt["id"]]["state"] == "rejected"


async def test_duplicate_paperclip_request_cannot_move_to_another_generation():
    h = ConsentHarness()
    await h.store.submit(AUTH, h.token(), REQUEST)
    await h.finish()
    h.target = h.target | {"generation": "new"}
    with pytest.raises(ex.DesktopError, match="request_conflict"):
        await h.store.submit(AUTH, h.token(), REQUEST | {"expectedConnectionGeneration": "new"})
    assert len(h.effects) == 2


@pytest.fixture
def connected(authority, monkeypatch):
    from core.services import connection_service as cs

    service = object.__new__(cs.ConnectionService)
    service.table_name = "connections"
    rows = {}
    for device, pair in authority.pairs.items():
        pair["commands"] = ["device.info", "desktop.operation.approve.v1"] if device == "native" else ["system.run"]
        values = {
            "ownerId": "org_1",
            "userId": "user_1",
            "deviceId": device,
            "generation": "native-gen" if device == "native" else "gen_1",
            "clientId": pair["clientId"],
            "taskAddress": "http://worker",
        }
        rows[service.desktop_ready_id("org_1", device)] = {k: {"S": v} for k, v in values.items()} | {
            "ttl": {"N": str(int(time.time()) + 120)},
            "commands": {"L": [{"S": c} for c in pair["commands"]]},
        }
    service._client = SimpleNamespace(
        get_item=lambda **kw: {"Item": deepcopy(rows.get(kw["Key"]["connectionId"]["S"], {}))}
    )
    service.query_by_owner = AsyncMock(return_value=[(key, "user_1", "desktop-ready") for key in rows])
    monkeypatch.setattr(cs, "ConnectionService", lambda: service)
    monkeypatch.setattr(cs, "_get_singleton", lambda: service)
    return rows


async def test_real_reciprocal_native_selection_and_discovery_omit_capabilities(authority, connected):
    result = await ex.discover_devices(AUTH)
    assert len(result["devices"]) == 2
    assert all(set(d) == {"deviceId", "generation", "commands"} for d in result["devices"])
    target = await ex._resolve(AUTH, REQUEST | {"deviceId": "native", "expectedConnectionGeneration": "native-gen"})
    native = await ex._resolve_companion(AUTH, target, ex.CONSENT_COMMAND)
    assert native["memberId"] == "user_1"
    assert native["deviceId"] == "native"
    assert native["pairedCliId"] == "device_1" and native["pairedCliGeneration"] == "gen_1"


@pytest.mark.parametrize("bad", ["stale", "wrong_member", "foreign_owner", "native_revoked", "cli_disconnected"])
async def test_real_resolution_denies_stale_or_foreign_connections(authority, connected, bad):
    from core.services.connection_service import ConnectionService

    rows = list(connected.values())
    native = next(r for r in rows if r["deviceId"]["S"] == "native")
    if bad == "stale":
        native["generation"]["S"] = "changed"
    elif bad == "wrong_member":
        native["userId"]["S"] = "other"
    elif bad == "foreign_owner":
        native["ownerId"]["S"] = "org_other"
    elif bad == "native_revoked":
        authority.pairs.pop("native")
    else:
        connected.pop(ConnectionService().desktop_ready_id("org_1", "device_1"))
    with pytest.raises(ex.DesktopError):
        target = await ex._resolve(AUTH, REQUEST | {"deviceId": "native", "expectedConnectionGeneration": "native-gen"})
        await ex._resolve_companion(AUTH, target, ex.CONSENT_COMMAND)


async def test_paperclip_native_peer_route_binds_operation_and_cancel(authority, connected, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from core.services.node_broker import NodeSession
    from routers.desktop_execution import router, settings

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/desktop-execution")
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.ASGITransport(app=app), **kw))
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "backend-only")
    monkeypatch.setattr(ex, "operations", lambda: SimpleNamespace(address="http://worker"))
    session = NodeSession("native", {ex.CONSENT_COMMAND})
    session.generation = "native-gen"
    monkeypatch.setattr(ex, "_session", lambda target: session)
    request = REQUEST | {"deviceId": "native", "expectedConnectionGeneration": "native-gen"}
    target = await ex._resolve(AUTH, request)
    native = await ex._resolve_companion(AUTH, target, ex.CONSENT_COMMAND)
    params = {
        "binding": "a" * 64,
        "workspace": "org_1",
        "company": "company_1",
        "agent": "agent_1",
        "run": "run_1",
        "device": "native",
        "nativeDevice": "native",
        "operation": request,
        "expiresAtMs": int((time.time() + 60) * 1000),
    }
    invoke_id = "00000000-0000-4000-8000-000000000001-consent"
    await ex._approval_rpc(native, "cancel", invoke_id)
    assert (await session.send.get())["event"] == "node.invoke.cancel"
    prompt = asyncio.create_task(ex._approval_rpc(native, "invoke", invoke_id, params))
    frame = await asyncio.wait_for(session.send.get(), 2)
    assert frame["payload"]["command"] == ex.CONSENT_COMMAND
    session.resolve(
        {
            "type": "req",
            "id": "response",
            "method": "node.invoke.result",
            "params": {
                "id": invoke_id,
                "nodeId": "native",
                "ok": True,
                "payload": {"approved": False, "binding": "a" * 64},
            },
        }
    )
    assert not (await prompt)["payload"]["approved"]
    for update in (
        {"company": "wrong"},
        {"nativeDevice": "wrong"},
        {"operation": request | {"expectedConnectionGeneration": "stale"}},
    ):
        with pytest.raises(ex.DesktopError):
            await ex._approval_rpc(native, "invoke", invoke_id, params | update)
    async with client_type(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
        response = await client.post("/api/v1/desktop-execution/internal/approval", json={})
    assert response.status_code == 403


async def test_consent_cannot_survive_pair_change_between_child_phases():
    h = ConsentHarness()
    receipt = await h.store.submit(AUTH, h.token(), REQUEST)
    await h.finish()
    row = h.store.records[receipt["id"]]
    h.native["generation"] = "new-native"
    with pytest.raises(ex.DesktopError):
        await h.store._phase(row, h.target, REQUEST, "later-phase")
    assert len(h.effects) == 2


async def test_paperclip_shell_still_requires_its_exact_command_approval():
    h = ConsentHarness()
    receipt = await h.store.submit(
        AUTH, h.token(), REQUEST | {"command": "system.run", "params": {"argv": ["echo", "hello"], "cwd": "/tmp"}}
    )
    await h.finish()
    assert h.store.records[receipt["id"]]["state"] == "succeeded"
    assert [r["command"] for r, _ in h.effects] == [
        "desktop.operation.approve.v1",
        "system.run.prepare",
        "system.run.approve.v1",
        "system.run",
    ]
    assert h.effects[-1][0]["params"]["approvalDecision"] == "allow-once"


async def test_paperclip_file_consent_cannot_bypass_folder_policy():
    h = ConsentHarness()
    h.store.file_policy = AsyncMock(return_value={"readRoots": ["/allowed"], "writeRoots": [], "denyRoots": []})
    receipt = await h.store.submit(
        AUTH, h.token(), REQUEST | {"command": "file.fetch", "params": {"path": "/private/file"}}
    )
    await h.finish()
    assert h.store.records[receipt["id"]]["error"]["code"] == "file_path_denied"
    assert [r["command"] for r, _ in h.effects] == ["desktop.operation.approve.v1"]


async def test_binary_consent_summary_is_bounded_and_exact():
    import base64
    import hashlib

    h = ConsentHarness()
    binary = b"a" * (512 * 1024)
    encoded = base64.b64encode(binary).decode()
    await h.store.submit(
        AUTH,
        h.token(),
        REQUEST
        | {
            "command": "file.write",
            "params": {"path": "/allowed/file", "contentBase64": encoded, "sha256": hashlib.sha256(binary).hexdigest()},
        },
    )
    await h.finish()
    params = h.effects[0][0]["params"]
    assert len(ex._json(params)) < 2048
    assert params["operation"]["params"]["contentBase64"] == {
        "base64Bytes": len(encoded),
        "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
    }
