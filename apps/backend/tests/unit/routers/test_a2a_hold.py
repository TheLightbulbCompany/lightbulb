"""Held tasks: holder notification, the holder's decision, exceptions, expiry and rules changes."""

import asyncio
import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext, get_current_user
from core.repositories import a2a_repo
from core.services.a2a_authority import operands_hash

URI = "https://thelightbulb.company/a2a/ext/authority/v1"
ORG = "org_test_456"
AGENT = str(uuid.uuid4())  # a hosted agent id is a uuid (AgentMessageRequest.agent_id)
OWNER = "user_owner"
REQUESTER = "user_test_123"  # the `client` fixture's admin
RULES = {"revision": "r-1", "holders": {"agent:*": [OWNER]}, "rules": [], "default": "hold"}
BASE = f"/a2a/{ORG}/{AGENT}"


def msg(text="please do the thing", skill="message"):
    auth = {
        "operation": {"skill": skill, "resource": f"agent:{AGENT}", "operands": {"text": text}},
        "intent": "do the thing",
    }
    return {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": text}],
        "extensions": [URI],
        "metadata": {URI: auth},
    }


def rpc(method, params):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


@pytest.fixture
def hold_env(monkeypatch, a2a_env):
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": AGENT, "name": "Friday"}])
    )
    notify = AsyncMock()
    monkeypatch.setattr("core.services.messaging.create_agent_message", notify)
    asyncio.run(a2a_repo.put_rules(ORG, RULES))
    return SimpleNamespace(**vars(a2a_env), notify=notify)


@contextmanager
def as_user(app, user_id, role="org:member"):
    """Call routes as this user for the block, then restore the previous caller."""
    prev = app.dependency_overrides[get_current_user]

    async def _ctx():
        return AuthContext(user_id=user_id, org_id=ORG, org_role=role)

    app.dependency_overrides[get_current_user] = _ctx
    try:
        yield
    finally:
        app.dependency_overrides[get_current_user] = prev


def send(c, text="please do the thing", skill="message"):
    return c.post(BASE, json=rpc("SendMessage", {"message": msg(text, skill)})).json()["result"]["task"]


def get(c, task_id):
    return c.post(BASE, json=rpc("GetTask", {"id": task_id})).json()["result"]


def decide(c, task_id, verdict="allow", reason="fine"):
    return c.post(f"{BASE}/tasks/{task_id}/decide", json={"verdict": verdict, "reason": reason})


def records(c, task_id):
    return [r["record"] for r in c.get(f"{BASE}/tasks/{task_id}/record").json()]


def test_out_of_rule_message_is_held_and_holder_is_notified_with_operands_and_chain(client, app, hold_env):
    task = send(client)
    assert task["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    assert task["metadata"][URI]["state"] == "held"
    hold_env.send.assert_not_awaited()
    hold_env.notify.assert_awaited_once()
    owner_id, req = hold_env.notify.await_args.args
    assert owner_id == ORG and str(req.agent_id) == AGENT and req.event_id == f"a2a:{task['id']}:hold:{OWNER}"
    assert f"/a2a/{ORG}/{AGENT}/tasks/{task['id']}/decide" in req.text and REQUESTER in req.text
    assert "Request: please do the thing" in req.text and f"(via {REQUESTER})" in req.text
    holder_auth = hold_env.dm.await_args.args[0]
    assert holder_auth.user_id == OWNER and hold_env.dm.await_args.kwargs == {"create_key": task["id"]}
    [hold] = records(client, task["id"])
    assert hold["decision"] == "hold" and hold["holder"] == [OWNER] and hold["answered_by"] is None


def test_requester_cannot_decide_own_task_and_non_holder_is_403(client, app, hold_env):
    task_id = send(client)["id"]
    r = decide(client, task_id)
    assert r.status_code == 403 and r.json()["error"]["message"] == "self"
    with as_user(app, "user_test_789"):
        r = decide(client, task_id)
    assert r.status_code == 403 and r.json()["error"]["message"] == "not_holder"
    assert get(client, task_id)["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    hold_env.send.assert_not_awaited()


def test_holder_allow_dispatches_writes_exception_and_record(client, app, hold_env):
    task_id = send(client)["id"]
    with as_user(app, OWNER):
        r = decide(client, task_id)
        assert decide(client, task_id).status_code == 409  # answered once only
    assert r.status_code == 200 and r.json()["status"]["state"] == "TASK_STATE_WORKING"
    hold_env.send.assert_awaited_once()
    assert hold_env.send.await_args.args[0].user_id == REQUESTER
    assert hold_env.send.await_args.args[2].text == "please do the thing"
    assert get(client, task_id)["status"]["state"] == "TASK_STATE_WORKING"
    recs = records(client, task_id)
    assert [x["decision"] for x in recs] == ["hold", "allow_by_holder"]
    assert recs[1]["answered_by"] == OWNER and recs[1]["holder"] == OWNER
    h = operands_hash({"text": "please do the thing"})
    row = asyncio.run(a2a_repo.get_exception(ORG, f"agent:{AGENT}", "message", REQUESTER, h, int(time.time())))
    assert row is not None and row["holder"] == OWNER and row["taskId"] == task_id


def test_holder_deny_rejects_without_dispatch(client, app, hold_env):
    task_id = send(client)["id"]
    with as_user(app, OWNER):
        r = decide(client, task_id, verdict="deny", reason="no")
    assert r.status_code == 200 and r.json()["status"]["state"] == "TASK_STATE_REJECTED"
    hold_env.send.assert_not_awaited()
    assert [x["decision"] for x in records(client, task_id)] == ["hold", "deny_by_holder"]


def test_second_identical_request_is_allowed_by_exception(client, app, hold_env):
    task_id = send(client)["id"]
    with as_user(app, OWNER):
        assert decide(client, task_id).status_code == 200
    again = send(client)
    exc = asyncio.run(
        a2a_repo.get_exception(
            ORG,
            f"agent:{AGENT}",
            "message",
            REQUESTER,
            operands_hash({"text": "please do the thing"}),
            int(time.time()),
        )
    )
    assert again["status"]["state"] == "TASK_STATE_WORKING"
    assert again["metadata"][URI]["rule"] == f"exception:{exc['id']}"
    other = send(client, text="something different")
    assert other["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    with as_user(app, "user_bob"):  # the same request from someone else: the exception is not theirs
        assert send(client)["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"


def test_every_holder_is_notified(client, app, hold_env):
    asyncio.run(a2a_repo.put_rules(ORG, {**RULES, "holders": {"agent:*": [OWNER, "user_second"]}}))
    task_id = send(client)["id"]
    assert hold_env.notify.await_count == 2
    ids = [c.args[1].event_id for c in hold_env.notify.await_args_list]
    assert ids == [f"a2a:{task_id}:hold:{OWNER}", f"a2a:{task_id}:hold:user_second"]
    assert [c.args[0].user_id for c in hold_env.dm.await_args_list] == [OWNER, "user_second"]


def test_expired_hold_reads_as_canceled_and_rejects_decide(client, app, hold_env, monkeypatch):
    task_id = send(client)["id"]
    later = int(time.time()) + 86_401
    monkeypatch.setattr("core.services.a2a_tasks.time", SimpleNamespace(time=lambda: later))
    t = get(client, task_id)
    assert t["status"]["state"] == "TASK_STATE_CANCELED" and t["metadata"][URI]["reason"] == "expired"
    get(client, task_id)  # a second read writes nothing more
    with as_user(app, OWNER):
        r = decide(client, task_id)
    assert r.status_code == 409 and r.json()["error"]["message"] == "expired"
    recs = records(client, task_id)
    assert [x["decision"] for x in recs] == ["hold", "canceled"] and recs[1]["rule"] == "expired"
    hold_env.send.assert_not_awaited()


def test_put_rules_assigns_the_revision_and_a_new_one_redecides_held_tasks(client, app, hold_env):
    with as_user(app, "user_test_789"):
        assert client.put(f"/a2a/{ORG}/rules", json=RULES).status_code == 403
        assert client.get(f"/a2a/{ORG}/rules").status_code == 403
    rev = client.put(f"/a2a/{ORG}/rules", json=RULES).json()["revision"]
    assert len(rev) == 64 and rev != "r-1"
    assert client.put(f"/a2a/{ORG}/rules", json={**RULES, "revision": "mine"}).json()["revision"] == rev  # ignored
    assert client.get(f"/a2a/{ORG}/rules").json()["revision"] == rev
    task_id = send(client)["id"]
    assert (
        client.put(f"/a2a/{ORG}/rules", json=RULES).json()["revision"] == rev
    )  # same document: the hold is not re-decided
    assert get(client, task_id)["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    changed = {**RULES, "holders": {"agent:*": [OWNER, "user_second"]}}
    new = client.put(f"/a2a/{ORG}/rules", json=changed).json()["revision"]
    assert new != rev and client.get(f"/a2a/{ORG}/rules").json()["revision"] == new
    assert get(client, task_id)["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"  # still held (test_a2a_redecide)
    assert [x["decision"] for x in records(client, task_id)] == ["hold", "redecided"]


def test_failed_dispatch_after_allow_is_a_failed_task(client, app, hold_env):
    task_id = send(client)["id"]
    hold_env.send.side_effect = RuntimeError("runtime down")
    with as_user(app, OWNER):
        r = decide(client, task_id)
    assert r.status_code == 200 and r.json()["status"]["state"] == "TASK_STATE_FAILED"
    assert get(client, task_id)["metadata"][URI]["reason"] == "dispatch_failed"
    assert [(x["decision"], x["rule"]) for x in records(client, task_id)] == [
        ("hold", "default"),
        ("allow_by_holder", "default"),
        ("failed", "dispatch_failed"),
    ]


def test_get_rules_without_a_document_returns_the_defaults(client, a2a_env):
    r = client.get(f"/a2a/{ORG}/rules").json()
    assert r["revision"] == "default" and r["holders"]["agent:*"] == [REQUESTER]
