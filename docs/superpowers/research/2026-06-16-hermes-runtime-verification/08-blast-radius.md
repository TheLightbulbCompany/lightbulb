# Blast-radius re-verification (Hermes runtime option) — 2026-06-16

Re-verifies the OpenClaw-coupling blast radius from the 2026-05-17 audit
(`git show c9562d39:docs/audit-2026-05-17-hermes-vs-openclaw.md`, §8) against
`main` as of 2026-06-16. Every claim below was read in the current worktree;
line numbers are current, not the audit's.

## Headline

- **File count grew: audit said 57 → today 72** `.py` files under
  `apps/backend/core` + `apps/backend/routers` match `openclaw`
  (case-insensitive). Counting all file types (Atlas seed markdown/scripts)
  it is 133. The audit's "57 files" was a bare-`grep` count; the closest
  apples-to-apples today is the **72 `.py`** number.
- **No work started.** Zero hits for `runtime_kind`, `hermes`, or
  `RuntimeBackend` anywhere in `apps/backend`, `apps/frontend`, `apps/infra`.
  Greenfield.

## 1. Files HARD-coupled to OpenClaw protocol/filesystem (today)

All audit-named hard-coupled files still exist and still carry the coupling,
with line drift:

| File | Coupling | Status vs audit |
|---|---|---|
| `core/gateway/connection_pool.py` | `ws_connect`, `GATEWAY_PORT=18789` (now at `:25`), protocol-v4 frames | Present. Grew **1761 → 2152 lines**. |
| `core/containers/config.py` | `write_openclaw_config` (`:1209`), `read…from_efs`, `openclaw.json` writer | Present. Grew to **1349 lines**. |
| `core/services/config_patcher.py` | deep-merge into `openclaw.json` | Present (still matches `openclaw`). |
| `core/services/friday_identity.py` | IDENTITY/SOUL/USER markdown templates (`:44/:61`); cites upstream `src/agents/workspace.ts` | Present. **728 lines.** |
| `core/containers/workspace.py` | `pre_stage_auth_profile_store` | Present but **MOVED**: audit cited `686-795`; now `725-836`. Also new 6.6-era legacy `agents/*/agent/auth-profiles.json` cleanup at `:860`. |
| `core/services/agent_export.py` | runtime-state filtering, `FRIDAY_PUBLISH_ERROR_MSG` (`:50`) | Present **but coupling SHAPE changed — see Drift #1.** |
| `core/services/catalog_service.py` | deploy path `.openclaw/workspaces/{new_agent_id}` (`:307`), deep-merge into `openclaw.json` | Present. **517 lines.** |
| `core/services/catalog_slice.py` | slices an agent out of `openclaw.json`; cites `openclaw/src/config/zod-schema.ts` (`:65`) | Present. **381 lines.** |

Routers hard-coupled to OpenClaw RPC/proxy (audit §8.1):
`container_rpc.py`, `control_ui_proxy.py`, `hooks_proxy.py`, `updates.py`,
`debug.py`, `websocket_chat.py`, `workspace_files.py` — all still present.

## 2. Drift / contradictions found (highest value)

### Drift #1 — `_OPENCLAW_RUNTIME_STATE_DIRS` is GONE; export source dir changed
The audit claimed `agent_export.py` strips `sessions`/`agent` via a constant
`_OPENCLAW_RUNTIME_STATE_DIRS = {"sessions", "agent"}` and tars
`agents/<id>/agent/`. **Both are false today.**
- The constant does not exist. The junk-dir filter is now
  `_SKIP_DIR_NAMES` (`agent_export.py:88-99`) = `{__pycache__, .cache, .git,
  .pytest_cache, .ruff_cache, .venv, node_modules, .DS_Store}`. It does **not**
  contain `sessions` or `agent`.
- The export now tars **`workspaces/{agent_id}/`**, not `agents/{id}/`
  (`_resolve_agent_dir`, `agent_export.py:212-251`; docstring at `:218-228`
  explicitly says pointing at `agents/{id}/` was the bug). Runtime-state dirs
  (`sessions/`, `agent/`) live under `agents/`, which is simply never tarred —
  so the explicit-strip mechanism the audit described was removed as
  unnecessary. Repointed by **PR #713** (`provider strip + Paperclip-first
  agent ID + MCP path rewrite`). **The audit's §6 marketplace-mapping
  reasoning that leans on `_OPENCLAW_RUNTIME_STATE_DIRS` is stale.**

### Drift #2 — There are FOUR OpenClaw-WS clients, not one pool
The audit's §8.4 RuntimeBackend model assumes callers funnel through
`gateway_pool.request(...)`, so wrapping `connection_pool.py` covers the RPC
surface. **It does not.** Three additional code paths open their own raw
OpenClaw connection, each with its own `GATEWAY_PORT` constant and handshake:
- `routers/container_rpc.py` — `from websockets import connect as ws_connect`
  (`:22`), private `_openclaw_handshake` (`:40`) + `_call_gateway_rpc` (`:85`).
  **Does NOT touch `get_gateway_pool` at all.** This is the generic `POST /rpc`
  proxy — a large chunk of the control-dashboard RPC surface.
- `routers/control_ui_proxy.py` — raw HTTP+WS proxy to
  `http://{ip}:{GATEWAY_PORT}/` (`:149/:204/:271`). No `get_gateway_pool` (0 hits).
- `core/services/container_auth.py` — **NEW since audit** (created 2026-06-13):
  raw `ws_connect` to `ws://{ip}:{GATEWAY_PORT}` (`:62`) to revoke a provider
  credential from a running container.

`GATEWAY_PORT = 18789` is now **triplicated**: `core/containers/ecs_manager.py:37`
(the canonical home — `connection_pool`, `container_rpc`, `container_auth` all
import from here), plus separate redeclarations in
`core/gateway/connection_pool.py:25` and `core/gateway/node_connection.py:42`.
A `RuntimeBackend` abstraction must absorb all four clients, not just the pool.

## 3. RuntimeBackend caller list — validated against today

Actual `get_gateway_pool` / `gateway_pool` callers (10 files):
- `core/containers/__init__.py` (singleton factory)
- `core/gateway/connection_pool.py` (self)
- `core/services/admin_service.py`
- `core/services/catalog_service.py`
- `core/services/node_proxy.py`
- `core/services/paperclip_openclaw_pairing.py` (**new** since audit, 2026-05-08)
- `routers/admin.py`
- `routers/channels.py`
- `routers/container.py`
- `routers/teams/agents.py`
- `routers/websocket_chat.py` (the primary chat path; `get_gateway_pool()` at
  `:183/:356/:528/:877/:1197`)

**Correction to audit §8.4:** it lists `container_rpc.py` and
`control_ui_proxy.py` among the pool callers to refactor. Neither calls the
pool — they are independent raw clients (Drift #2). Conversely the audit's
"~25 router files that call `gateway_pool.request(...)`" is an overcount: only
**11 files total** reference the pool, of which **5 are routers**.

## 4. NEW OpenClaw coupling added since 2026-05-17 (audit missed these)

Created after the audit, with hard coupling:
- `core/services/container_auth.py` (2026-06-13) — 4th raw WS client (Drift #2).
- `core/services/credentials/mcp_server.py` (2026-06-01) — installs
  `{command,args,env}` blocks into the **live `openclaw.json`** and
  re-asserts `openclaw_paths` rails on overwrite (`:67-77`). A Hermes runtime
  has no `openclaw.json`; the entire integrations/credentials reconcile rail is
  OpenClaw-config-shaped.
- `core/services/operator_proxy.py` (2026-05-31) — runs the **OpenClaw
  device-auth handshake** for the Paperclip `openclaw_gateway` adapter relay
  (`:2-30`).
- `core/services/google_identity.py` (2026-06-03) — writes into `IDENTITY.md`
  relying on OpenClaw auto-loading it (`:11`); same identity-file coupling as
  `friday_identity.py`.
- `core/openclaw_version.py` (2026-05-31) — image-tag SSOT; brand+version,
  not protocol. Brand-adjacent.
- `core/services/atlas_seed/` (2026-05-29) — seed workspace files/skills;
  filesystem-layout coupling (persona/skill dirs).

Frontend (§8.2) all still present and grew: `useGateway.tsx` 514→610,
`useAgentChat.ts` 1103→1710, `useExecApprovals.tsx` 560, `useSystemHealth.ts`
188. Infra (§8.3) WS stack intact: `WebSocketApi`/`WebSocketStage`
(`api-stack.ts:451/:474`), `CfnVpcLink` (`:126`), `ws-connections` table (`:258`).

## Net

The dual-harness blast radius is **larger** than the audit estimated: +15 `.py`
files, the marketplace export mechanism it cited no longer exists in that form,
and there are 4 independent OpenClaw-WS clients (3 of which bypass the pool the
RuntimeBackend was supposed to wrap), plus a whole new credentials/integrations
rail that writes `openclaw.json`. None of it is started.
