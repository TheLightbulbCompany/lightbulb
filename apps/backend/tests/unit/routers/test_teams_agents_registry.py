"""`GET /teams/agents` and `/teams/members` answer from the registry and Clerk with Paperclip down."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from core.auth import AuthContext, get_current_user
from core.repositories import agent_registry_repo
from core.services import paperclip_admin_client
from routers.teams import agents as agents_mod

WS = "org_1"
CEO, GROWTH, GONE = (f"00000000-0000-4000-8000-00000000000{i}" for i in (1, 2, 3))
ROWS = [
    {"agentId": CEO, "name": "CEO", "role": "ceo", "title": "Lead", "persona": "Runs it", "status": "active"},
    {"agentId": GROWTH, "name": "Growth", "role": "cmo", "title": "Growth", "persona": "Grows", "status": "paused"},
    {"agentId": GONE, "name": "Gone", "role": "general", "status": "deleted"},
]


@pytest.fixture
def client(monkeypatch):
    def paperclip_down(*_a, **_k):
        raise AssertionError("the roster must not call Paperclip")

    monkeypatch.setattr(paperclip_admin_client.PaperclipAdminClient, "__init__", paperclip_down)
    monkeypatch.setattr(agents_mod, "_admin", paperclip_down)
    monkeypatch.setattr(agents_mod, "_ctx", paperclip_down)
    monkeypatch.setattr(agent_registry_repo, "list_agents", AsyncMock(return_value=ROWS))
    repo = MagicMock()
    repo.get_workspace = AsyncMock(return_value=SimpleNamespace(seeded_agent_ids=[CEO], member_user_ids={"user_sam"}))
    repo.resolve_owner_row = AsyncMock(return_value=None)
    repo.get_identity = AsyncMock(return_value=SimpleNamespace(paperclip_user_id="pcu_sam"))
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    monkeypatch.setattr(agents_mod, "_load_agent_sidecars", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "core.services.clerk_admin.list_organization_members",
        AsyncMock(
            return_value=[
                {"user_id": "user_sam", "first_name": "Sam", "last_name": "", "email": "s@x.test", "image_url": None}
            ]
        ),
    )
    app = FastAPI()
    app.include_router(agents_mod.router, prefix="/api/v1/teams")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_sam", org_id=WS)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_agents_come_from_the_registry_with_paperclip_down(client):
    r = await client.get("/api/v1/teams/agents")

    assert r.status_code == 200
    agents = {a["id"]: a for a in r.json()["agents"]}
    assert set(agents) == {CEO, GROWTH}  # deleted rows dropped
    ceo, growth = agents[CEO], agents[GROWTH]
    assert (ceo["name"], ceo["title"], ceo["capabilities"]) == ("Lightbulb", "Lead", "Runs it")  # front agent
    assert ceo["status"] == "idle" and growth["status"] == "paused"  # registry `active` reads as idle
    assert ceo["seeded"] is True and growth["seeded"] is False
    assert growth["mentionHandle"] == "growth" and ceo["mentionHandle"] == "lightbulb"


async def test_members_need_no_paperclip_context(client):
    r = await client.get("/api/v1/teams/members")

    assert r.status_code == 200
    assert r.json()["members"] == [
        {"userId": "user_sam", "name": "Sam", "email": "s@x.test", "imageUrl": None, "paperclipUserId": "pcu_sam"}
    ]


@pytest.mark.parametrize("path", ["heartbeat", "wakeup"])
async def test_heartbeat_and_wake_are_gone(client, path):
    r = await client.post(f"/api/v1/teams/agents/{CEO}/{path}", json={})
    assert r.status_code == 410
