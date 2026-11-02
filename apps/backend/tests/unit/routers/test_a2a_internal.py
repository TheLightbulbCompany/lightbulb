"""ask_session from a hosted agent: the hook-token route, origin person, decision and records."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.repositories import a2a_repo
from core.services.network_proof import _unb64

ORG = "org_test_456"
TOKEN = "hook-token-for-org"
AGENT = str(uuid.uuid4())
OWNER = "user_owner"
ROOM = str(uuid.uuid4())
ORIGIN_MSG = str(uuid.uuid4())
AGENT_MSG = str(uuid.uuid4())
FOREIGN_MSG = str(uuid.uuid4())
URL = f"/api/v1/internal/a2a/{ORG}/ask-session"
H = {"Authorization": f"Bearer {TOKEN}"}
RULES = {
    "revision": "r-1",
    "holders": {"agent:*": [OWNER], "session:*": ["@session-person"]},
    "rules": [],
    "default": "hold",
}


@pytest.fixture
def env(monkeypatch, a2a_env):
    monkeypatch.setattr(
        "core.services.container_auth.container_repo.get_by_owner_id",
        AsyncMock(return_value={"owner_id": ORG, "hook_token": TOKEN}),
    )
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": AGENT, "name": "Friday"}])
    )
    notify = AsyncMock()
    monkeypatch.setattr("core.services.messaging.create_agent_message", notify)
    # The network layer under network_route.app_call: OWNER's computer link and the wire call.
    link = {"key": "k1", "userId": OWNER, "generation": "g1", "connectedAt": 5, "taskAddress": "http://t"}
    conns = SimpleNamespace(routes_for_user=AsyncMock(return_value=[link]), live_routes=AsyncMock(return_value=[link]))
    monkeypatch.setattr("core.services.network_route._connections", lambda: conns)
    monkeypatch.setattr("core.services.messaging.human_display_name", AsyncMock(return_value="Owner"))
    through = AsyncMock(return_value={"ok": True})  # network_route.call
    monkeypatch.setattr("core.services.network_route.call", through)
    session = {"humanUserId": OWNER}
    monkeypatch.setattr(
        "core.repositories.local_sessions_repo.get_session", AsyncMock(side_effect=lambda o, s: dict(session))
    )
    messages = {
        ORIGIN_MSG: {"messageId": ORIGIN_MSG, "author": {"kind": "human", "id": "user_x"}},
        AGENT_MSG: {"messageId": AGENT_MSG, "author": {"kind": "agent", "id": AGENT}},
        FOREIGN_MSG: {"messageId": FOREIGN_MSG, "author": {"kind": "human", "id": "user_foreign"}},
    }
    monkeypatch.setattr(
        "core.repositories.messaging_repo.get_message_by_id", AsyncMock(side_effect=lambda r, m: messages.get(m))
    )
    monkeypatch.setattr(
        "core.repositories.messaging_repo.get_room", AsyncMock(return_value={"roomId": ROOM, "ownerId": ORG})
    )
    monkeypatch.setattr(
        "core.repositories.messaging_repo.get_member",
        AsyncMock(
            side_effect=lambda r, k, i: {
                "kind": k,
                "id": i,
                **({"homeOwnerId": "org_other"} if i == "user_foreign" else {}),
            }
        ),
    )
    put_task = AsyncMock(wraps=a2a_repo.put_task)
    monkeypatch.setattr("core.repositories.a2a_repo.put_task", put_task)
    asyncio.run(a2a_repo.put_rules(ORG, RULES))
    return SimpleNamespace(notify=notify, through=through, session=session, put_task=put_task)


def body(**kw):
    return {
        "sessionId": "abc",
        "text": "status?",
        "intent": "founder asked",
        "agentId": AGENT,
        "runId": "run-1",
        "roomId": ROOM,
        **kw,
    }


def claims(jws):
    return json.loads(_unb64(jws.split(".")[1]))


def test_wrong_hook_token_is_401(client, env):
    r = client.post(URL, json=body(), headers={"Authorization": "Bearer WRONG"})
    assert r.status_code == 401
    env.through.assert_not_awaited()


def test_origin_human_no_rule_is_held_for_session_person(client, env):
    r = client.post(URL, json=body(originMessageId=ORIGIN_MSG), headers=H)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["state"] == "held" and out["holders"] == [OWNER]
    env.through.assert_not_awaited()
    env.notify.assert_awaited_once()
    row = asyncio.run(a2a_repo.get_task(ORG, out["taskId"]))
    auth = claims(row["authority"])
    assert auth["requester"] == {"person": "user_x", "org": ORG, "verified_by": "origin-message"}
    assert [h["agent"] for h in auth["chain"]] == [None, AGENT]
    assert auth["chain"][1]["session"] == "run-1" and auth["chain"][1]["person"] == "user_x"
    assert auth["operation"]["resource"] == "session:abc" and auth["operation"]["skill"] == "ask_session"
    [rec] = asyncio.run(a2a_repo.list_records(ORG, out["taskId"]))
    assert claims(rec["jws"])["decision"] == "hold"


def test_no_origin_requester_is_admin_holder_agent_runtime(client, env):
    env.session["humanUserId"] = "user_laptop"
    r = client.post(URL, json=body(), headers=H)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["state"] == "held" and out["holders"] == ["user_laptop"]
    [rec] = asyncio.run(a2a_repo.list_records(ORG, out["taskId"]))
    req = claims(rec["jws"])["input"]["requester"]
    assert req["person"] == OWNER and req["verified_by"] == "agent-runtime"


def test_tokenless_malformed_request_is_401_not_422(client, env):
    assert client.post(URL, json={"nonsense": 1}).status_code == 401


@pytest.mark.parametrize("origin", [AGENT_MSG, FOREIGN_MSG, str(uuid.uuid4())])
def test_named_origin_that_fails_a_check_is_403_and_stores_nothing(client, env, origin):
    r = client.post(URL, json=body(originMessageId=origin), headers=H)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "bad_origin"
    env.put_task.assert_not_awaited()
    env.through.assert_not_awaited()


def test_no_origin_admin_holder_is_session_person_is_held_and_the_admin_answers_it(client, app, env):
    from tests.unit.routers.test_a2a_hold import as_user

    r = client.post(URL, json=body(), headers=H)  # session person is OWNER, the admin holder
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["state"] == "held" and out["holders"] == [OWNER]
    with as_user(app, OWNER, role="org:admin"):  # the stand-in requester is the holder: not self
        d = client.post(f"/a2a/{ORG}/{AGENT}/tasks/{out['taskId']}/decide", json={"verdict": "allow"})
    assert d.status_code == 200, d.text
    assert d.json()["status"]["state"] == "TASK_STATE_COMPLETED"
    env.through.assert_awaited_once()
    assert env.through.await_args.args[1] == "app.send"


def test_allowed_ask_session_reaches_app_send_and_completes_delivered(client, env):
    rule = {"id": "s1", "resource": "session:*", "skill": "ask_session", "requesters": [f"agent:{AGENT}"]}
    rules = {**RULES, "rules": [{**rule, "effect": "allow"}]}
    asyncio.run(a2a_repo.put_rules(ORG, rules))
    r = client.post(URL, json=body(), headers=H)  # no origin: rides the stand-in requester OWNER's link
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["state"] == "completed"
    (link, op, params, _), _ = env.through.await_args
    assert link["key"] == "k1" and op == "app.send"
    assert params["to"] == "abc" and params["request_id"] == out["taskId"] and params["app"] == AGENT
    assert params["title"] == "Friday via Owner" and params["text"].endswith("\nstatus?")
    row = asyncio.run(a2a_repo.get_task(ORG, out["taskId"]))
    assert row["delivered"] is True and row["artifact"]["parts"] == [{"text": "delivered"}]
    recs = [claims(x["jws"]) for x in asyncio.run(a2a_repo.list_records(ORG, out["taskId"]))]
    assert [(x["decision"], x["rule"]) for x in recs] == [("allow", "s1"), ("completed", "s1")]
