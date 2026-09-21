import asyncio
from copy import deepcopy

import pytest

from core.services import desktop_execution as execution


AUTH = {
    "ownerId": "user_1",
    "memberId": "user_1",
    "runId": "run_1",
    "agentId": "agent_1",
    "family": "room",
    "roomId": "room_1",
}
TARGET = {
    "ownerId": "user_1",
    "memberId": "user_1",
    "deviceId": "device_1",
    "generation": "gen_1",
    "taskAddress": "http://10.0.0.1:8000",
    "commands": ["device.info", "device.health"],
    "pairingGroupId": "pair_1",
}
REQUEST = {
    "requestId": "a67c56bf-48d9-4608-813c-ac86df8f2d70",
    "runId": "run_1",
    "agentId": "agent_1",
    "deviceId": "device_1",
    "expectedConnectionGeneration": "gen_1",
    "command": "device.info",
    "params": {},
    "executionDeadlineMs": 1000,
}


class Harness:
    def __init__(self):
        self.now = 1000.0
        self.effects = []
        self.cancels = []
        self.answer = asyncio.Event()
        self.target = deepcopy(TARGET)
        self.result = {"ok": True, "payload": {"name": "Mac"}}
        self.store = execution.DesktopOperations(
            secret="backend-secret",
            address=TARGET["taskAddress"],
            resolve=self.resolve,
            dispatch=self.dispatch,
            cancel=self.cancel,
            authorize=self.authorize,
            clock=lambda: self.now,
        )

    async def authorize(self, auth):
        return auth

    async def resolve(self, auth, request):
        if self.target is None:
            raise execution.DesktopError("desktop_generation_stale", 409)
        return self.target

    async def dispatch(self, target, request, invoke_id):
        self.effects.append((request, invoke_id))
        await self.answer.wait()
        return self.result

    async def cancel(self, target, invoke_id):
        self.cancels.append(invoke_id)

    def token(self):
        return self.store.admit(AUTH, self.target)

    async def finish(self):
        self.answer.set()
        await asyncio.gather(*self.store.tasks.values())


def test_digest_and_strict_command_validation():
    assert execution.request_digest({"a": 1, "b": 2}) == execution.request_digest({"b": 2, "a": 1})
    for update in (
        {"params": {"approved": True}},
        {"command": "system.execApprovals.set"},
        {"command": "browser.proxy", "params": {"url": "http://localhost"}},
        {"executionDeadlineMs": 180001},
        {"memberId": "other"},
    ):
        with pytest.raises(execution.DesktopError):
            execution.validate_request(REQUEST | update)


async def test_duplicate_and_changed_input_never_repeat_effect():
    h = Harness()
    token = h.token()
    first = await h.store.submit(AUTH, token, REQUEST)
    assert first["state"] == "queued"
    second = await h.store.submit(AUTH, token, REQUEST)
    assert second["id"] == first["id"]
    with pytest.raises(execution.DesktopError, match="request_conflict"):
        await h.store.submit(AUTH, token, REQUEST | {"command": "device.health"})
    await h.finish()
    assert len(h.effects) == 1
    assert (await h.store.get(AUTH, first["id"]))["result"] == {"name": "Mac"}
    h.store.records[first["id"]].pop("result")
    assert (await h.store.submit(AUTH, token, REQUEST))["id"] == first["id"]
    assert len(h.effects) == 1


async def test_stop_before_submission_reserves_cancelled_identity():
    h = Harness()
    token = h.token()
    receipt_id = token + "." + REQUEST["requestId"]
    cancelled = await h.store.cancel(AUTH, receipt_id)
    assert cancelled["state"] == "rejected"
    for admission in (token, h.token()):
        result = await h.store.submit(AUTH, admission, REQUEST)
        assert result["error"]["code"] == "cancelled_before_dispatch"
    assert not h.store.tasks and not h.effects


async def test_expiry_process_loss_and_forgery_cannot_dispatch():
    h = Harness()
    token = h.token()
    for auth in (AUTH | {"ownerId": "user_2"}, AUTH | {"memberId": "user_2"}, AUTH | {"runId": "other"}):
        with pytest.raises(execution.DesktopError, match="forbidden"):
            await h.store.submit(auth, token, REQUEST)
    with pytest.raises(execution.DesktopError, match="invalid_admission"):
        await h.store.submit(AUTH, token[:-5] + "xxxxx", REQUEST)
    h.now += 66
    assert (await h.store.submit(AUTH, token, REQUEST))["error"]["code"] == "admission_expired"
    h.now = 1000
    h.store.epoch = "replacement-worker"
    assert (await h.store.submit(AUTH, token, REQUEST))["state"] == "unknown"
    assert not h.effects


async def test_capacity_rejected_before_dispatch_and_cancel_latches_before_start():
    h = Harness()
    token = h.token()
    receipts = []
    for n in range(8):
        request = REQUEST | {"requestId": f"00000000-0000-4000-8000-{n:012d}"}
        receipts.append(await h.store.submit(AUTH, token, request))
    with pytest.raises(execution.DesktopError, match="desktop_capacity"):
        await h.store.submit(AUTH, token, REQUEST)
    cancelled = await h.store.cancel(AUTH, receipts[0]["id"])
    assert cancelled["state"] == "cancel_requested"
    await h.finish()
    assert len(h.effects) == 7
    assert (await h.store.get(AUTH, receipts[0]["id"]))["error"]["code"] == "cancelled_before_dispatch"


async def test_generation_loss_cancel_and_late_completion_are_honest():
    h = Harness()
    receipt = await h.store.submit(AUTH, h.token(), REQUEST)
    await asyncio.sleep(0)
    assert len(h.effects) == 1
    assert (await h.store.cancel(AUTH, receipt["id"]))["state"] == "cancel_requested"
    assert len(h.cancels) == 1
    h.target = None
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "unknown"
    await h.finish()
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "unknown"


async def test_expired_body_retains_identity_and_timeout_is_not_termination():
    h = Harness()
    token = h.token()
    receipt = await h.store.submit(AUTH, token, REQUEST)
    await asyncio.sleep(0)
    h.now += 12
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "unknown"
    await h.finish()
    assert (await h.store.submit(AUTH, token, REQUEST))["id"] == receipt["id"]
    assert len(h.effects) == 1


async def test_gate_off_keeps_receipts_and_cancel_accessible():
    h = Harness()
    token = h.token()
    receipt = await h.store.submit(AUTH, token, REQUEST)
    h.store.enabled = lambda owner: False
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "queued"
    assert (await h.store.submit(AUTH, token, REQUEST))["id"] == receipt["id"]
    assert (await h.store.cancel(AUTH, receipt["id"]))["state"] == "cancel_requested"
    with pytest.raises(execution.DesktopError, match="desktop_disabled"):
        await h.store.submit(AUTH, token, REQUEST | {"requestId": "00000000-0000-4000-8000-000000000001"})
    await h.finish()


async def test_oversize_result_does_not_keep_body_or_replay():
    h = Harness()
    h.result = {"ok": True, "payload": "x" * (1024 * 1024 + 1)}
    token = h.token()
    receipt = await h.store.submit(AUTH, token, REQUEST)
    await h.finish()
    status = await h.store.get(AUTH, receipt["id"])
    assert "result" not in status
    assert status["error"]["code"] == "result_too_large"
    assert (await h.store.submit(AUTH, token, REQUEST))["id"] == receipt["id"]
    assert len(h.effects) == 1


async def test_cancel_status_after_admission_expiry_and_retained_identity_limit():
    h = Harness()
    token = h.token()
    request = REQUEST | {"executionDeadlineMs": 180000}
    receipt = await h.store.submit(AUTH, token, request)
    await asyncio.sleep(0)
    h.now += 70
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "running"
    assert (await h.store.cancel(AUTH, receipt["id"]))["state"] == "cancel_requested"
    await h.finish()
    h.now = 1000
    for n in range(127):
        await h.store.submit(AUTH, token, REQUEST | {"requestId": f"00000000-0000-4000-8000-{n:012d}"})
        await h.finish()
    with pytest.raises(execution.DesktopError, match="desktop_capacity"):
        await h.store.submit(AUTH, token, REQUEST | {"requestId": "00000000-0000-4000-8000-999999999999"})
    assert len(h.effects) == 128
    h.now += 371
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "unknown"
    assert (await h.store.submit(AUTH, token, request))["state"] == "unknown"
    assert len(h.effects) == 128


async def test_body_eviction_keeps_dispatch_tombstones_and_expiry():
    h = Harness()
    h.result = {"ok": True, "payload": "x" * 900000}
    token = h.token()
    first = None
    for n in range(12):
        receipt = await h.store.submit(AUTH, token, REQUEST | {"requestId": f"00000000-0000-4000-8000-{n:012d}"})
        first = first or receipt
        await h.finish()
    assert sum(len(execution._json(r["result"])) for r in h.store.records.values() if "result" in r) <= 8 * 1024 * 1024
    expired = await h.store.get(AUTH, first["id"])
    assert expired["state"] == "unknown"
    assert expired["error"]["code"] == "result_expired"
    retry = REQUEST | {"requestId": "00000000-0000-4000-8000-000000000000"}
    assert (await h.store.submit(AUTH, token, retry))["id"] == first["id"]
    assert len(h.effects) == 12


async def test_same_request_id_with_rediscovered_admission_returns_original_receipt():
    h = Harness()
    first = await h.store.submit(AUTH, h.token(), REQUEST)
    await h.finish()
    second = await h.store.submit(AUTH, h.token(), REQUEST)
    assert second["id"] == first["id"]
    assert len(h.effects) == 1


async def test_real_one_hop_forwarding_lost_response_is_not_replayed(monkeypatch):
    import httpx
    import json
    from core.config import settings

    receiver = Harness()
    sender = Harness()
    sender.store.address = "http://10.0.0.2:8000"
    current = [sender.store]
    monkeypatch.setattr(execution, "operations", lambda: current[0])
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "internal")
    token = receiver.token()
    calls = []

    async def peer(request):
        assert request.headers["x-lightbulb-ws-internal"] == "internal"
        body = json.loads(request.content)
        calls.append(body)
        current[0] = receiver.store
        try:
            receipt = await execution.route_operation(
                "submit", body["auth"], admission_token=body["admissionToken"], request=body["request"], forwarded=True
            )
        finally:
            current[0] = sender.store
        if len(calls) == 1:
            raise httpx.ReadTimeout("lost after admission", request=request)
        return httpx.Response(200, json=receipt)

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(peer), **kwargs)
    )
    first = await execution.submit_desktop(AUTH, token, REQUEST)
    assert first["state"] == "unknown"
    await receiver.finish()
    second = await execution.submit_desktop(AUTH, token, REQUEST)
    assert len(calls) == 2 and len(receiver.effects) == 1
    assert first["id"] == second["id"] and second["state"] == "succeeded"
    assert "10.0.0.1" not in json.dumps(first)
    with pytest.raises(execution.DesktopError, match="desktop_forward_loop"):
        await execution.route_operation("submit", AUTH, admission_token=token, request=REQUEST, forwarded=True)


async def test_revoked_target_during_authorization_never_enters_dispatch():
    h = Harness()
    receipt = await h.store.submit(AUTH, h.token(), REQUEST)
    h.target = None
    await h.finish()
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "rejected"
    assert not h.effects


async def test_late_completion_after_generation_loss_is_unknown_without_a_status_poll():
    h = Harness()
    receipt = await h.store.submit(AUTH, h.token(), REQUEST)
    await asyncio.sleep(0)
    h.target = None
    await h.finish()
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "unknown"


async def test_cancel_while_authority_check_is_waiting_latches_before_dispatch():
    h = Harness()
    receipt = await h.store.submit(AUTH, h.token(), REQUEST)
    waiting = asyncio.Event()

    async def delayed_authority(auth):
        await waiting.wait()

    h.store.authorize = delayed_authority
    await asyncio.sleep(0)
    assert (await h.store.cancel(AUTH, receipt["id"]))["state"] == "cancel_requested"
    waiting.set()
    await h.finish()
    assert not h.effects
    assert (await h.store.get(AUTH, receipt["id"]))["error"]["code"] == "cancelled_before_dispatch"


async def test_actual_broker_dispatch_selects_exact_device_and_generation(monkeypatch):
    from core.services import node_broker
    from routers.gateway_relay import node_sessions

    h = Harness()
    h.store.dispatch = execution._dispatch
    h.store.cancel_dispatch = execution._cancel
    intended = node_broker.NodeSession("device_1", {"device.info"})
    intended.generation = "gen_1"
    other = node_broker.NodeSession("device_2", {"device.info"})
    other.generation = "gen_other"
    monkeypatch.setitem(node_sessions, ("user_1", "device_1"), intended)
    monkeypatch.setitem(node_sessions, ("user_1", "device_2"), other)
    receipt = await h.store.submit(AUTH, h.token(), REQUEST)
    await asyncio.sleep(0)
    frame = await intended.send.get()
    assert frame["payload"]["nodeId"] == "device_1"
    assert frame["payload"]["timeoutMs"] == 1000  # grace never extends the execution request
    assert other.send.empty()
    await h.store.cancel(AUTH, receipt["id"])
    assert (await intended.send.get())["payload"]["invokeId"] == frame["payload"]["id"]
    intended.resolve(
        {
            "type": "req",
            "id": "reply",
            "method": "node.invoke.result",
            "params": {"id": frame["payload"]["id"], "ok": True, "payload": {"name": "Mac"}},
        }
    )
    await h.finish()
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "succeeded"
    intended.generation = "replacement"
    receipt = await h.store.submit(AUTH, h.token(), REQUEST | {"requestId": "00000000-0000-4000-8000-999999999999"})
    await h.finish()
    assert (await h.store.get(AUTH, receipt["id"]))["state"] == "unknown"
    assert intended.send.empty() and other.send.empty()


@pytest.mark.parametrize(
    "stage,expected", [("authorize", "rejected"), ("before_resolve", "rejected"), ("after_resolve", "unknown")]
)
async def test_whole_background_lifecycle_has_one_admission_deadline(monkeypatch, stage, expected):
    monkeypatch.setattr(execution, "RESULT_GRACE_SECONDS", 0)
    h = Harness()
    receipt = await h.store.submit(AUTH, h.token(), REQUEST | {"executionDeadlineMs": 20})
    entered = asyncio.Event()
    blocked = asyncio.Event()
    calls = 0

    async def authorize(auth):
        entered.set()
        await blocked.wait()

    async def resolve(auth, request):
        nonlocal calls
        calls += 1
        if calls == (2 if stage == "after_resolve" else 1):
            entered.set()
            await blocked.wait()
        return h.target

    if stage == "authorize":
        h.store.authorize = authorize
    else:
        h.store.resolve = resolve
    h.answer.set()
    task = h.store.tasks[receipt["id"]]
    try:
        await asyncio.wait_for(entered.wait(), 0.2)
        await asyncio.wait_for(asyncio.shield(task), 0.2)
        assert not h.store.tasks
        assert h.store.records[receipt["id"]]["state"] == expected
        assert len(h.effects) == (1 if stage == "after_resolve" else 0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_pruning_overdue_authority_wait_reclaims_task_capacity():
    h = Harness()
    receipt = await h.store.submit(AUTH, h.token(), REQUEST)
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def authorize(auth):
        entered.set()
        await blocked.wait()

    h.store.authorize = authorize
    task = h.store.tasks[receipt["id"]]
    try:
        await entered.wait()
        h.now += 2000
        h.store._prune()
        await asyncio.wait_for(asyncio.shield(task), 0.2)
        assert not h.store.tasks
        assert h.store.records[receipt["id"]]["state"] == "rejected"
        assert not h.effects
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
