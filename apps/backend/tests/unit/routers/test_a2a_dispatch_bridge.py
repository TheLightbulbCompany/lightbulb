"""A hosted ask_session rides the workspace's cloud member first (``network.a2a_send``), falls back to
the origin person's computer link, and the laptop session's reply completes the task."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from core.repositories import a2a_repo
from core.services import network_member, network_route
from tests.unit.routers.test_a2a_internal import AGENT, H, ORG, RULES, URL, body, env  # noqa: F401

URI = "https://thelightbulb.company/a2a/ext/authority/v1"
REPLY_URL = f"/api/v1/internal/a2a/{ORG}/session-reply"
ALLOW = {
    "id": "s1",
    "resource": "session:*",
    "skill": "ask_session",
    "requesters": [f"agent:{AGENT}"],
    "effect": "allow",
}


@pytest.fixture
def bridge(monkeypatch, env):  # noqa: F811
    asyncio.run(a2a_repo.put_rules(ORG, {**RULES, "rules": [ALLOW]}))
    warm = AsyncMock(return_value=True)
    monkeypatch.setattr("core.services.network_member._session_is_warm", warm)
    rpc = AsyncMock(return_value={"receipt": "r1"})
    monkeypatch.setattr("core.services.container_rpc.call", rpc)
    best_link = AsyncMock(wraps=network_route.best_link)
    monkeypatch.setattr("core.services.network_route.best_link", best_link)
    env.warm, env.rpc, env.best_link = warm, rpc, best_link
    return env


def ask(client):
    r = client.post(URL, json=body(), headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_dispatch_prefers_the_bridge_when_the_workspace_has_a_runtime(client, bridge):
    out = ask(client)
    assert out["state"] == "completed"
    bridge.rpc.assert_awaited_once()
    owner, method, params = bridge.rpc.await_args.args
    assert owner == ORG and method == "network.a2a_send"
    row = asyncio.run(a2a_repo.get_task(ORG, out["taskId"]))
    assert params["to"] == "abc" and params["request_id"] == out["taskId"]
    assert params["person"] == network_member.app_person(bridge.session["humanUserId"])
    assert params["text"].startswith("[A2A] ") and params["text"].endswith("\nstatus?")
    assert params["a2a"] == {
        "role": "ROLE_AGENT",
        "parts": [{"text": params["text"]}],
        "metadata": {URI: row["authority"]},
        "extensions": [URI],
    }
    bridge.best_link.assert_not_awaited()
    bridge.through.assert_not_awaited()
    assert row["delivered"] is True and row["dispatchRequestId"] == out["taskId"]


def test_dispatch_falls_back_to_best_link_when_the_bridge_raises(client, bridge):
    bridge.rpc.side_effect = RuntimeError('{"code": "UNAVAILABLE"}')  # the member went down mid-request
    out = ask(client)
    assert out["state"] == "completed"
    bridge.rpc.assert_awaited_once()
    bridge.best_link.assert_awaited_once()
    (link, op, params, _), _ = bridge.through.await_args
    assert link["key"] == "k1" and op == "app.send" and params["request_id"] == out["taskId"]


def test_session_reply_completes_the_task_with_the_reply_text(client, bridge):
    task_id = ask(client)["taskId"]
    before = asyncio.run(a2a_repo.list_records(ORG, task_id))
    r = client.post(REPLY_URL, json={"request_id": task_id, "text": "all green"}, headers=H)
    assert r.status_code == 200, r.text
    row = asyncio.run(a2a_repo.get_task(ORG, task_id))
    assert row["state"] == "completed" and row["artifact"]["parts"] == [{"text": "all green"}]
    assert len(asyncio.run(a2a_repo.list_records(ORG, task_id))) == len(before)  # completed once, one record
    assert client.post(REPLY_URL, json={"request_id": "nope", "text": "x"}, headers=H).status_code == 404


def test_session_reply_is_idempotent(client, bridge):
    task_id = ask(client)["taskId"]
    for text in ("first", "second"):
        assert client.post(REPLY_URL, json={"request_id": task_id, "text": text}, headers=H).status_code == 200
    row = asyncio.run(a2a_repo.get_task(ORG, task_id))
    assert row["artifact"]["parts"] == [{"text": "first"}]
    decisions = [r["jws"] for r in asyncio.run(a2a_repo.list_records(ORG, task_id))]
    assert len(decisions) == 2  # allow, completed: a reply writes no second completion


def test_session_reply_with_a_working_task_completes_it_with_a_record(client, bridge):
    task_id = ask(client)["taskId"]
    asyncio.run(a2a_repo.update_task(ORG, task_id, state="working", extra={"delivered": False}))
    r = client.post(REPLY_URL, json={"request_id": task_id, "text": "done"}, headers=H)
    assert r.status_code == 200, r.text
    row = asyncio.run(a2a_repo.get_task(ORG, task_id))
    assert row["state"] == "completed" and row["artifact"]["parts"] == [{"text": "done"}]
    assert len(asyncio.run(a2a_repo.list_records(ORG, task_id))) == 3


def test_session_reply_with_a_bad_hook_token_is_401(client, bridge):
    task_id = ask(client)["taskId"]
    r = client.post(REPLY_URL, json={"request_id": task_id, "text": "x"}, headers={"Authorization": "Bearer WRONG"})
    assert r.status_code == 401  # _require_hook_token answers 401, never 403
    assert asyncio.run(a2a_repo.get_task(ORG, task_id))["artifact"]["parts"] == [{"text": "delivered"}]
