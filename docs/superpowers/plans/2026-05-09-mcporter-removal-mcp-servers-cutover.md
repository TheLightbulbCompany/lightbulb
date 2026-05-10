# mcporter Removal + MCP Servers Tab Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `openclaw.json → mcp.servers` the single canonical MCP registry. Rip out the half-wired mcporter scaffolding (which nothing downstream reads) and reroute the MCP Servers control-panel tab to the same place upstream's `openclaw mcp set/list/unset` CLI operates on.

**Architecture:** The Isol8 backend already patches `openclaw.json` from outside via `core/services/config_patcher.py` — file-locked, atomic-rename, chown to uid 1000, chokidar-friendly hot-reload. Channels, updates, catalog, and admin already use these helpers. The integrations router will join them. Reads use the existing `read_openclaw_config_from_efs(owner_id)` helper. No new RPCs, no UI changes.

**Tech Stack:** FastAPI + pytest (backend), `fcntl.lockf` (file locking), `pydantic` (request validation). Frontend untouched.

**Spec:** `docs/superpowers/specs/2026-05-09-mcporter-removal-mcp-servers-cutover-design.md`

---

## File Map

**Modify:**
- `apps/backend/core/services/config_patcher.py` — add `set_openclaw_config_path` helper (single small public function reusing `_locked_rmw`).
- `apps/backend/routers/integrations.py` — replace `_read_mcporter_config` / `_write_mcporter_config` and the four endpoints' bodies. Same request/response Pydantic models, same routes.
- `apps/backend/core/containers/workspace.py` — drop `.mcporter` from `_EXCLUDED_NAMES`; delete the default mcporter-write block in `ensure_user_dir`.
- `apps/backend/core/containers/config.py` — delete `write_mcporter_config`.
- `apps/backend/core/containers/ecs_manager.py` — drop the `write_mcporter_config` import + call; update the docstring listing files written.
- `apps/backend/tests/unit/containers/test_config.py` — drop `write_mcporter_config` test cases.
- `apps/backend/tests/unit/routers/test_integrations.py` — rewrite tests to mock `read_openclaw_config_from_efs` + `config_patcher` helpers.

**Delete:** Nothing whole — only blocks inside files.

**Create:** Nothing — `apps/backend/tests/unit/services/test_config_patcher_set_path.py` only if there isn't already coverage for `_locked_rmw`-style helpers (we'll check in Task 1).

---

## Task 1: Add `set_openclaw_config_path` helper

We need a "replace this leaf entirely" semantic for the PUT endpoint (replacing the whole `mcp.servers` dict — `patch_openclaw_config` deep-merges, which would preserve removed entries). The existing helpers cover append, remove-from-list, delete, deep-merge — none cover "set this path". Adding one keeps the integrations router simple and gives future patchers a clean primitive.

**Files:**
- Modify: `apps/backend/core/services/config_patcher.py:343` (add new function after `delete_openclaw_config_path`)
- Test: `apps/backend/tests/unit/services/test_config_patcher.py` (check if it exists; create if not)

- [ ] **Step 1: Find or create the test file**

```bash
ls apps/backend/tests/unit/services/test_config_patcher.py 2>/dev/null && echo EXISTS || echo MISSING
```

If MISSING, create `apps/backend/tests/unit/services/test_config_patcher.py` with:

```python
"""Tests for core.services.config_patcher helpers."""

import json
from pathlib import Path

import pytest

from core.services.config_patcher import (
    ConfigPatchError,
    set_openclaw_config_path,
)


@pytest.fixture
def efs_root(tmp_path, monkeypatch):
    """Point config_patcher at a temp EFS root."""
    monkeypatch.setattr(
        "core.services.config_patcher._efs_mount_path",
        str(tmp_path),
    )
    return tmp_path


def _seed_config(efs_root: Path, owner_id: str, config: dict) -> Path:
    user_dir = efs_root / owner_id
    user_dir.mkdir(parents=True, exist_ok=True)
    config_path = user_dir / "openclaw.json"
    config_path.write_text(json.dumps(config, indent=2))
    return config_path
```

If the file already exists, skip the boilerplate above and add the new tests below into the existing file.

- [ ] **Step 2: Write failing tests for `set_openclaw_config_path`**

Append to `test_config_patcher.py`:

```python
class TestSetOpenclawConfigPath:
    @pytest.mark.asyncio
    async def test_replaces_existing_leaf(self, efs_root):
        config_path = _seed_config(
            efs_root,
            "user_set_1",
            {"mcp": {"servers": {"old": {"command": "x"}}}},
        )
        await set_openclaw_config_path(
            "user_set_1",
            ["mcp", "servers"],
            {"new": {"command": "y"}},
        )
        result = json.loads(config_path.read_text())
        assert result["mcp"]["servers"] == {"new": {"command": "y"}}

    @pytest.mark.asyncio
    async def test_creates_missing_intermediates(self, efs_root):
        config_path = _seed_config(efs_root, "user_set_2", {})
        await set_openclaw_config_path(
            "user_set_2",
            ["mcp", "servers"],
            {"a": {"command": "b"}},
        )
        result = json.loads(config_path.read_text())
        assert result["mcp"]["servers"] == {"a": {"command": "b"}}

    @pytest.mark.asyncio
    async def test_overwrites_non_dict_intermediate(self, efs_root):
        """If an intermediate path segment is not a dict, replace it."""
        config_path = _seed_config(efs_root, "user_set_3", {"mcp": "stringy"})
        await set_openclaw_config_path(
            "user_set_3",
            ["mcp", "servers"],
            {"a": {"command": "b"}},
        )
        result = json.loads(config_path.read_text())
        assert result["mcp"] == {"servers": {"a": {"command": "b"}}}

    @pytest.mark.asyncio
    async def test_empty_path_raises(self, efs_root):
        _seed_config(efs_root, "user_set_4", {})
        with pytest.raises(ConfigPatchError):
            await set_openclaw_config_path("user_set_4", [], {"x": 1})

    @pytest.mark.asyncio
    async def test_missing_config_raises(self, efs_root):
        with pytest.raises(ConfigPatchError):
            await set_openclaw_config_path("nonexistent_owner", ["mcp", "servers"], {})

    @pytest.mark.asyncio
    async def test_noop_when_value_unchanged(self, efs_root):
        """Setting an identical value should not bump the file mtime."""
        config_path = _seed_config(
            efs_root,
            "user_set_5",
            {"mcp": {"servers": {"a": {"command": "b"}}}},
        )
        original_mtime = config_path.stat().st_mtime_ns
        await set_openclaw_config_path(
            "user_set_5",
            ["mcp", "servers"],
            {"a": {"command": "b"}},
        )
        assert config_path.stat().st_mtime_ns == original_mtime
```

- [ ] **Step 3: Run the new tests; confirm they fail**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_config_patcher.py::TestSetOpenclawConfigPath -v
```

Expected: ImportError (`set_openclaw_config_path` not exported from `config_patcher`).

- [ ] **Step 4: Implement `set_openclaw_config_path`**

Add to `apps/backend/core/services/config_patcher.py` immediately after `delete_openclaw_config_path` (around line 343):

```python
async def set_openclaw_config_path(
    owner_id: str,
    path: list[str],
    value: Any,
) -> None:
    """Replace the leaf at `path` with `value` in the owner's openclaw.json.

    Semantics:
    - Creates missing intermediate dicts.
    - If an intermediate segment exists but is not a dict, it is replaced
      with a dict (consistent with treating the path as authoritative).
    - No-op if the leaf already equals `value` (skips write + chokidar reload).
    - Same fcntl.lockf exclusive lock as the other helpers.

    Used for "replace this entire subtree" semantics that deep-merge cannot
    express (e.g. PUT /integrations/mcp/servers wipes any server not in the
    new dict).
    """
    if not path:
        raise ConfigPatchError("path must not be empty")

    def _mutate(current: dict) -> bool:
        cursor = current
        for segment in path[:-1]:
            existing = cursor.get(segment)
            if not isinstance(existing, dict):
                cursor[segment] = {}
            cursor = cursor[segment]

        leaf_key = path[-1]
        if cursor.get(leaf_key) == value:
            return False  # already the desired value, skip the write
        cursor[leaf_key] = copy.deepcopy(value)
        return True

    await _locked_rmw(owner_id, _mutate, f"set path={path}")
```

- [ ] **Step 5: Run the tests again; confirm they pass**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_config_patcher.py::TestSetOpenclawConfigPath -v
```

Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/config_patcher.py apps/backend/tests/unit/services/test_config_patcher.py
git commit -m "feat(config-patcher): add set_openclaw_config_path for replace-leaf semantics"
```

---

## Task 2: Rewrite `integrations.py` to use config_patcher (read path)

Swap the GET handler to read via `read_openclaw_config_from_efs` instead of mcporter.json. Keep the request/response shape identical.

**Files:**
- Modify: `apps/backend/routers/integrations.py` (entire file rewrite)
- Modify: `apps/backend/tests/unit/routers/test_integrations.py:13-43` (replace `mock_workspace` fixture with `mock_config` fixture)

- [ ] **Step 1: Rewrite the test fixtures and the GET test first**

Replace lines 1-53 of `apps/backend/tests/unit/routers/test_integrations.py` with:

```python
"""Tests for the MCP server integrations router."""

from unittest.mock import patch, MagicMock

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
            import copy
            return copy.deepcopy(store["config"])

        async def patch_config(owner_id: str, patch_dict: dict):
            import copy
            from core.services.config_patcher import _deep_merge

            store["config"] = _deep_merge(store["config"], copy.deepcopy(patch_dict))

        async def set_path(owner_id: str, path: list[str], value):
            import copy
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
            "patch": patch_config,
            "set": set_path,
            "delete": delete_path,
        }

    @pytest.fixture
    def patched_router(self, fake_config):
        """Patches all four config helpers used by integrations.py."""
        with (
            patch("routers.integrations.read_openclaw_config_from_efs", side_effect=fake_config["read"]),
            patch("routers.integrations.patch_openclaw_config", side_effect=fake_config["patch"]),
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
```

- [ ] **Step 2: Run the GET test; confirm it fails**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_integrations.py::TestIntegrationsRouter::test_list_empty_servers -v
```

Expected: FAIL — `read_openclaw_config_from_efs` is not yet imported in `routers/integrations.py`.

- [ ] **Step 3: Replace `apps/backend/routers/integrations.py` with the new implementation**

Full file replacement:

```python
"""MCP server integration management endpoints.

CRUD for OpenClaw-native MCP server definitions stored under ``mcp.servers``
in the owner's ``openclaw.json``. Equivalent to ``openclaw mcp list/show/set/unset``
inside the container — but operated from the Isol8 control panel via the
file-locked patcher in ``core.services.config_patcher``.

Reads use ``read_openclaw_config_from_efs``. Writes use the same atomic
file-lock + chown machinery as channels, updates, catalog, admin — every
patch triggers OpenClaw's chokidar-based hot reload, so changes take effect
without a container restart.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.containers.config import read_openclaw_config_from_efs
from core.services.config_patcher import (
    ConfigPatchError,
    delete_openclaw_config_path,
    patch_openclaw_config,
    set_openclaw_config_path,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class ServerEntry(BaseModel):
    command: str = Field(..., description="Command to run the MCP server")
    args: list[str] = Field(default_factory=list, description="Command arguments")
    env: Dict[str, str] = Field(default_factory=dict, description="Environment variables")


class ServersResponse(BaseModel):
    servers: Dict[str, Any]


class PutServersRequest(BaseModel):
    servers: Dict[str, Any] = Field(..., description="Complete servers dict to replace")


class PatchServerRequest(BaseModel):
    command: str = Field(..., description="Command to run the MCP server")
    args: list[str] = Field(default_factory=list, description="Command arguments")
    env: Dict[str, str] = Field(default_factory=dict, description="Environment variables")


def _validate_servers(servers: dict) -> None:
    """Validate that each server entry has at minimum a command field (string)."""
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=422,
                detail=f"Server '{name}' must be an object",
            )
        if "command" not in entry or not isinstance(entry["command"], str):
            raise HTTPException(
                status_code=422,
                detail=f"Server '{name}' must have a 'command' string field",
            )


async def _read_mcp_servers(owner_id: str) -> dict:
    """Return the current ``mcp.servers`` block, or empty dict if absent."""
    config = await read_openclaw_config_from_efs(owner_id)
    if not isinstance(config, dict):
        return {}
    mcp = config.get("mcp")
    if not isinstance(mcp, dict):
        return {}
    servers = mcp.get("servers")
    return servers if isinstance(servers, dict) else {}


@router.get(
    "/integrations/mcp/servers",
    summary="List configured MCP servers",
    description="Returns all MCP server entries from the owner's openclaw.json mcp.servers block.",
    response_model=ServersResponse,
    operation_id="list_mcp_servers",
    tags=["integrations"],
)
async def list_mcp_servers(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    servers = await _read_mcp_servers(owner_id)
    return {"servers": servers}


@router.put(
    "/integrations/mcp/servers",
    summary="Replace all MCP server configurations",
    description="Replaces the entire mcp.servers dict in the owner's openclaw.json.",
    response_model=ServersResponse,
    operation_id="replace_mcp_servers",
    tags=["integrations"],
)
async def replace_mcp_servers(
    body: PutServersRequest,
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    if auth.is_org_context:
        require_org_admin(auth)
    _validate_servers(body.servers)
    try:
        await set_openclaw_config_path(owner_id, ["mcp", "servers"], body.servers)
    except ConfigPatchError as exc:
        raise HTTPException(status_code=503, detail=f"container_not_ready: {exc}") from exc
    return {"servers": body.servers}


@router.patch(
    "/integrations/mcp/servers/{name}",
    summary="Add or update a single MCP server",
    description="Adds or updates a single MCP server entry by name (deep-merged into mcp.servers).",
    response_model=ServersResponse,
    operation_id="upsert_mcp_server",
    tags=["integrations"],
)
async def upsert_mcp_server(
    name: str,
    body: PatchServerRequest,
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    if auth.is_org_context:
        require_org_admin(auth)
    entry = body.model_dump()
    try:
        await patch_openclaw_config(owner_id, {"mcp": {"servers": {name: entry}}})
    except ConfigPatchError as exc:
        raise HTTPException(status_code=503, detail=f"container_not_ready: {exc}") from exc
    servers = await _read_mcp_servers(owner_id)
    return {"servers": servers}


@router.delete(
    "/integrations/mcp/servers/{name}",
    summary="Remove an MCP server",
    description="Removes an MCP server entry by name from the owner's openclaw.json mcp.servers.",
    response_model=ServersResponse,
    operation_id="delete_mcp_server",
    tags=["integrations"],
)
async def delete_mcp_server(
    name: str,
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    if auth.is_org_context:
        require_org_admin(auth)
    try:
        await delete_openclaw_config_path(owner_id, ["mcp", "servers", name])
    except ConfigPatchError as exc:
        raise HTTPException(status_code=503, detail=f"container_not_ready: {exc}") from exc
    servers = await _read_mcp_servers(owner_id)
    return {"servers": servers}
```

- [ ] **Step 4: Run the GET test again; confirm it passes**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_integrations.py::TestIntegrationsRouter::test_list_empty_servers -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/integrations.py apps/backend/tests/unit/routers/test_integrations.py
git commit -m "feat(integrations): route MCP servers tab to openclaw.json mcp.servers"
```

---

## Task 3: Update remaining `test_integrations.py` cases for the new backing store

Carry the existing PUT / PATCH / DELETE / round-trip / validation tests over to the new fixture. Same coverage, new dependencies.

**Files:**
- Modify: `apps/backend/tests/unit/routers/test_integrations.py` (replace remaining tests)

- [ ] **Step 1: Replace the rest of `test_integrations.py` (everything after `test_list_empty_servers`)**

Append to the `TestIntegrationsRouter` class:

```python
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
        """PATCH must deep-merge, not replace."""
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
            assert "existing" in servers
            assert "added" in servers
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
            patch("routers.integrations.patch_openclaw_config", side_effect=_raise),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.patch(
                    "/api/v1/integrations/mcp/servers/x",
                    json={"command": "y", "args": [], "env": {}},
                )
        app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 503
        assert "container_not_ready" in resp.json()["detail"]
```

- [ ] **Step 2: Run the full integrations test file; confirm all pass**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_integrations.py -v
```

Expected: all tests in `TestIntegrationsRouter` pass (count will depend on whether the file had other classes). The new tests are the 8 above plus `test_list_empty_servers` from Task 2 = 9 total in this class.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/tests/unit/routers/test_integrations.py
git commit -m "test(integrations): rewrite MCP servers tests against config_patcher backing store"
```

---

## Task 4: Strip mcporter from container provisioning

Now that the integrations router no longer reads or writes `.mcporter/mcporter.json`, the file is dead. Remove the writer, the helper, the import, and the workspace-tree exclusion.

**Files:**
- Modify: `apps/backend/core/containers/workspace.py:37-44` (drop `.mcporter` from `_EXCLUDED_NAMES`)
- Modify: `apps/backend/core/containers/workspace.py:149-181` (delete the default-mcporter write block in `ensure_user_dir`)
- Modify: `apps/backend/core/containers/config.py:779+` (delete `write_mcporter_config`)
- Modify: `apps/backend/core/containers/ecs_manager.py:23-29` (drop `write_mcporter_config` import)
- Modify: `apps/backend/core/containers/ecs_manager.py:1518` (update docstring listing files written)
- Modify: `apps/backend/core/containers/ecs_manager.py:1614` (delete the mcporter write call)
- Modify: `apps/backend/tests/unit/containers/test_config.py:275-300` (delete `write_mcporter_config` tests)

- [ ] **Step 1: Write a failing test that proves provisioning no longer touches `.mcporter`**

Add to `apps/backend/tests/unit/containers/test_workspace.py` (create the file or append, depending on whether one already exists — `ls apps/backend/tests/unit/containers/` to check):

```python
import os

import pytest

from core.containers.workspace import Workspace, _EXCLUDED_NAMES


def test_excluded_names_no_longer_includes_mcporter():
    """mcporter is gone — the directory should not be hidden any more
    (because we don't create it, but also so any orphaned file from a
    pre-cutover provision becomes visible to the user as plain workspace
    content rather than mysteriously hidden)."""
    assert ".mcporter" not in _EXCLUDED_NAMES


def test_ensure_user_dir_does_not_create_mcporter(tmp_path):
    """Provisioning must not write a default mcporter.json any more."""
    ws = Workspace(efs_mount_path=str(tmp_path))
    ws.ensure_user_dir("user_no_mcporter")
    user_dir = tmp_path / "user_no_mcporter"
    assert user_dir.exists()
    assert not (user_dir / ".mcporter").exists()
    assert not (user_dir / ".mcporter" / "mcporter.json").exists()
```

If `Workspace` doesn't accept an `efs_mount_path` kwarg, fall back to monkeypatching `core.containers.workspace.settings.EFS_MOUNT_PATH` to `str(tmp_path)` before constructing the workspace — match the pattern in any other workspace test. (Inspect `apps/backend/tests/unit/containers/test_workspace.py` if it exists; otherwise mirror the `test_config_patcher.py` `efs_root` monkeypatch fixture.)

- [ ] **Step 2: Run the test; confirm it fails**

```bash
cd apps/backend && uv run pytest tests/unit/containers/test_workspace.py -v -k "mcporter"
```

Expected: FAIL — `.mcporter` is in `_EXCLUDED_NAMES`, and `ensure_user_dir` still writes the default file.

- [ ] **Step 3: Drop `.mcporter` from `_EXCLUDED_NAMES`**

In `apps/backend/core/containers/workspace.py`, change lines 37-44 from:

```python
_EXCLUDED_NAMES: set[str] = {
    "openclaw.json",
    ".openclaw",
    "node_modules",
    "__pycache__",
    ".mcporter",
    ".git",
}
```

to:

```python
_EXCLUDED_NAMES: set[str] = {
    "openclaw.json",
    ".openclaw",
    "node_modules",
    "__pycache__",
    ".git",
}
```

- [ ] **Step 4: Delete the default mcporter write in `ensure_user_dir`**

In `apps/backend/core/containers/workspace.py`, lines 149-181, replace:

```python
    def ensure_user_dir(self, user_id: str) -> Path:
        """Create the user workspace directory if it does not exist.

        Also writes default config files (e.g. mcporter.json) if they
        are not already present.

        Returns:
            The Path to the user's workspace directory.

        Raises:
            WorkspaceError: On filesystem errors.
        """
        p = self.user_path(user_id)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Failed to create user directory for %s: %s", user_id, exc)
            raise WorkspaceError(
                f"Failed to create user directory for {user_id}: {exc}",
                user_id=user_id,
            ) from exc

        # Write default mcporter config if not already present
        mcporter_path = p / ".mcporter" / "mcporter.json"
        if not mcporter_path.exists():
            try:
                mcporter_path.parent.mkdir(parents=True, exist_ok=True)
                mcporter_path.write_text('{\n  "servers": {}\n}\n', encoding="utf-8")
                os.chmod(mcporter_path, 0o600)
            except OSError as exc:
                logger.warning("Failed to write default mcporter.json for %s: %s", user_id, exc)

        return p
```

with:

```python
    def ensure_user_dir(self, user_id: str) -> Path:
        """Create the user workspace directory if it does not exist.

        Returns:
            The Path to the user's workspace directory.

        Raises:
            WorkspaceError: On filesystem errors.
        """
        p = self.user_path(user_id)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Failed to create user directory for %s: %s", user_id, exc)
            raise WorkspaceError(
                f"Failed to create user directory for {user_id}: {exc}",
                user_id=user_id,
            ) from exc

        return p
```

- [ ] **Step 5: Run the new tests; confirm they pass**

```bash
cd apps/backend && uv run pytest tests/unit/containers/test_workspace.py -v -k "mcporter"
```

Expected: 2 passed.

- [ ] **Step 6: Delete `write_mcporter_config` from `core/containers/config.py`**

In `apps/backend/core/containers/config.py`, locate `write_mcporter_config` (starts at line 779) and delete the entire function. The function ends at the next top-level `async def` or `def`. Use `grep -n "^def \|^async def " apps/backend/core/containers/config.py | grep -A1 "write_mcporter_config"` to see the exact boundary.

- [ ] **Step 7: Delete `write_mcporter_config` import + call in `ecs_manager.py`**

In `apps/backend/core/containers/ecs_manager.py`:

1. Remove `write_mcporter_config,` from the import block (line 27).
2. Update the docstring at lines 1508-1521. Replace the bullet:
   ```
   - `.mcporter/mcporter.json` — MCP server registry (currently empty)
   ```
   with nothing — just delete that line. The docstring keeps the other three bullets.
3. Delete line 1614:
   ```python
   workspace.write_file(owner_id, ".mcporter/mcporter.json", write_mcporter_config())
   ```

- [ ] **Step 8: Delete `write_mcporter_config` tests**

In `apps/backend/tests/unit/containers/test_config.py`:

1. Remove `write_mcporter_config,` from the import block at line 13.
2. Delete the test class containing `write_mcporter_config` tests at lines 275-300. Confirm the boundary by reading the file: the block likely starts with `class TestWriteMcporterConfig:` (or a free function decorated with `@pytest`) and ends just before the next class.

- [ ] **Step 9: Run the affected test files; confirm they still pass**

```bash
cd apps/backend && uv run pytest tests/unit/containers/test_config.py tests/unit/containers/test_workspace.py tests/unit/routers/test_integrations.py -v
```

Expected: all green. No `write_mcporter_config`-related tests remain; the workspace and integrations tests pass against the new state.

- [ ] **Step 10: Commit**

```bash
git add apps/backend/core/containers/workspace.py \
        apps/backend/core/containers/config.py \
        apps/backend/core/containers/ecs_manager.py \
        apps/backend/tests/unit/containers/test_config.py \
        apps/backend/tests/unit/containers/test_workspace.py
git commit -m "chore: rip out dead mcporter scaffolding from provisioning"
```

---

## Task 5: Final verification + manual smoke

Per the user's `feedback_run_tests_at_end.md` memory, the full suite runs only here.

- [ ] **Step 1: Run the full backend test suite**

```bash
cd apps/backend && uv run pytest tests/ -v
```

Expected: all green. Pay attention to any test outside the touched modules that imports `write_mcporter_config` or references `.mcporter/mcporter.json` — grep to confirm none remain:

```bash
grep -rn "write_mcporter_config\|\\.mcporter/mcporter\\.json\|MCPORTER_CONFIG_PATH" apps/backend
```

Expected: zero hits.

- [ ] **Step 2: Run lint**

```bash
turbo run lint --filter=@isol8/backend
```

Expected: no new lint errors.

- [ ] **Step 3: Verify nothing else references `.mcporter` across the repo**

```bash
grep -rn "\\.mcporter" apps/ --include="*.py" --include="*.ts" --include="*.tsx"
```

Expected: zero hits (the only legitimate match before this PR was in `workspace.py` and `integrations.py` — both touched).

- [ ] **Step 4: Manual smoke (dev environment, optional but recommended)**

Per CLAUDE.md, the user will deploy via `git push` → GitHub Actions backend deploy. After deploy completes and a fresh provision is done:

1. Open the MCP Servers tab in the dev control panel.
2. Add a server (e.g. `linear`, command `npx`, args `["-y", "@linear/mcp"]`).
3. Inside the container, run `openclaw mcp list` — confirm the server is listed.
4. Confirm chokidar hot-reload picked it up (no container restart needed): the agent's tool catalog should show the new MCP tools without a redeploy.
5. Delete the server from the UI; confirm `openclaw mcp list` shows it removed.

If hot-reload didn't pick it up, that's an OpenClaw-config issue (chokidar polling isn't enabled or the path mapping is off) — file a separate issue rather than reverting this PR.

- [ ] **Step 5: Push branch and open PR**

Note the user's memories: `feedback_no_push_without_approval.md` (don't push without explicit approval) and `feedback_always_pr_never_direct_main.md` (always cut a PR). Wait for user approval before pushing.

When approved:

```bash
git push -u origin <branch-name>
gh pr create --title "Drop dead mcporter scaffolding; route MCP Servers tab to OpenClaw native" --body "$(cat <<'EOF'
## Summary
- MCP Servers control-panel tab now reads/writes `openclaw.json → mcp.servers` directly via the existing `config_patcher` machinery (file-locked, atomic-rename, chokidar-friendly hot-reload). No more dead `.mcporter/mcporter.json`.
- Provisioning no longer creates `.mcporter/`. `write_mcporter_config` is gone.
- New `set_openclaw_config_path` helper added to `config_patcher` for replace-leaf semantics (used by PUT).

## Test plan
- [x] `apps/backend` unit tests pass (full suite)
- [x] `apps/backend` lint clean
- [ ] Manual smoke on dev: add a server in the UI, verify with `openclaw mcp list` inside the container, confirm hot-reload picks it up

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review checklist

- [x] **Spec coverage:** Every section of the spec maps to a task. Backing store swap → Task 2. Provisioning cleanup → Task 4. Failure modes → Task 3 (`test_container_not_ready_returns_503`). Test plan → Tasks 2/3/4/5.
- [x] **Placeholder scan:** No "TBD"/"TODO"/"add appropriate handling" patterns. Every code change has the exact code.
- [x] **Type consistency:** `set_openclaw_config_path(owner_id, path, value)` signature is identical between Task 1 (definition) and Task 2 (caller). `_read_mcp_servers(owner_id) -> dict` matches caller usage.
- [x] **No new files unless needed:** Only added test files under `tests/unit/services/test_config_patcher.py` and possibly `tests/unit/containers/test_workspace.py` if they don't already exist. Production code changes are all in-place edits.
