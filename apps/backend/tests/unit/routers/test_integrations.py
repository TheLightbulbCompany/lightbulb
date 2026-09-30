"""Tests for the MCP server integrations router."""

import copy
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from core.auth import get_current_user, AuthContext


class TestIntegrationsRouter:
    """Test /integrations/mcp/servers CRUD endpoints."""

    @pytest.fixture(autouse=True)
    def _no_connected_integrations_by_default(self):
        """PUT/PATCH now check integration_owned_server_keys (M6-R32), a real DynamoDB read
        (integrations_repo.list_for_owner) when unmocked. Default every test in this class to
        "nothing connected"; the collision tests below override it."""
        with patch("core.services.credentials.mcp_server.integrations_repo.list_for_owner", AsyncMock(return_value=[])):
            yield

    @pytest.fixture
    def patched_router(self):
        """In-memory stand-in for the reserved manual-MCP row, plus a no-op profile
        refresh. Yields ``{"store": {...}}`` so a test can read back what landed."""
        store: dict = {}

        async def get_manual(owner_id: str):
            return copy.deepcopy(store)

        async def set_manual(owner_id: str, servers: dict):
            store.clear()
            store.update(copy.deepcopy(servers))

        with (
            patch("routers.integrations.integrations_repo.get_manual_mcp_servers", get_manual),
            patch("routers.integrations.integrations_repo.set_manual_mcp_servers", set_manual),
            patch("routers.integrations.reconcile_integrations", AsyncMock()),
        ):
            yield {"store": store}

    @pytest.fixture
    def auth_override(self):
        auth = AuthContext(user_id="human_integ_test", org_id="user_integ_test", org_role="org:admin")

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
        assert resp.json()["managed_keys"] == []

    @pytest.mark.asyncio
    async def test_list_marks_connected_provider_servers_as_managed(self, app, patched_router, auth_override):
        app.dependency_overrides[get_current_user] = auth_override
        patched_router["store"]["custom"] = {"command": "npx"}
        with patch(
            "core.services.credentials.mcp_server.integrations_repo.list_for_owner",
            AsyncMock(return_value=[{"provider": "posthog", "status": "connected"}]),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/v1/integrations/mcp/servers")
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200
        assert set(resp.json()["servers"]) == {"custom", "posthog"}
        assert resp.json()["managed_keys"] == ["posthog"]

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
        patched_router["store"].update({"old": {"command": "old-cmd"}})
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
        patched_router["store"].update({"existing": {"command": "keep"}})
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
        patched_router["store"].update({"linear": {"command": "npx", "args": [], "env": {"LINEAR_TOKEN": "stale"}}})
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
    async def test_put_rejects_sanitized_name_collision(self, app, patched_router, auth_override):
        """M6-R26 fix round 1, minor: 'acme-crm' and 'acme_crm' both sanitize
        to MCP_ACME_CRM_* -- today that only raises much later, deep inside
        hermes_profiles.translate_mcp_servers, swallowed by an AgentCore
        write's best-effort profile refresh (a silent no-op behind a 200 OK).
        Reject it here instead, at submission time."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/v1/integrations/mcp/servers",
                json={
                    "servers": {
                        "acme-crm": {"command": "npx", "args": [], "env": {"TOKEN": "a"}},
                        "acme_crm": {"command": "npx", "args": [], "env": {"TOKEN": "b"}},
                    }
                },
            )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 422
        assert "acme-crm" in resp.json()["detail"] or "acme_crm" in resp.json()["detail"]

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
        stored = patched_router["store"]["linear"]
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
        stored = patched_router["store"]["fast"]
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
        stored = patched_router["store"]["notion"]
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
        stored = patched_router["store"]["linear"]
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
        stored = patched_router["store"]["context7"]
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

    @pytest.mark.asyncio
    async def test_patch_respects_explicit_remote_request_timeout(self, app, patched_router, auth_override):
        """Explicit-value-wins must hold for the remote shape too, not just stdio."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/v1/integrations/mcp/servers/slowlist",
                json={"transport": "streamable-http", "url": "https://x/mcp", "requestTimeoutMs": 90000},
            )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200
        stored = patched_router["store"]["slowlist"]
        assert stored["requestTimeoutMs"] == 90000

    @pytest.mark.asyncio
    async def test_put_rejects_stdio_entry_with_stray_headers(self, app, patched_router, auth_override):
        """headers is a remote-only field; a stdio entry carrying it is a mixed shape."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/v1/integrations/mcp/servers",
                json={"servers": {"weird": {"command": "npx", "headers": {"X-K": "v"}}}},
            )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_put_passes_through_unmodeled_openclaw_keys(self, app, patched_router, auth_override):
        """OpenClaw's per-server schema is a catchall with keys this API doesn't
        enumerate (toolFilter, supportsParallelToolCalls, ...) — they must pass
        through to the stored block, neither rejected nor silently dropped."""
        app.dependency_overrides[get_current_user] = auth_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/v1/integrations/mcp/servers",
                json={
                    "servers": {
                        "big-remote": {
                            "transport": "streamable-http",
                            "url": "https://x/mcp",
                            "toolFilter": {"include": ["query-*"]},
                        }
                    }
                },
            )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200
        stored = patched_router["store"]["big-remote"]
        assert stored["toolFilter"] == {"include": ["query-*"]}

    # -------------------------------------------------------------------
    # Row #6: manual MCP servers on an AgentCore owner
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_put_agentcore_owner_stores_manual_and_refreshes_hermes(self, app, auth_override):
        """PUT on an AgentCore owner stores the manual bucket and refreshes
        Hermes profiles instead of touching openclaw.json."""
        app.dependency_overrides[get_current_user] = auth_override
        with (
            patch("routers.integrations.integrations_repo.set_manual_mcp_servers", AsyncMock()) as set_manual,
            patch(
                "routers.integrations.build_connected_integration_servers",
                AsyncMock(return_value={"github": {"command": "npx"}}),
            ),
            patch("routers.integrations.reconcile_integrations", AsyncMock()) as reconcile,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put(
                    "/api/v1/integrations/mcp/servers",
                    json={"servers": {"github": {"command": "npx", "args": [], "env": {"GITHUB_TOKEN": "ghp_x"}}}},
                )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200
        set_manual.assert_awaited_once()
        stored_servers = set_manual.await_args.args[1]
        assert "github" in stored_servers
        reconcile.assert_awaited_once()
        assert resp.json()["servers"] == {"github": {"command": "npx"}}

    @pytest.mark.asyncio
    async def test_patch_agentcore_owner_stores_manual_and_refreshes_hermes(self, app, auth_override):
        app.dependency_overrides[get_current_user] = auth_override
        with (
            patch("routers.integrations.integrations_repo.get_manual_mcp_servers", AsyncMock(return_value={})),
            patch("routers.integrations.integrations_repo.set_manual_mcp_servers", AsyncMock()) as set_manual,
            patch(
                "routers.integrations.build_connected_integration_servers",
                AsyncMock(return_value={"linear": {"command": "npx"}}),
            ),
            patch("routers.integrations.reconcile_integrations", AsyncMock()) as reconcile,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.patch(
                    "/api/v1/integrations/mcp/servers/linear",
                    json={"command": "npx", "args": ["-y", "@linear/mcp"], "env": {}},
                )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200
        set_manual.assert_awaited_once()
        stored_servers = set_manual.await_args.args[1]
        assert "linear" in stored_servers
        reconcile.assert_awaited_once()
        assert resp.json()["servers"] == {"linear": {"command": "npx"}}

    @pytest.mark.asyncio
    async def test_delete_agentcore_owner_pops_manual_and_refreshes_hermes(self, app, auth_override):
        app.dependency_overrides[get_current_user] = auth_override
        with (
            patch(
                "routers.integrations.integrations_repo.get_manual_mcp_servers",
                AsyncMock(return_value={"stale": {"command": "old"}}),
            ),
            patch("routers.integrations.integrations_repo.set_manual_mcp_servers", AsyncMock()) as set_manual,
            patch("routers.integrations.build_connected_integration_servers", AsyncMock(return_value={})),
            patch("routers.integrations.reconcile_integrations", AsyncMock()) as reconcile,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.delete("/api/v1/integrations/mcp/servers/stale")
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200
        set_manual.assert_awaited_once_with("user_integ_test", {})
        reconcile.assert_awaited_once()
        assert resp.json()["servers"] == {}

    @pytest.mark.asyncio
    async def test_put_agentcore_secret_lands_in_env_not_config(self, app, auth_override):
        """A header/env secret in a manually-added server never lands literally
        in the response's config shape -- it's stored raw here (the scrub to a
        ${VAR} ref happens downstream in translate_mcp_servers/
        build_connected_integration_servers, exercised in test_mcp_server.py),
        but the write path itself must go through the manual bucket, not
        openclaw.json."""
        app.dependency_overrides[get_current_user] = auth_override
        with (
            patch("routers.integrations.integrations_repo.set_manual_mcp_servers", AsyncMock()) as set_manual,
            patch("routers.integrations.build_connected_integration_servers", AsyncMock(return_value={})),
            patch("routers.integrations.reconcile_integrations", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put(
                    "/api/v1/integrations/mcp/servers",
                    json={
                        "servers": {
                            "acme": {
                                "transport": "streamable-http",
                                "url": "https://acme.example/mcp",
                                "headers": {"Authorization": "Bearer sk_live_x"},
                            }
                        }
                    },
                )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200
        stored_servers = set_manual.await_args.args[1]
        assert stored_servers["acme"]["headers"]["Authorization"] == "Bearer sk_live_x"

    @pytest.mark.asyncio
    async def test_get_agentcore_owner_reflects_state_across_two_puts(self, app, auth_override):
        """M6-R26 fix round 1, Critical 2: list_mcp_servers used to read EFS
        (always {} for AgentCore) and the frontend's McpServersTab merges
        {...previousServers, ...newlyWritten} on every write -- so a GET that
        always answered empty wiped every server that came before it. GET must
        reflect real, cumulative state across writes."""
        app.dependency_overrides[get_current_user] = auth_override
        store: dict = {}

        async def fake_get_manual(owner_id):
            return dict(store)

        async def fake_set_manual(owner_id, servers):
            store.clear()
            store.update(servers)

        with (
            patch("routers.integrations.integrations_repo.get_manual_mcp_servers", fake_get_manual),
            patch("routers.integrations.integrations_repo.set_manual_mcp_servers", fake_set_manual),
            patch(
                "core.services.credentials.mcp_server.integrations_repo.list_for_owner",
                AsyncMock(return_value=[]),
            ),
            patch("routers.integrations.reconcile_integrations", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp1 = await client.put(
                    "/api/v1/integrations/mcp/servers",
                    json={"servers": {"github": {"command": "npx", "args": [], "env": {}}}},
                )
                assert resp1.status_code == 200
                assert "github" in resp1.json()["servers"]

                resp2 = await client.put(
                    "/api/v1/integrations/mcp/servers",
                    json={
                        "servers": {
                            "github": {"command": "npx", "args": [], "env": {}},
                            "linear": {"command": "npx", "args": ["-y", "@linear/mcp"], "env": {}},
                        }
                    },
                )
                assert resp2.status_code == 200

                get_resp = await client.get("/api/v1/integrations/mcp/servers")
        app.dependency_overrides.pop(get_current_user, None)
        assert get_resp.status_code == 200
        servers = get_resp.json()["servers"]
        assert "github" in servers, "the FIRST write must survive a later GET"
        assert "linear" in servers

    # -----------------------------------------------------------------------
    # Ruling M6-R32 — a manual server sharing a key with a CONNECTED integration
    # is rejected at write time (409 naming the integration), instead of being
    # silently overwritten by the next reconcile/refresh and vanishing from the
    # response with no signal to the caller. Checked before either runtime branch.
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_put_rejects_a_manual_server_name_colliding_with_a_connected_integration(self, app, auth_override):
        app.dependency_overrides[get_current_user] = auth_override
        with patch(
            "core.services.credentials.mcp_server.integrations_repo.list_for_owner",
            AsyncMock(return_value=[{"provider": "stripe", "status": "connected"}]),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put(
                    "/api/v1/integrations/mcp/servers",
                    json={"servers": {"stripe": {"command": "npx", "args": [], "env": {}}}},
                )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 409
        assert "stripe" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_patch_rejects_a_manual_server_name_colliding_with_a_connected_integration(self, app, auth_override):
        app.dependency_overrides[get_current_user] = auth_override
        with patch(
            "core.services.credentials.mcp_server.integrations_repo.list_for_owner",
            AsyncMock(return_value=[{"provider": "stripe", "status": "connected"}]),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.patch(
                    "/api/v1/integrations/mcp/servers/stripe",
                    json={"command": "npx", "args": [], "env": {}},
                )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 409
        assert "stripe" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_put_allows_a_manual_server_name_not_owned_by_any_connected_integration(
        self, app, auth_override, patched_router
    ):
        """A collision check against a DIFFERENT connected provider must not block an
        unrelated manual server name."""
        app.dependency_overrides[get_current_user] = auth_override
        with (
            patch(
                "core.services.credentials.mcp_server.integrations_repo.list_for_owner",
                AsyncMock(return_value=[{"provider": "stripe", "status": "connected"}]),
            ),
            patch("routers.integrations.reconcile_integrations", AsyncMock()),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put(
                    "/api/v1/integrations/mcp/servers",
                    json={"servers": {"my-custom-server": {"command": "npx", "args": [], "env": {}}}},
                )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_put_agentcore_owner_also_rejects_the_collision(self, app, auth_override):
        """The check runs before the runtime branch, so it applies on AgentCore too."""
        app.dependency_overrides[get_current_user] = auth_override
        with (
            patch(
                "core.services.credentials.mcp_server.integrations_repo.list_for_owner",
                AsyncMock(return_value=[{"provider": "stripe", "status": "connected"}]),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put(
                    "/api/v1/integrations/mcp/servers",
                    json={"servers": {"stripe": {"command": "npx", "args": [], "env": {}}}},
                )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 409
        assert "stripe" in resp.json()["detail"]
