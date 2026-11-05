"""Each backend door asks `effect_doors.require_intent` before acting, with the classifier's operands, and turns a
refusal into 403 without touching the door (wave2-Q Task 12)."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from core.services import apps_service as svc
from core.services import effect_doors
from main import app
from routers import agent_mail, agent_text, apps, company_link_wallet

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
    state = {"refuse": False}

    async def require_intent(ws, agent_id, kind, operands, *, match=None):
        calls.append((ws, agent_id, kind, operands, match))
        if state["refuse"]:
            raise effect_doors.DoorRefused(kind)

    monkeypatch.setattr(effect_doors, "require_intent", require_intent)
    monkeypatch.setattr(
        agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": OWNER, "hook_token": TOKEN})
    )
    monkeypatch.setattr(agent_mail, "_check_rate_limit", lambda owner: True)
    monkeypatch.setattr(company_link_wallet, "_require_hook_token", AsyncMock(return_value={"owner_id": OWNER}))
    monkeypatch.setattr(apps, "_require_hook_token", AsyncMock(return_value={"owner_id": OWNER}))
    monkeypatch.setattr(apps, "require_always_on_entitlement", AsyncMock())
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
    (ws, agent, kind, operands, _) = gate["calls"][-1]
    assert (ws, agent, kind) == (OWNER, AGENT, "mail.send")
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
    assert gate["calls"][-1][2:4] == (
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
    (_, agent, kind, operands, match) = gate["calls"][-1]
    assert (agent, kind) == (AGENT, "purchase")
    assert operands == {"merchant": "Acme", "url": "https://acme.io", "amount": 35.0, "why": "hosting"}
    assert match({"merchant": "Acme", "url": "https://acme.io", "amount": 35, "why": "hosting"})  # the tool's int
    assert not match({"merchant": "Acme", "url": "https://acme.io", "amount": 36, "why": "hosting"})


def test_app_publish_door_for_agents_only(client, gate, monkeypatch):
    publish = AsyncMock(
        return_value={"app_id": "app_1", "state": "ready", "cards": [], "canvas_url": "u", "sample_data": False}
    )
    monkeypatch.setattr(svc, "publish", publish)
    hook = {**H, "X-Lightbulb-Owner": OWNER, "X-Lightbulb-Agent-Id": AGENT}
    body = {"source_id": "src_1"}
    gate["refuse"] = True
    r = client.post("/api/v1/apps/build/app_1/publish", json=body, headers=hook)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "effect_not_approved" and publish.await_count == 0
    gate["refuse"] = False
    assert client.post("/api/v1/apps/build/app_1/publish", json=body, headers=hook).status_code == 200
    (_, agent, kind, operands, match) = gate["calls"][-1]
    assert (agent, kind, operands) == (AGENT, "app.publish", {"appId": "app_1"})
    assert match({}) and match({"appId": "app_1"}) and not match({"appId": "app_2"})
    # A person publishing from their computer is not a Worker: the door is not asked.
    from core.auth import AuthContext

    monkeypatch.setattr(
        apps.network_login,
        "computer_identity",
        AsyncMock(return_value=(AuthContext(user_id="user_1", org_id=OWNER), "label")),
    )
    gate["refuse"] = True
    proof = {"X-Lightbulb-Proof": "p", "X-Lightbulb-Challenge": "c", "X-Lightbulb-Sig": "s"}
    assert client.post("/api/v1/apps/build/app_1/publish", json=body, headers=proof).status_code == 200
