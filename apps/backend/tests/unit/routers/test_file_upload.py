"""Tests for the agent file-upload endpoint (POST /container/files)."""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from core.auth import get_current_user, AuthContext


@pytest.fixture
def profile_writes(monkeypatch):
    """Capture what would land in the agent's profile."""
    calls = []

    def _write(owner_id, agent_id, files, removed):
        calls.append((owner_id, agent_id, files, removed))

    monkeypatch.setattr("core.services.hermes_profiles.write_profile_subtree", _write)
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value={"owner_id": "user_123"})
    )
    return calls


@pytest.fixture
def auth_override():
    auth = AuthContext(user_id="human_123", org_id="user_123", org_role="org:admin")

    async def _override():
        return auth

    return _override


AGENT_ID = "agent-xyz"


async def _upload(app, files):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"/api/v1/container/files?agent_id={AGENT_ID}", files=files)


@pytest.mark.asyncio
async def test_upload_single_file_lands_in_the_agents_profile_home(app, auth_override, profile_writes):
    """The object key is profile-relative (`home/uploads/...`); the path handed back is
    relative to the turn's `$HOME`, which IS that `home/` dir -- otherwise the notice the
    composer prepends names a path the agent cannot open."""
    app.dependency_overrides[get_current_user] = auth_override
    try:
        resp = await _upload(app, [("files", ("test.txt", b"hello world", "text/plain"))])

        assert resp.status_code == 200
        body = resp.json()
        assert body["uploaded"] == [{"filename": "test.txt", "path": "uploads/test.txt", "size": 11}]
        assert profile_writes == [("user_123", AGENT_ID, {"home/uploads/test.txt": b"hello world"}, ())]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_upload_multiple_files_is_one_revision_bump(app, auth_override, profile_writes):
    """One subtree write for the request: `write_profile_subtree` bumps `.revision` last,
    and two bumps would make the bridge re-pull twice for one upload."""
    app.dependency_overrides[get_current_user] = auth_override
    try:
        resp = await _upload(
            app,
            [
                ("files", ("a.txt", b"aaa", "text/plain")),
                ("files", ("b.csv", b"1,2,3", "text/csv")),
            ],
        )

        assert resp.status_code == 200
        assert len(resp.json()["uploaded"]) == 2
        assert len(profile_writes) == 1
        assert set(profile_writes[0][2]) == {"home/uploads/a.txt", "home/uploads/b.csv"}
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_upload_rejects_oversized_file(app, auth_override, profile_writes):
    """Upload rejects files larger than 10MB."""
    app.dependency_overrides[get_current_user] = auth_override
    try:
        big_data = b"x" * (10 * 1024 * 1024 + 1)
        resp = await _upload(app, [("files", ("big.bin", big_data, "application/octet-stream"))])

        assert resp.status_code == 400
        assert "10MB" in resp.json()["detail"]
        assert profile_writes == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_upload_no_workspace(app, auth_override, profile_writes, monkeypatch):
    """404 when the owner has no workspace row at all."""
    app.dependency_overrides[get_current_user] = auth_override
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None))
    try:
        resp = await _upload(app, [("files", ("test.txt", b"hello", "text/plain"))])

        assert resp.status_code == 404
        assert profile_writes == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_upload_sanitizes_filename(app, auth_override, profile_writes):
    """Upload sanitizes filenames with path traversal attempts."""
    app.dependency_overrides[get_current_user] = auth_override
    try:
        resp = await _upload(app, [("files", ("../../etc/passwd", b"nope", "text/plain"))])

        assert resp.status_code == 200
        name = resp.json()["uploaded"][0]["filename"]
        assert ".." not in name and "/" not in name
        assert list(profile_writes[0][2]) == [f"home/uploads/{name}"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_upload_missing_agent_id_returns_422(app, auth_override):
    """Upload without agent_id query param fails FastAPI validation with 422."""
    app.dependency_overrides[get_current_user] = auth_override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/container/files",
                files=[("files", ("test.txt", b"hi", "text/plain"))],
            )

        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_upload_traversal_agent_id_returns_400(app, auth_override):
    """Upload with agent_id containing a traversal sequence is rejected with 400."""
    app.dependency_overrides[get_current_user] = auth_override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/container/files?agent_id=../other",
                files=[("files", ("test.txt", b"hi", "text/plain"))],
            )

        assert resp.status_code == 400
        assert "Invalid agent_id" in resp.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)
