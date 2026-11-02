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
    through = AsyncMock()
    monkeypatch.setattr("core.services.hosted_mcp._through_member", through)
    session = {"humanUserId": OWNER}
    monkeypatch.setattr(
        "core.repositories.local_sessions_repo.get_session", AsyncMock(side_effect=lambda o, s: dict(session))
    )
    messages = {ORIGIN_MSG: {"messageId": ORIGIN_MSG, "author": {"kind": "human", "id": "user_x"}}}
    monkeypatch.setattr(
        "core.repositories.messaging_repo.get_message_by_id", AsyncMock(side_effect=lambda r, m: messages.get(m))
    )
    monkeypatch.setattr(
        "core.repositories.messaging_repo.get_room", AsyncMock(return_value={"roomId": ROOM, "ownerId": ORG})
    )
    monkeypatch.setattr(
        "core.repositories.messaging_repo.get_member",
        AsyncMock(side_effect=lambda r, k, i: {"kind": k, "id": i} if k == "human" else None),
    )
    asyncio.run(a2a_repo.put_rules(ORG, RULES))
    return SimpleNamespace(notify=notify, through=through, session=session)


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
