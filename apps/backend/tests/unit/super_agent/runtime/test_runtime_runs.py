"""runtime_runs: the one backend client of the bridge's /internal/v1/runs (spec 02 §5.1, C12)."""

import json
import logging

import pytest

from core import tasks_schema as ts
from core.services import local_session_grants as grants
from core.services import runtime_runs as rr
from core.services.runtime_transport import RuntimeResponse

OWNER = "org_abc"
AGENT = "a9e8d7c6-b5a4-4392-8170-6f5e4d3c2b1a"
TASK = "7d1e2f30-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
IDENTITY = rr.RunIdentity(run_id=ts.run_id(OWNER, TASK, 1), owner_id=OWNER, task_id=TASK, attempt=1, agent_id=AGENT)
REQUEST = rr.RunRequest(input="Reconcile October.", instructions="You are Finance.", max_iterations=40)
CONTAINER = {"owner_id": OWNER, "gateway_token": "gw-1", "agentcore_runtime_arn": "arn:x"}


@pytest.fixture
def bridge(monkeypatch):
    """A fake runtime_transport.request; answers come off `bridge.replies` (default: a running status)."""
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")
    calls, replies = [], []

    async def container(owner_id):
        assert owner_id == OWNER
        return CONTAINER

    async def request(container, path, *, method, json_body, headers, timeout_s):
        calls.append({"path": path, "method": method, "body": json_body, "headers": headers, "timeout": timeout_s})
        status, body = replies.pop(0) if replies else (200, {"run_id": IDENTITY.run_id, "status": "running"})
        return RuntimeResponse(status, json.dumps(body))

    monkeypatch.setattr(rr, "_container", container)
    monkeypatch.setattr(rr.runtime_transport, "request", request)

    class Bridge:
        pass

    Bridge.calls, Bridge.replies = calls, replies
    return Bridge


async def test_create_get_stop_round_trip(bridge):
    tools = [{"name": "terminal", "argsHash": "0123456789abcdef", "ok": True, "errorClass": None}]
    bridge.replies[:] = [
        (202, {"run_id": IDENTITY.run_id, "status": "queued", "session_id": IDENTITY.session_key}),
        (
            200,
            {
                "run_id": IDENTITY.run_id,
                "status": "completed",
                "output": "done",
                "usage": {"model": "m", "input_tokens": 3},
                "recentTools": tools,
            },
        ),
        (200, {"run_id": IDENTITY.run_id, "status": "completed", "output": "done"}),
    ]
    created = await rr.create_run(OWNER, IDENTITY, REQUEST)
    got = await rr.get_run(OWNER, IDENTITY)
    stopped = await rr.stop_run(OWNER, IDENTITY)

    assert created.status == "queued"
    assert (got.status, got.output, got.usage["input_tokens"], got.recent_tools) == ("completed", "done", 3, tools)
    assert stopped.status == "completed" and stopped.recent_tools == []
    create, poll, stop = bridge.calls
    assert (create["path"], create["method"], create["timeout"]) == ("/internal/v1/runs", "POST", 90)
    assert (poll["path"], poll["method"], poll["timeout"]) == (f"/internal/v1/runs/{IDENTITY.run_id}", "GET", 25)
    assert (stop["path"], stop["method"]) == (f"/internal/v1/runs/{IDENTITY.run_id}/stop", "POST")
    wire = {
        "runId": IDENTITY.run_id,
        "ownerId": OWNER,
        "taskId": TASK,
        "attempt": 1,
        "agentId": AGENT,
        "sessionKey": f"agent:{AGENT}:lightbulb:task:{TASK}",
        "mode": "worker",
        "session": {"kind": "workspace"},
    }
    assert create["body"] == {
        "identity": wire,
        "request": {
            "input": REQUEST.input,
            "instructions": REQUEST.instructions,
            "effects": "off",
            "limits": {"max_iterations": 40},
        },
    }
    assert poll["body"] == stop["body"] == {"identity": wire}
    assert all(c["headers"]["Authorization"] == "Bearer gw-1" for c in bridge.calls)


async def test_create_sends_task_grant_header(bridge):
    await rr.create_run(OWNER, IDENTITY, REQUEST)
    got = grants.verify(bridge.calls[0]["headers"]["X-Lightbulb-Local-Session-Token"])
    assert got.origin == {"kind": "task", "runId": IDENTITY.run_id, "taskId": TASK}
    assert (got.owner_id, got.agent_id, got.room_id, got.requester_id) == (OWNER, AGENT, None, None)


async def test_create_without_signing_key_still_sends(bridge, monkeypatch, caplog):
    monkeypatch.delenv("PAPERCLIP_SERVICE_TOKEN_KEY")
    with caplog.at_level(logging.WARNING):
        await rr.create_run(OWNER, IDENTITY, REQUEST)
    assert "X-Lightbulb-Local-Session-Token" not in bridge.calls[0]["headers"]
    assert "local session" in caplog.text


async def test_404_is_run_lost(bridge):
    bridge.replies.append((404, {"error": "run_not_found"}))
    with pytest.raises(rr.RunLost):
        await rr.get_run(OWNER, IDENTITY)


async def test_older_image_404_not_found_is_refused_not_lost(bridge):
    bridge.replies.append((404, {"error": "not_found"}))
    with pytest.raises(rr.RunRefused):
        await rr.get_run(OWNER, IDENTITY)


async def test_non_object_json_is_tolerated(bridge, monkeypatch):
    async def request(*_a, **_k):
        return RuntimeResponse(503, json.dumps(["recycling"]))

    monkeypatch.setattr(rr.runtime_transport, "request", request)
    with pytest.raises(rr.RuntimeBusy):
        await rr.get_run(OWNER, IDENTITY)


async def test_409_is_refused(bridge):
    bridge.replies[:] = [(409, {"error": "idempotency_conflict"}), (400, {"error": "unsupported_in_this_version"})]
    for _ in range(2):
        with pytest.raises(rr.RunRefused):
            await rr.create_run(OWNER, IDENTITY, REQUEST)


async def test_503_is_runtime_busy(bridge, monkeypatch):
    bridge.replies.append((503, {"error": "recycling"}))
    with pytest.raises(rr.RuntimeBusy):
        await rr.create_run(OWNER, IDENTITY, REQUEST)

    async def unreachable(*_a, **_k):
        import httpx

        raise httpx.ConnectError("down")

    monkeypatch.setattr(rr.runtime_transport, "request", unreachable)
    with pytest.raises(rr.RuntimeBusy):
        await rr.get_run(OWNER, IDENTITY)


@pytest.mark.parametrize("kind", ["user", "input"])
async def test_steer_kinds_pass_through(bridge, kind):
    bridge.replies.append((200, {"accepted": True}))
    assert await rr.steer_run(OWNER, IDENTITY, "check the totals", kind) is True
    call = bridge.calls[0]
    assert call["path"] == f"/internal/v1/runs/{IDENTITY.run_id}/steer"
    assert call["body"]["kind"] == kind and call["body"]["text"] == "check the totals"


def test_identity_session_key_matches_tasks_schema():
    import re

    assert IDENTITY.session_key == ts.worker_session_key(AGENT, TASK)
    assert re.fullmatch(ts.export_schema()["sessionKeys"]["worker"], IDENTITY.session_key)
