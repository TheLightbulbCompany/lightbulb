import asyncio
from copy import deepcopy

import pytest

from core.services import desktop_execution as ex
from tests.unit.services.test_desktop_execution import AUTH, REQUEST, TARGET, Harness

SHELL = REQUEST | {"command": "system.run", "params": {"argv": ["/bin/echo", "hello"], "cwd": "/tmp", "env": {}}}


class ShellHarness(Harness):
    def __init__(self):
        super().__init__()
        self.native = TARGET | {"deviceId": "native", "generation": "native-gen"}
        self.store.companion = self.companion
        self.decision = True
        self.prompt = asyncio.Event()
        self.release = asyncio.Event()
        self.plan = {
            "argv": ["/bin/echo", "hello"],
            "cwd": "/private/tmp",
            "agentId": AUTH["agentId"],
            "sessionKey": AUTH["runId"],
        }

    async def companion(self, auth, target):
        if self.native is None:
            raise ex.DesktopError("shell_approval_unavailable")
        return deepcopy(self.native)

    async def dispatch(self, target, request, invoke_id):
        self.effects.append((request, invoke_id))
        if request["command"] == "system.run.prepare":
            return {
                "ok": True,
                "payload": {"plan": deepcopy(self.plan), "execPolicy": {"security": "allowlist", "ask": "always"}},
            }
        if request["command"] == ex.APPROVAL_COMMAND:
            self.prompt.set()
            await self.release.wait()
            return {"ok": True, "payload": {"approved": self.decision, "binding": request["params"]["binding"]}}
        return {"ok": True, "payload": {"exitCode": 0}}

    async def start(self):
        receipt = await self.store.submit(AUTH, self.token(), SHELL)
        await self.prompt.wait()
        return receipt

    async def complete(self):
        self.release.set()
        await asyncio.gather(*self.store.tasks.values())


@pytest.mark.parametrize(
    "extra", [{"approved": True}, {"approvalDecision": "allow-once"}, {"security": "full"}, {"systemRunPlan": {}}]
)
def test_shell_rejects_model_approval_and_policy(extra):
    with pytest.raises(ex.DesktopError):
        ex.validate_request(SHELL | {"params": SHELL["params"] | extra})


@pytest.mark.parametrize(
    "params",
    [
        {"argv": [], "cwd": "/tmp"},
        {"argv": ["echo\0bad"], "cwd": "/tmp"},
        {"argv": ["echo"], "cwd": "relative"},
        {"argv": ["echo"], "cwd": "/tmp", "env": {"A=B": "x"}},
        {"argv": ["echo"], "cwd": "/tmp", "env": {"A": "x" * 1025}},
    ],
)
def test_shell_parameter_bounds(params):
    with pytest.raises(ex.DesktopError):
        ex.validate_request(SHELL | {"params": params})


def test_native_parameters_are_typed_and_notification_text_is_literal():
    text = '"; do shell script "anything"\n'
    request = ex.validate_request(REQUEST | {"command": "system.notify", "params": {"body": text}})
    assert request["params"]["body"] == text
    for update in (
        {"command": []},
        {"command": "screen.snapshot", "params": {"format": []}},
        {"command": "system.notify", "params": {"body": "x", "approved": True}},
    ):
        with pytest.raises(ex.DesktopError):
            ex.validate_request(REQUEST | update)


async def test_approved_plan_is_unchanged_and_consumed_once():
    h = ShellHarness()
    receipt = await h.start()
    await h.complete()
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "succeeded"
    runs = [r for r, _ in h.effects if r["command"] == "system.run"]
    assert len(runs) == 1
    assert runs[0]["params"]["systemRunPlan"] == h.plan
    assert runs[0]["params"]["approvalDecision"] == "allow-once"
    await h.store.submit(AUTH, h.token(), SHELL)
    assert len(h.effects) == 3


@pytest.mark.parametrize("scenario", ["deny", "expired", "revoke", "native_lost", "cancel"])
async def test_invalidated_approval_never_executes(scenario):
    h = ShellHarness()
    receipt = await h.start()
    if scenario == "deny":
        h.decision = False
    elif scenario == "expired":
        h.now += 121
    elif scenario == "revoke":
        h.target = None
    elif scenario == "native_lost":
        h.native = None
    elif scenario == "cancel":
        await h.store.cancel(AUTH, receipt["id"])
        assert h.cancels == [h.effects[-1][1]]
    await h.complete()
    assert not [r for r, _ in h.effects if r["command"] == "system.run"]


async def test_changed_prepared_input_is_denied_before_prompt_or_execution():
    h = ShellHarness()
    h.plan["argv"] = ["/bin/echo", "changed"]
    receipt = await h.store.submit(AUTH, h.token(), SHELL)
    await asyncio.gather(*h.store.tasks.values())
    assert (await h.store.get(AUTH, receipt["id"]))["error"]["code"] == "shell_prepare_mismatch"
    assert [r["command"] for r, _ in h.effects] == ["system.run.prepare"]


async def test_no_native_companion_and_cancel_before_dispatch_execute_nothing():
    h = ShellHarness()
    h.native = None
    receipt = await h.store.submit(AUTH, h.token(), SHELL)
    await asyncio.gather(*h.store.tasks.values())
    assert not h.effects
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "rejected"

    h = ShellHarness()
    receipt = await h.store.submit(AUTH, h.token(), SHELL)
    await h.store.cancel(AUTH, receipt["id"])
    await asyncio.gather(*h.store.tasks.values())
    assert not h.effects


async def test_cross_worker_approval_auth_exact_target_and_cancel(monkeypatch):
    import time
    from types import SimpleNamespace
    import httpx
    from fastapi import FastAPI
    from core.services.node_broker import NodeSession
    from routers.desktop_execution import router, settings

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/desktop-execution")
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.ASGITransport(app=app), **kw))
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "trusted-backend-secret")
    native = TARGET | {
        "deviceId": "native",
        "generation": "ng",
        "taskAddress": "http://other-worker",
        "approvalAuth": AUTH,
        "cliDeviceId": TARGET["deviceId"],
        "cliGeneration": TARGET["generation"],
    }
    session = NodeSession("native", {ex.APPROVAL_COMMAND})
    session.generation = "ng"
    monkeypatch.setattr(ex, "operations", lambda: SimpleNamespace(address="http://other-worker"))
    monkeypatch.setattr(ex, "_session", lambda target: session)

    async def authorize(auth):
        assert auth == AUTH

    async def resolve(auth, request):
        assert request["deviceId"] == TARGET["deviceId"]
        return TARGET

    async def companion(auth, target):
        return native

    monkeypatch.setattr(ex, "_reauthorize", authorize)
    monkeypatch.setattr(ex, "_resolve", resolve)
    monkeypatch.setattr(ex, "_resolve_companion", companion)
    params = {
        "binding": "a" * 64,
        "workspace": AUTH["ownerId"],
        "agent": AUTH["agentId"],
        "device": TARGET["deviceId"],
        "run": AUTH["runId"],
        "argv": ["/bin/true"],
        "cwd": "/tmp",
        "env": {},
        "expiresAtMs": int((time.time() + 60) * 1000),
    }
    invoke_id = "00000000-0000-4000-8000-000000000001-approval"
    await ex._approval_rpc(native, "cancel", invoke_id)
    assert (await session.send.get())["payload"] == {"invokeId": invoke_id, "nodeId": "native"}
    task = asyncio.create_task(ex._approval_rpc(native, "invoke", invoke_id, params))
    frame = await asyncio.wait_for(session.send.get(), 2)
    assert frame["payload"]["id"] == invoke_id
    assert frame["payload"]["nodeId"] == "native"
    await ex._approval_rpc(native, "cancel", invoke_id)
    assert (await session.send.get())["payload"] == {"invokeId": invoke_id, "nodeId": "native"}
    session.resolve(
        {
            "type": "req",
            "id": "response",
            "method": "node.invoke.result",
            "params": {
                "id": invoke_id,
                "nodeId": "native",
                "ok": True,
                "payload": {"approved": False, "binding": params["binding"]},
            },
        }
    )
    assert (await task)["payload"]["approved"] is False
    async with client_type(transport=httpx.ASGITransport(app=app), base_url="http://other-worker") as client:
        response = await client.post("/api/v1/desktop-execution/internal/approval", json={})
        assert response.status_code == 403
    with pytest.raises(ex.DesktopError):
        await ex._approval_rpc(native | {"generation": "stale"}, "invoke", invoke_id, params)
