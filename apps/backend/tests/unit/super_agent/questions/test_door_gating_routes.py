"""Each backend door asks `effect_doors.require_intent` before acting, with the classifier's operands, and turns a
refusal into 403 without touching the door (wave2-Q Task 12)."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from core.services import effect_doors
from main import app
from routers import agent_mail, agent_text, company_link_wallet

OWNER, AGENT, TOKEN = "org_1", "agent-finance", "hook-token"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    app.dependency_overrides[company_link_wallet._enabled] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def gate(monkeypatch):
    calls = []
    state = {"refuse": False, "row": {"allowedBy": None, "taskId": "task-1"}}

    async def require_intent(ws, kind, operands, *, match=None):
        calls.append((ws, kind, operands, match))
        if state["refuse"]:
            raise effect_doors.DoorRefused(kind)
        return state["row"]

    monkeypatch.setattr(effect_doors, "require_intent", require_intent)
    monkeypatch.setattr(
        agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": OWNER, "hook_token": TOKEN})
    )
    monkeypatch.setattr(agent_mail, "_check_rate_limit", lambda owner: True)
    monkeypatch.setattr(company_link_wallet, "_require_hook_token", AsyncMock(return_value={"owner_id": OWNER}))
    state["calls"] = calls
    return state


def test_mail_door(client, gate, monkeypatch):
    send = AsyncMock(return_value="sent")
    monkeypatch.setattr(agent_mail, "send", send)
    body = {"agent_id": AGENT, "subject": "Q3", "body_text": "hi\n", "to": "acme@example.com"}
    gate["refuse"] = True
    r = client.post(f"/api/v1/agent-mail/u/{OWNER}/send", json=body, headers=H)
    assert r.status_code == 403 and r.json()["detail"]["status"] == "effect_not_approved"
    send.assert_not_awaited()
    gate["refuse"] = False
    assert client.post(f"/api/v1/agent-mail/u/{OWNER}/send", json=body, headers=H).status_code == 200
    (ws, kind, operands, _) = gate["calls"][-1]
    assert (ws, kind) == (OWNER, "mail.send")
    assert operands == {"agentId": AGENT, "to": "acme@example.com", "subject": "Q3", "body": "hi"}
    send.assert_awaited_once()


def test_text_door(client, gate, monkeypatch):
    reply = AsyncMock(return_value="sent")
    monkeypatch.setattr(agent_text, "send_reply", reply)
    body = {"agent_id": AGENT, "reply_key": "team@acme.example/rk1", "text": "On my way"}
    gate["refuse"] = True
    r = client.post(f"/api/v1/agent-text/u/{OWNER}/send", json=body, headers=H)
    assert r.status_code == 403 and reply.await_count == 0
    gate["refuse"] = False
    assert client.post(f"/api/v1/agent-text/u/{OWNER}/send", json=body, headers=H).status_code == 200
    assert gate["calls"][-1][1:3] == (
        "text.send",
        {"agentId": AGENT, "replyKey": "team@acme.example/rk1", "body": "On my way"},
    )


def test_purchase_door(client, gate, monkeypatch):
    request = AsyncMock(return_value={"requestId": "pr1", "status": "pending"})
    monkeypatch.setattr(company_link_wallet.purchases, "request_purchase", request)
    body = {
        "agentId": AGENT,
        "merchant": "Acme",
        "url": "https://acme.io",
        "amount": 35.0,
        "why": "hosting",
        "issueId": "i1",
    }
    gate["refuse"] = True
    r = client.post(f"/api/v1/internal/link-wallet/{OWNER}/requests", json=body, headers=H)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "effect_not_approved" and request.await_count == 0
    gate["refuse"] = False
    assert client.post(f"/api/v1/internal/link-wallet/{OWNER}/requests", json=body, headers=H).status_code == 200
    (_, kind, operands, match) = gate["calls"][-1]
    assert kind == "purchase"
    assert operands == {"merchant": "Acme", "url": "https://acme.io", "amount": 35.0, "why": "hosting"}
    assert match({"merchant": "Acme", "url": "https://acme.io", "amount": 35, "why": "hosting"})  # the tool's int
    assert not match({"merchant": "Acme", "url": "https://acme.io", "amount": 36, "why": "hosting"})
    assert request.await_args.kwargs["approved"] is True  # the intent row came from an answered Question
    assert request.await_args.kwargs["issue_id"] == "task-1"  # the intent's task, not the Worker's issueId
    for junk in ({"issueId": "made-up"}, {"issueId": None}, {}):
        b = {k: v for k, v in body.items() if k != "issueId"} | junk
        assert client.post(f"/api/v1/internal/link-wallet/{OWNER}/requests", json=b, headers=H).status_code == 200
        assert request.await_args.kwargs["issue_id"] == "task-1"
    gate["row"] = {"allowedBy": "mode:auto", "taskId": "task-1"}
    client.post(f"/api/v1/internal/link-wallet/{OWNER}/requests", json=body, headers=H)
    assert request.await_args.kwargs["approved"] is False  # the mode let it through: no person said yes
