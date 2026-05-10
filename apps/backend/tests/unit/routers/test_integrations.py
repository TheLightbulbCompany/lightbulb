"""Tests for the MCP server integrations router."""

import copy
from unittest.mock import patch

import pytest
from httpx import AsyncClient, ASGITransport

from core.auth import get_current_user, AuthContext


class TestIntegrationsRouter:
    """Test /integrations/mcp/servers CRUD endpoints."""

    @pytest.fixture
    def fake_config(self):
        """In-memory openclaw.json that the patched helpers operate on."""
        store = {"config": {"mcp": {"servers": {}}}}

        async def read_config(owner_id: str):
            # Deep-copy to mimic real I/O semantics — the router shouldn't
            # mutate the underlying store directly.
            return copy.deepcopy(store["config"])

        async def set_path(owner_id: str, path: list[str], value):
            cursor = store["config"]
            for seg in path[:-1]:
                if not isinstance(cursor.get(seg), dict):
                    cursor[seg] = {}
                cursor = cursor[seg]
            cursor[path[-1]] = copy.deepcopy(value)

        async def delete_path(owner_id: str, path: list[str]):
            cursor = store["config"]
            for seg in path[:-1]:
                if not isinstance(cursor.get(seg), dict):
                    return
                cursor = cursor[seg]
            cursor.pop(path[-1], None)

        return {
            "store": store,
            "read": read_config,
            "set": set_path,
            "delete": delete_path,
        }

    @pytest.fixture
    def patched_router(self, fake_config):
        """Patches all three config helpers used by integrations.py."""
        with (
            patch("routers.integrations.read_openclaw_config_from_efs", side_effect=fake_config["read"]),
            patch("routers.integrations.set_openclaw_config_path", side_effect=fake_config["set"]),
            patch("routers.integrations.delete_openclaw_config_path", side_effect=fake_config["delete"]),
        ):
            yield fake_config

    @pytest.fixture
    def auth_override(self):
        auth = AuthContext(user_id="user_integ_test")

        async def _mock():
            return auth

        return _mock

    @pytest.mark.asyncio
    async def test_list_empty_servers(self, app, patched_router, auth_override):
        """GET returns empty servers when openclaw.json has no mcp.servers."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/integrations/mcp/servers")
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200
        assert resp.json()["servers"] == {}

    @pytest.mark.asyncio
    async def test_put_replaces_servers(self, app, patched_router, auth_override):
        """PUT replaces the entire servers dict."""
        app.dependency_overrides[get_current_user] = auth_override
        servers = {
            "github": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_TOKEN": "ghp_test"},
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/v1/integrations/mcp/servers",
                json={"servers": servers},
            )
            assert resp.status_code == 200
            assert "github" in resp.json()["servers"]

            resp2 = await client.get("/api/v1/integrations/mcp/servers")
            assert resp2.json()["servers"]["github"]["command"] == "npx"
        app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_put_wipes_removed_entries(self, app, patched_router, auth_override):
        """PUT semantics: keys not present in the new dict are deleted."""
        app.dependency_overrides[get_current_user] = auth_override
        # Seed an existing server.
        patched_router["store"]["config"]["mcp"]["servers"] = {
            "old": {"command": "old-cmd"},
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/v1/integrations/mcp/servers",
                json={"servers": {"new": {"command": "new-cmd"}}},
            )
            assert resp.status_code == 200
            body = resp.json()["servers"]
            assert "new" in body
            assert "old" not in body, "PUT must overwrite, not deep-merge"
        app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_patch_adds_server(self, app, patched_router, auth_override):
        """PATCH adds a single server entry."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/v1/integrations/mcp/servers/linear",
                json={"command": "npx", "args": ["-y", "@linear/mcp"], "env": {}},
            )
            assert resp.status_code == 200
            assert "linear" in resp.json()["servers"]
        app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_patch_preserves_other_servers(self, app, patched_router, auth_override):
        """PATCH must not affect other server entries."""
        app.dependency_overrides[get_current_user] = auth_override
        patched_router["store"]["config"]["mcp"]["servers"] = {
            "existing": {"command": "keep"},
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.patch(
                "/api/v1/integrations/mcp/servers/added",
                json={"command": "node", "args": [], "env": {}},
            )
            resp = await client.get("/api/v1/integrations/mcp/servers")
            servers = resp.json()["servers"]
            assert "existing" in servers, "PATCH must not delete other servers"
            assert "added" in servers
        app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_patch_replaces_entry_clears_env(self, app, patched_router, auth_override):
        """PATCH does a full overwrite of the named server entry — passing env: {}
        wipes any prior env vars (regression guard for the deep-merge bug fixed
        in commit 01c19bf5).
        """
        app.dependency_overrides[get_current_user] = auth_override
        patched_router["store"]["config"]["mcp"]["servers"] = {
            "linear": {"command": "npx", "args": [], "env": {"LINEAR_TOKEN": "stale"}},
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/v1/integrations/mcp/servers/linear",
                json={"command": "npx", "args": [], "env": {}},
            )
            assert resp.status_code == 200
            servers = resp.json()["servers"]
            assert servers["linear"]["env"] == {}, "PATCH with env: {} must wipe old env vars"
        app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_delete_removes_server(self, app, patched_router, auth_override):
        """DELETE removes a server entry."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.patch(
                "/api/v1/integrations/mcp/servers/to-remove",
                json={"command": "echo", "args": [], "env": {}},
            )
            resp = await client.delete("/api/v1/integrations/mcp/servers/to-remove")
            assert resp.status_code == 200
            assert "to-remove" not in resp.json()["servers"]
        app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_put_rejects_invalid_server(self, app, patched_router, auth_override):
        """PUT rejects servers without command field."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/v1/integrations/mcp/servers",
                json={"servers": {"bad": {"args": ["test"]}}},
            )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_unauthenticated_request_rejected(self, app, patched_router):
        """Requests without auth are rejected."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/integrations/mcp/servers")
        assert resp.status_code == 401 or resp.status_code == 403

    @pytest.mark.asyncio
    async def test_round_trip_crud(self, app, patched_router, auth_override):
        """Full CRUD round-trip: add, read, update, delete."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/v1/integrations/mcp/servers/test-server",
                json={"command": "node", "args": ["server.js"], "env": {"KEY": "val"}},
            )
            assert resp.status_code == 200
            assert resp.json()["servers"]["test-server"]["command"] == "node"

            resp = await client.get("/api/v1/integrations/mcp/servers")
            assert "test-server" in resp.json()["servers"]

            resp = await client.patch(
                "/api/v1/integrations/mcp/servers/test-server",
                json={"command": "bun", "args": ["run", "server.ts"], "env": {}},
            )
            assert resp.json()["servers"]["test-server"]["command"] == "bun"

            resp = await client.delete("/api/v1/integrations/mcp/servers/test-server")
            assert "test-server" not in resp.json()["servers"]
        app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_container_not_ready_returns_503(self, app, auth_override):
        """When the patcher raises ConfigPatchError (container not provisioned),
        the endpoint returns 503 with a structured detail."""
        from core.services.config_patcher import ConfigPatchError

        async def _raise(*args, **kwargs):
            raise ConfigPatchError("Config not found for owner ...")

        app.dependency_overrides[get_current_user] = auth_override
        with (
            patch("routers.integrations.read_openclaw_config_from_efs", return_value=None),
            patch("routers.integrations.set_openclaw_config_path", side_effect=_raise),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.patch(
                    "/api/v1/integrations/mcp/servers/x",
                    json={"command": "y", "args": [], "env": {}},
                )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 503
        assert "container_not_ready" in resp.json()["detail"]
