"""Each backend door asks `effect_doors.require_intent` before acting, with the classifier's operands, and turns a
refusal into 403 without touching the door (wave2-Q Task 12)."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from core.services import effect_doors
from main import app
from routers import agent_mail

OWNER, AGENT, TOKEN = "org_1", "agent-finance", "hook-token"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client():
    app.dependency_overrides.clear()
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
