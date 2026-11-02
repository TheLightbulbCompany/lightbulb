import asyncio

import pytest

from core.services import a2a_authority, a2a_tasks

URI = "https://thelightbulb.company/a2a/ext/authority/v1"
AUTH = {
    "operation": {"skill": "message", "resource": "agent:friday", "operands": {"text": "hello"}},
    "intent": "say hello",
}


def rpc(method, params, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params}


def msg(auth_req, extensions=(URI,), key=URI):
    return {
        "messageId": "m1",
        "role": "ROLE_USER",
        "parts": [{"text": "hello"}],
        "extensions": list(extensions),
        "metadata": {key: auth_req},
    }


@pytest.fixture
def client_org_admin(client):
    # tests/conftest.py's `client` is already the org admin (user_test_123 in org_test_456).
    return client


def send(c, message=None):
    return c.post("/a2a/org_test_456/friday", json=rpc("SendMessage", {"message": message or msg(AUTH)})).json()


def get(c, task_id):
    return c.post("/a2a/org_test_456/friday", json=rpc("GetTask", {"id": task_id})).json()["result"]


def reply(env, text, message_id="am1", reply_to=None):
    asyncio.run(
        a2a_tasks.on_agent_reply(
            "org_test_456",
            room_id=env.room,
            agent_id="friday",
            message_id=message_id,
            text=text,
            reply_to_message_id=reply_to,
        )
    )


def test_card_lists_required_extension(client, a2a_env):
    r = client.get("/a2a/org_test_456/friday/agent-card.json")
    c = r.json()
    ext = c["capabilities"]["extensions"][0]
    assert r.status_code == 200 and ext == {"uri": URI, "required": True, "description": ext["description"]}
    assert c["name"] == "Friday" and c["capabilities"]["streaming"] is False
    assert c["supportedInterfaces"][0]["protocolVersion"] == "1.0" and "signatures" not in c
    assert {s["id"] for s in c["skills"]} == {"message", "ask_session"}
    assert client.get("/a2a/org_test_456/nobody/agent-card.json").status_code == 404


def test_keys_lists_the_issuer_key(client, a2a_env):
    assert client.get("/a2a/keys.json").json()["keys"][0]["kid"] == a2a_authority.network_proof.kid()


def test_send_without_extension_is_32008(client_org_admin, a2a_env):
    assert send(client_org_admin, msg(AUTH, extensions=()))["error"]["code"] == -32008
    a2a_env.send.assert_not_awaited()


def test_send_with_wrong_metadata_key_is_32008(client_org_admin, a2a_env):
    assert send(client_org_admin, msg(AUTH, key="https://other/ext"))["error"]["code"] == -32008


@pytest.mark.parametrize(
    ("auth_req", "check"),
    [
        ({"intent": "no operation"}, "shape"),
        ({**AUTH, "operation": {**AUTH["operation"], "resource": "agent:someone_else"}}, "resource"),
    ],
)
def test_present_but_invalid_authority_is_32602_naming_the_check(client_org_admin, a2a_env, auth_req, check):
    err = send(client_org_admin, msg(auth_req))["error"]
    assert err["code"] == -32602 and check in err["message"]
    a2a_env.send.assert_not_awaited()


@pytest.mark.parametrize("agent", ["nobody", "user_bob"])
def test_send_to_an_id_not_on_the_roster_is_32602(client_org_admin, a2a_env, agent):
    auth_req = {**AUTH, "operation": {**AUTH["operation"], "resource": f"agent:{agent}"}}
    r = client_org_admin.post(f"/a2a/org_test_456/{agent}", json=rpc("SendMessage", {"message": msg(auth_req)}))
    err = r.json()["error"]
    assert err["code"] == -32602 and "agent" in err["message"]
    a2a_env.dm.assert_not_awaited()
    a2a_env.send.assert_not_awaited()


def test_operands_other_than_the_text_sent_are_32602(client_org_admin, a2a_env):
    bad = {**AUTH, "operation": {**AUTH["operation"], "operands": {"text": "something else"}}}
    assert send(client_org_admin, msg(bad))["error"]["code"] == -32602
    a2a_env.send.assert_not_awaited()


def test_allowed_send_dispatches_and_is_working_then_completes_on_reply(client_org_admin, a2a_env):
    task = send(client_org_admin)["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_WORKING" and task["contextId"] == a2a_env.room
    a2a_env.send.assert_awaited_once()
    assert a2a_env.dm.await_args.kwargs == {"create_key": task["id"]}
    assert a2a_env.send.await_args.args[2].text == "hello"
    reply(a2a_env, "hi back", reply_to=a2a_env.sent[0])
    t = get(client_org_admin, task["id"])
    assert t["status"]["state"] == "TASK_STATE_COMPLETED" and t["artifacts"][0]["parts"][0]["text"] == "hi back"
    assert URI in t["metadata"] and t["metadata"][URI]["rule"] == "r1"


def states(c, *ids):
    return [get(c, i)["status"]["state"].removeprefix("TASK_STATE_") for i in ids]


def test_reply_addressed_to_a_dispatch_completes_that_task_even_if_an_older_one_is_open(client_org_admin, a2a_env):
    first = send(client_org_admin)["result"]["task"]["id"]
    second = send(client_org_admin)["result"]["task"]["id"]
    reply(a2a_env, "two", reply_to=a2a_env.sent[1])
    assert states(client_org_admin, first, second) == ["WORKING", "COMPLETED"]


def test_reply_addressed_to_no_dispatch_completes_nothing(client_org_admin, a2a_env):
    first = send(client_org_admin)["result"]["task"]["id"]
    reply(a2a_env, "elsewhere", reply_to="00000000-0000-0000-0000-000000000000")
    assert states(client_org_admin, first) == ["WORKING"]


def test_unaddressed_reply_links_only_when_one_task_is_open(client_org_admin, a2a_env):
    first = send(client_org_admin)["result"]["task"]["id"]
    reply(a2a_env, "just one", message_id="am1")
    assert states(client_org_admin, first) == ["COMPLETED"]
    second = send(client_org_admin)["result"]["task"]["id"]
    third = send(client_org_admin)["result"]["task"]["id"]
    reply(a2a_env, "which one?", message_id="am2")
    assert states(client_org_admin, second, third) == ["WORKING", "WORKING"]


def test_a_retried_reply_completes_one_task_only(client_org_admin, a2a_env):
    first = send(client_org_admin)["result"]["task"]["id"]
    reply(a2a_env, "hi", message_id="am1")
    second = send(client_org_admin)["result"]["task"]["id"]
    reply(a2a_env, "hi", message_id="am1")  # the same post, retried
    assert states(client_org_admin, first, second) == ["COMPLETED", "WORKING"]
    records = client_org_admin.get(f"/a2a/org_test_456/friday/tasks/{first}/record").json()
    assert [r["record"]["decision"] for r in records] == ["allow", "completed"]


def test_wrong_org_is_403_and_cancel_is_32004_and_unknown_method_is_32601(client_org_admin, a2a_env):
    assert client_org_admin.post("/a2a/org_other/friday", json=rpc("GetTask", {"id": "x"})).status_code == 403
    c = client_org_admin.post("/a2a/org_test_456/friday", json=rpc("CancelTask", {"id": "x"}))
    assert c.status_code == 200 and c.json()["error"]["code"] == -32004
    assert client_org_admin.post("/a2a/org_test_456/friday", json=rpc("Nope", {})).json()["error"]["code"] == -32601
    assert (
        client_org_admin.post("/a2a/org_test_456/friday", json=rpc("GetTask", {"id": "x"})).json()["error"]["code"]
        == -32001
    )


def test_record_endpoint_returns_signed_records_oldest_first(client_org_admin, a2a_env):
    task_id = send(client_org_admin)["result"]["task"]["id"]
    reply(a2a_env, "hi back", reply_to=a2a_env.sent[0])
    records = client_org_admin.get(f"/a2a/org_test_456/friday/tasks/{task_id}/record").json()
    assert [r["record"]["decision"] for r in records] == ["allow", "completed"]
    first = a2a_authority.verify(records[0]["jws"], typ=a2a_authority.RECORD_TYP)
    assert first == records[0]["record"] and first["rule"] == "r1" and first["task"] == task_id
    assert first["input"]["requester"]["person"] == "user_test_123"
    assert get(client_org_admin, task_id)["metadata"][URI]["record"] == records[-1]["jws"]


@pytest.mark.parametrize("progress", [False, True])
def test_agent_reply_route_links_only_final_replies_and_survives_a_hook_failure(client, monkeypatch, progress):
    import uuid
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    room, agent, mid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    receipt = SimpleNamespace(message_id=mid, room_id=room, created_at=datetime.now(timezone.utc))
    monkeypatch.setattr("routers.messaging._require_hook_token", AsyncMock(return_value={}))
    monkeypatch.setattr("routers.messaging.service.create_agent_message", AsyncMock(return_value=receipt))
    monkeypatch.setattr("core.services.meeting_notes.on_agent_reply", AsyncMock())
    hook = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr("core.services.a2a_tasks.on_agent_reply", hook)
    body = {"eventId": "e1", "roomId": str(room), "agentId": str(agent), "text": "hi back", "progress": progress}
    body |= {"replyToMessageId": None, "threadRootMessageId": None}
    r = client.post("/api/v1/internal/messaging/agent-messages/org_test_456", json=body)
    assert r.status_code == 201
    if progress:
        hook.assert_not_awaited()
    else:
        hook.assert_awaited_once_with(
            "org_test_456",
            room_id=str(room),
            agent_id=str(agent),
            message_id=str(mid),
            text="hi back",
            reply_to_message_id=None,
        )


@pytest.mark.parametrize(
    ("holders", "state", "meta_state", "rule"),
    [
        ({"agent:*": ["user_holder"]}, "TASK_STATE_AUTH_REQUIRED", "held", "default"),
        ({}, "TASK_STATE_REJECTED", "rejected", "no_holder"),
    ],
)
def test_out_of_rule_send_is_held_or_rejected_without_a_holder(
    client_org_admin, a2a_env, holders, state, meta_state, rule
):
    from core.repositories import a2a_repo

    asyncio.run(
        a2a_repo.put_rules("org_test_456", {"revision": "rev1", "holders": holders, "rules": [], "default": "hold"})
    )
    task = send(client_org_admin)["result"]["task"]
    assert task["status"]["state"] == state
    assert (task["metadata"][URI]["state"], task["metadata"][URI]["rule"]) == (meta_state, rule)
    a2a_env.send.assert_not_awaited()


def test_dispatch_failure_is_a_failed_task(client_org_admin, a2a_env):
    a2a_env.send.side_effect = RuntimeError("runtime down")
    task = send(client_org_admin)["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED" and task["metadata"][URI]["reason"] == "dispatch_failed"
