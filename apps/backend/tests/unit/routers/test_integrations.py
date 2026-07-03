"""Tests for the MCP server integrations router."""

import copy
from unittest.mock import AsyncMock, patch

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
    async def test_put_servers_reasserts_connected_integration(self, app, patched_router, auth_override):
        """A whole-dict PUT that omits an integration-owned key (e.g. `stripe`)
        would drop it. The handler must re-assert integration-owned servers
        after the user's write so a manual edit can't clobber a connected
        integration's MCP block.
        """
        app.dependency_overrides[get_current_user] = auth_override
        with patch("routers.integrations.reconcile_integrations", AsyncMock()) as reconcile:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put(
                    "/api/v1/integrations/mcp/servers",
                    json={
                        "servers": {
                            "my-custom": {"command": "node", "args": ["x.js"], "env": {}},
                        }
                    },
                )
            assert resp.status_code == 200
            reconcile.assert_awaited_once_with("user_integ_test")
        app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_put_response_reflects_reconciled_on_disk_state(self, app, patched_router, auth_override):
        """The PUT response must mirror the post-reconcile on-disk state, not the
        submitted body. After reconcile re-asserts integration-owned keys (e.g.
        `stripe`), returning `body.servers` would omit `stripe` and misrepresent
        what's actually in openclaw.json. Re-read like PATCH does.
        """
        app.dependency_overrides[get_current_user] = auth_override
        reread = {
            "stripe": {"command": "npx", "args": ["-y", "@stripe/mcp"], "env": {}},
            "my-custom": {"command": "node", "args": ["x.js"], "env": {}},
        }
        with (
            patch("routers.integrations.reconcile_integrations", AsyncMock()),
            patch("routers.integrations._read_mcp_servers", AsyncMock(return_value=reread)),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put(
                    "/api/v1/integrations/mcp/servers",
                    json={"servers": {"my-custom": {"command": "node", "args": ["x.js"], "env": {}}}},
                )
            assert resp.status_code == 200
            body = resp.json()["servers"]
            assert body == reread
            assert "stripe" in body, "PUT response must include reconcile-reasserted integration keys"
        app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_delete_reasserts_connected_integration(self, app, patched_router, auth_override):
        """Deleting an integration-owned key via the manual panel must not silently
        break a still-connected integration. DELETE is a manual-panel write like
        PUT/PATCH, so it re-asserts integration-owned keys and the response mirrors
        the post-reconcile on-disk state (`stripe` still present).
        """
        app.dependency_overrides[get_current_user] = auth_override
        reread = {"stripe": {"command": "npx", "args": ["-y", "@stripe/mcp"], "env": {}}}
        with (
            patch("routers.integrations.reconcile_integrations", AsyncMock()) as reconcile,
            patch("routers.integrations._read_mcp_servers", AsyncMock(return_value=reread)),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.delete("/api/v1/integrations/mcp/servers/stripe")
            assert resp.status_code == 200
            reconcile.assert_awaited_once()
            assert resp.json()["servers"] == reread
            assert "stripe" in resp.json()["servers"], "DELETE must re-assert integration-owned keys"
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
    async def test_put_rejects_args_as_string(self, app, patched_router, auth_override):
        """PUT rejects entries whose args are not a list of strings.

        Regression: a naive validator that only checks that command is a
        string lets a string args field land in openclaw.json and silently
        break OpenClaw's hot reload.
        """
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/v1/integrations/mcp/servers",
                json={"servers": {"bad": {"command": "npx", "args": "--stdio"}}},
            )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 422
        assert "bad" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_put_rejects_env_with_non_string_values(self, app, patched_router, auth_override):
        """PUT rejects entries whose env contains non-string values.

        Same regression guard as above — env: {"TOKEN": 1} would have
        slipped through pre-fix validation.
        """
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/v1/integrations/mcp/servers",
                json={"servers": {"bad": {"command": "npx", "env": {"TOKEN": 1}}}},
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
    async def test_list_servers_org_admin_allowed(self, app, patched_router):
        """Org admins can read the shared MCP server list."""

        async def _admin_auth():
            return AuthContext(user_id="user_admin", org_id="org_1", org_role="org:admin")

        app.dependency_overrides[get_current_user] = _admin_auth
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/integrations/mcp/servers")
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_list_servers_org_member_rejected(self, app, patched_router):
        """Non-admin org members can't read the shared MCP server list — the
        entries carry per-server `env` secrets only the admin set up.
        Mirrors the gate on the WRITE endpoints + GET /config.
        """

        async def _member_auth():
            return AuthContext(user_id="user_member", org_id="org_1", org_role="org:member")

        app.dependency_overrides[get_current_user] = _member_auth
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/integrations/mcp/servers")
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 403

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

    @pytest.mark.asyncio
    async def test_put_injects_stdio_connect_timeout_default(self, app, patched_router, auth_override):
        """A user-added stdio server gets connectionTimeoutMs=120000 injected —
        npx cold starts on the 0.5-vCPU container reliably exceed OpenClaw's
        default 30s connect timeout, which previously made every user-added npx
        server fail its catalog connect while appearing configured."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/v1/integrations/mcp/servers",
                json={"servers": {"linear": {"command": "npx", "args": ["-y", "@linear/mcp"], "env": {}}}},
            )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200
        stored = patched_router["store"]["config"]["mcp"]["servers"]["linear"]
        assert stored["connectionTimeoutMs"] == 120000

    @pytest.mark.asyncio
    async def test_patch_respects_explicit_timeout(self, app, patched_router, auth_override):
        """An explicit user-provided timeout is never overridden by the default."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/v1/integrations/mcp/servers/fast",
                json={"command": "fast-mcp", "args": [], "env": {}, "connectionTimeoutMs": 5000},
            )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200
        stored = patched_router["store"]["config"]["mcp"]["servers"]["fast"]
        assert stored["connectionTimeoutMs"] == 5000

    @pytest.mark.asyncio
    async def test_patch_accepts_remote_server_and_injects_request_timeout(self, app, patched_router, auth_override):
        """A remote (transport+url) server is a valid entry and gets
        requestTimeoutMs=60000 injected, lifting OpenClaw's 1.5s catalog
        tools/list cap that silently excludes slow-listing remote servers.
        The stored block must not carry null command (a literal null fails
        OpenClaw's config schema on hot reload)."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/v1/integrations/mcp/servers/notion",
                json={"transport": "streamable-http", "url": "https://mcp.notion.com/mcp"},
            )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200
        stored = patched_router["store"]["config"]["mcp"]["servers"]["notion"]
        assert stored["url"] == "https://mcp.notion.com/mcp"
        assert stored["requestTimeoutMs"] == 60000
        assert "command" not in stored

    @pytest.mark.asyncio
    async def test_put_rejects_entry_with_both_shapes(self, app, patched_router, auth_override):
        """An entry that is both stdio and remote is ambiguous — reject at the API
        rather than letting OpenClaw's hot reload fail on it."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/v1/integrations/mcp/servers",
                json={
                    "servers": {
                        "weird": {
                            "command": "npx",
                            "transport": "streamable-http",
                            "url": "https://h/mcp",
                        }
                    }
                },
            )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_put_strips_explicit_nulls_on_unset_shape(self, app, patched_router, auth_override):
        """A PUT body carrying explicit nulls for the unset shape's keys must not
        store them: a literal `transport: null` in openclaw.json fails OpenClaw's
        config schema on hot reload, which bricks the container. The stored entry
        is rebuilt from the validated model, never the raw client dict."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/v1/integrations/mcp/servers",
                json={
                    "servers": {
                        "linear": {
                            "command": "npx",
                            "args": ["-y", "@linear/mcp"],
                            "env": {},
                            "transport": None,
                            "url": None,
                        }
                    }
                },
            )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200
        stored = patched_router["store"]["config"]["mcp"]["servers"]["linear"]
        assert "transport" not in stored
        assert "url" not in stored
        assert stored["connectionTimeoutMs"] == 120000

    @pytest.mark.asyncio
    async def test_put_remote_entry_normalized_like_patch(self, app, patched_router, auth_override):
        """PUT and PATCH must store the same shape for a remote entry: request
        timeout injected, headers preserved, and no stdio artifacts (command,
        empty args/env) in the block."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/v1/integrations/mcp/servers",
                json={
                    "servers": {
                        "context7": {
                            "transport": "streamable-http",
                            "url": "https://mcp.context7.com/mcp",
                            "headers": {"X-API-Key": "k"},
                        }
                    }
                },
            )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200
        stored = patched_router["store"]["config"]["mcp"]["servers"]["context7"]
        assert stored["requestTimeoutMs"] == 60000
        assert stored["headers"] == {"X-API-Key": "k"}
        assert "command" not in stored
        assert "args" not in stored
        assert "env" not in stored

    @pytest.mark.asyncio
    async def test_put_rejects_stdio_entry_with_partial_remote_fields(self, app, patched_router, auth_override):
        """A stdio entry carrying a stray remote field (url without transport, or
        transport without url) is a mixed shape that would fail OpenClaw's hot
        reload — reject the partial overlap, not just the complete one."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for stray in ({"url": "https://x/mcp"}, {"transport": "streamable-http"}):
                resp = await client.put(
                    "/api/v1/integrations/mcp/servers",
                    json={"servers": {"weird": {"command": "npx", **stray}}},
                )
                assert resp.status_code == 422, stray
        app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_put_rejects_empty_string_shape_fields(self, app, patched_router, auth_override):
        """Empty-string command/transport/url must 422: they are never valid, and
        a falsy-but-present value would otherwise slip past the shape checks and
        write a mixed entry into openclaw.json."""
        app.dependency_overrides[get_current_user] = auth_override
        bodies = [
            # command:"" alongside a complete remote shape
            {"command": "", "transport": "streamable-http", "url": "https://x/mcp"},
            # stray transport:"" on a stdio entry
            {"command": "npx", "transport": ""},
            # url:"" alone
            {"transport": "streamable-http", "url": ""},
        ]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for body in bodies:
                resp = await client.put(
                    "/api/v1/integrations/mcp/servers",
                    json={"servers": {"weird": body}},
                )
                assert resp.status_code == 422, body
        app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_put_rejects_remote_entry_with_stray_args_or_env(self, app, patched_router, auth_override):
        """The symmetric direction: a remote entry carrying non-empty stdio fields
        (args/env) would write a mixed shape into openclaw.json — reject it."""
        app.dependency_overrides[get_current_user] = auth_override
        base = {"transport": "streamable-http", "url": "https://x/mcp"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for stray in ({"args": ["--flag"]}, {"env": {"K": "v"}}):
                resp = await client.put(
                    "/api/v1/integrations/mcp/servers",
                    json={"servers": {"weird": {**base, **stray}}},
                )
                assert resp.status_code == 422, stray
        app.dependency_overrides.pop(get_current_user, None)
