"""C9: GET/PUT /teams/agents/{agentId}/voice-notes (agent settings)."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("WS_MANAGEMENT_API_URL", "https://test.execute-api.us-east-1.amazonaws.com/test")

from core.services import hermes_profiles  # noqa: E402
from routers.teams import agents as agents_mod  # noqa: E402
from core.auth import AuthContext, get_current_user  # noqa: E402

AGENT = "a-ceo"
KEY = f"{hermes_profiles.profile_prefix('org_1', AGENT)}{hermes_profiles.VOICE_NOTES_FILENAME}"


@pytest.fixture
def store(monkeypatch):
    files: dict[str, str] = {}
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: files.get(key))
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: files.__setitem__(key, body))
    return files


@pytest.fixture
def client(monkeypatch, store):
    from core.services import agent_registry

    monkeypatch.setattr(agent_registry, "roster", AsyncMock(return_value=[{"id": AGENT, "name": "Nova"}]))
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=SimpleNamespace(starter_agent_ids={"ceo": AGENT}))
    monkeypatch.setattr(agents_mod, "_repo", lambda: repo)
    monkeypatch.setattr(agents_mod.voice_calls, "send_note_saved", AsyncMock())
    app = FastAPI()
    app.include_router(agents_mod.router)
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_1", org_id="org_1")
    return TestClient(app)


def test_get_without_file_returns_registry_name_and_role_voice(client):
    r = client.get(f"/agents/{AGENT}/voice-notes")
    assert r.status_code == 200
    assert r.json() == {"name": "Nova", "notes": "", "voice": "marin"}


def test_put_over_1000_chars_is_422(client, store):
    r = client.put(f"/agents/{AGENT}/voice-notes", json={"notes": "x" * 1001})
    assert r.status_code == 422
    assert not store


def test_put_keeps_the_name_line_and_bumps_revision(client, store):
    r = client.put(f"/agents/{AGENT}/voice-notes", json={"notes": "Be blunt."})
    assert r.status_code == 200
    assert r.json() == {"name": "Nova", "notes": "Be blunt.", "voice": "marin"}
    assert store[KEY] == "Your name is Nova.\n\nBe blunt."
    assert any(k.endswith(".revision") for k in store)
    assert client.get(f"/agents/{AGENT}/voice-notes").json()["notes"] == "Be blunt."
    # C3: an open Live window hears that the note was saved, by whom.
    agents_mod.voice_calls.send_note_saved.assert_awaited_once_with("org_1", AGENT, "Nova", "Be blunt.", "user_1")


@pytest.mark.parametrize("method", ["get", "put"])
def test_agent_outside_the_company_is_404(client, store, method):
    kwargs = {"json": {"notes": "hi"}} if method == "put" else {}
    r = getattr(client, method)("/agents/a-elsewhere/voice-notes", **kwargs)
    assert r.status_code == 404
    assert not store
