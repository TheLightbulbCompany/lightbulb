# mcporter Removal + MCP Servers Tab Cutover to OpenClaw Native

**Date:** 2026-05-09
**Status:** Approved for planning
**Owner:** prabuddha@gmail.com

## Problem

Isol8 currently maintains a half-wired `.mcporter/mcporter.json` registry that is dead code:

- `apps/backend/core/containers/workspace.py:171-177` writes a default empty `.mcporter/mcporter.json` on every container provision.
- `apps/backend/routers/integrations.py` exposes CRUD endpoints (`GET/PUT/PATCH/DELETE /integrations/mcp/servers`) backed by that file.
- `apps/frontend/src/components/control/panels/McpServersTab.tsx` reads/writes through those endpoints.
- **Nothing downstream consumes the file.** `openclaw.json` does not register `mcporter` as a server runner. `grep mcporter apps/backend/core/containers/config.py` shows zero references inside the OpenClaw config builder.

Meanwhile, OpenClaw upstream provides a fully fleshed-out native MCP path:

- `mcp.servers` block in `openclaw.json` is the canonical, schema-validated registry (`openclaw/src/config/schema.base.generated.ts:27788`).
- `openclaw mcp list/show/set/unset` CLI manages it (`openclaw/docs/cli/mcp.md:351-396`).
- The embedded Pi auto-exposes registered MCP tools in `coding`/`messaging` profiles.
- The gateway exposes `config.get`, `config.set`, `config.patch` RPCs (`openclaw/src/gateway/server-methods/config.ts:270-437`).
- `mcp.servers.*` paths are tracked as **hot-reloadable** — patches take effect without container restart (`openclaw/src/gateway/config-reload.test.ts:373-440`).

User symptom that surfaced this: an agent asked to "set up Resy MCP" wrote a config blob into its workspace dir because the canonical mcporter path is hidden (`.mcporter` is on `_EXCLUDED_NAMES`) and not discoverable. Even if the agent had found mcporter.json, populating it would have done nothing because nothing reads it.

## Goal

Make `openclaw.json → mcp.servers` the single canonical MCP registry. Remove all mcporter scaffolding from Isol8.

## Non-goals

- Per-agent MCP scoping. OpenClaw upstream has no agent-scoped MCP concept; deferred unless we later add it upstream.
- Credential vault / secret store. Env vars stay plaintext under `mcp.servers[name].env` (upstream's documented model).
- Migrating existing dev users' stale `.mcporter/mcporter.json` files. They become orphaned bytes, ignored.
- Changing the `McpServersTab` UI shape. Same form, different backing store.

## Design

### Backing store swap (`apps/backend/routers/integrations.py`)

Replace EFS file I/O with gateway RPC calls.

- `_read_mcp_servers(user_id) -> tuple[dict, str]` — calls `config.get` via the gateway pool, returns `(config["mcp"]["servers"] or {}, base_hash)`.
- `_patch_mcp_servers(user_id, servers, base_hash) -> None` — calls `config.patch` with `{"mcp": {"servers": servers}}` and the prior base hash.

The four endpoints keep identical request/response shapes:

| Endpoint | Behavior |
|----------|----------|
| `GET /integrations/mcp/servers` | One `config.get`, return `mcp.servers`. |
| `PUT /integrations/mcp/servers` | `config.get` for base hash → `config.patch` replacing the entire `mcp.servers` subtree. |
| `PATCH /integrations/mcp/servers/{name}` | `config.get` for base hash + current servers → merge → `config.patch`. |
| `DELETE /integrations/mcp/servers/{name}` | Same read-merge-patch with the named server removed. |

Optimistic-concurrency retry: on `config.patch` rejection due to stale base hash, re-fetch and retry once. Surface OpenClaw's validation errors directly (do not swallow).

Helpers stay in `integrations.py` rather than spinning up a new service file — surface is small.

### Provisioning cleanup

- `apps/backend/core/containers/workspace.py`
  - Drop `".mcporter"` from `_EXCLUDED_NAMES` (lines 37-44).
  - Delete the default mcporter.json write block (lines 171-179) and any related comments.
- `apps/backend/core/containers/config.py`
  - Delete `write_mcporter_config` (lines 779+).
- `apps/backend/core/containers/ecs_manager.py`
  - Remove the `write_mcporter_config` import (line 27) and the call at line 1614.
  - Update the comment at line 1518 listing files written to EFS.
- `apps/backend/tests/unit/containers/test_config.py`
  - Remove the `write_mcporter_config` test cases (lines 275-300).

### Frontend

`McpServersTab.tsx` — no change. The REST surface it consumes is unchanged.

`SkillsPanel.tsx` — no change. Continues to render `<McpServersTab />` under its `mcp` tab.

## Failure modes

- **Container not provisioned / gateway disconnected** — `config.get` errors before any read can succeed. The endpoint should return a clear 503-ish payload (e.g. `{"error": "container_not_ready"}`) rather than a generic 500. The frontend renders an empty state with a "container starting up" hint.
- **Optimistic concurrency race** — `config.patch` rejected for stale base hash. Retry once with a fresh `config.get`. If the second attempt also fails, return 409 to the client.
- **OpenClaw schema rejection** — invalid command/url/transport. Pass OpenClaw's error message through to the client so the user sees what's wrong (mirrors how upstream's `openclaw mcp set` reports validation errors).
- **Existing dev users with stale `.mcporter/mcporter.json`** — file becomes unreferenced. No active migration; harmless.

## Test plan

Backend unit:
- Mock the gateway pool. Test that `GET` returns `mcp.servers` from `config.get`.
- Test that `PUT/PATCH/DELETE` call `config.patch` with the right delta and base hash.
- Test the stale-base-hash retry path.
- Test the container-not-ready path returns a structured 503.
- Verify `write_mcporter_config` and its tests are deleted.

Backend integration (existing harness):
- Provision flow no longer creates `.mcporter/` on EFS.

Frontend:
- Existing unit tests for `McpServersTab` keep passing — REST contract unchanged.

Manual smoke:
- Add a server via the UI on dev. Confirm it appears in `openclaw.json → mcp.servers` (via `/debug/provision` PATCH inspection or `openclaw mcp list` inside the container).
- Confirm the agent can call its tool without a container restart (hot-reload should pick it up).

## Out of scope (explicit "go from there" follow-ups)

Per the brainstorming conversation, the user wants this PR landed before deciding on:

- Whether to add an "equivalent CLI command" hint under each server in the UI.
- Per-agent MCP scoping (would need an upstream OpenClaw change).
- A separate credential audit surface.
- Whether to teach the agent to register MCP servers itself (e.g. by exposing a tool, an instruction file, or shelling out to `openclaw mcp set`).

These are tracked as open questions, not blockers for this PR.
