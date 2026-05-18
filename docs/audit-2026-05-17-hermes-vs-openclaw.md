# Hermes vs OpenClaw: Primary-Source Migration Audit

**Date:** 2026-05-17
**Status:** Draft 1 — built from on-disk source only; no extrapolation from prior conversation
**Author:** Claude (audit pass)

## Why this exists

We are evaluating whether to replace the OpenClaw agent runtime with Nous Research's Hermes Agent. Prior conversational analysis kept producing confidently-wrong claims (`agents.list[]` has per-agent plugin config — false; Hermes has no remote-WS bridge — false; personalities share all state — partially false). This document is the rebuild: every claim has a file:line citation from primary source, and where source contradicts a prior claim, the contradiction is called out explicitly.

## Scope

- **In scope:** the agent runtime layer only — what runs inside a per-user container today (OpenClaw) and what would run there if we swapped (Hermes). Wire protocol, config schema, identity model, provider auth, marketplace artifact pipeline, Paperclip adapter, exec-approval, isolation boundaries.
- **Out of scope:** the SaaS shell (Clerk, Stripe, Aurora-backed Paperclip BFF, CDK, admin surface, Tauri desktop). Those stay regardless. The "should we be a SaaS" question is not what's being asked.

## Sources

- **Hermes** — upstream clone at `hermes-agent/` (gitignored). All paths in this doc prefixed `hermes-agent/` reference files in that tree.
- **OpenClaw** — upstream clone (sibling to this repo, gitignored). Paths prefixed `openclaw/` reference files in that tree. See `CLAUDE.md` for the standard convention.
- **Isol8 backend** — `apps/backend/`. Paths prefixed `apps/backend/`.
- **Paperclip** — `paperclip/` (read-only upstream clone, gitignored). Paths prefixed `paperclip/`.

Where a section's claim contradicts something stated earlier in the investigation, the section starts with a **CORRECTION** callout.

## Table of contents

1. [Runtime model: process layout, lifecycle, session caching](#1-runtime-model)
2. [Wire protocol: how the backend talks to the container](#2-wire-protocol)
3. [Config schema: per-agent vs container-wide isolation](#3-config-schema--isolation)
4. [Identity & seed-file model (Friday, Atlas)](#4-identity--seed-files)
5. [Provider auth (Bedrock, ChatGPT OAuth, BYOK)](#5-provider-auth)
6. [Marketplace artifact mapping](#6-marketplace-artifact-mapping)
7. [Paperclip adapter integration](#7-paperclip-adapter-integration)
8. [Frontend & infrastructure blast radius](#8-frontend--infra-blast-radius)
9. [Spike scope & open questions](#9-spike-scope--open-questions)
10. [Appendix: file inventory](#appendix-file-inventory)

---

## 1. Runtime model

### 1.1 Hermes — `gateway/run.py` is a long-lived daemon hosting many things

Entry point: `python -m gateway.run` (`hermes-agent/gateway/run.py:8-13` module docstring). The daemon brings up every messaging-platform adapter, the API server, the webhook server, an in-process cron ticker, and (when enabled) a kanban dispatcher. The runner class is `GatewayRunner`.

Key state for session/agent caching (`gateway/run.py:57-66`):

```py
_AGENT_CACHE_MAX_SIZE = 128
_AGENT_CACHE_IDLE_TTL_SECS = 3600.0  # evict agents idle for >1h
```

The cache itself (`gateway/run.py:1264-1276`):

```py
# Cache AIAgent instances per session to preserve prompt caching.
# Without this, a new AIAgent is created per message, rebuilding the
# system prompt (including memory) every turn — breaking prefix cache
# and costing ~10x more on providers with prompt caching (Anthropic).
# Key: session_key, Value: (AIAgent, config_signature_str)
self._agent_cache: "OrderedDict[str, tuple]" = OrderedDict()
self._agent_cache_lock = _threading.Lock()
```

So one daemon → up to 128 cached `AIAgent` instances → keyed by `session_key`. Mid-turn agents are explicitly skipped on LRU eviction (`gateway/run.py:14222-14227`):

> Agents currently in `_running_agents` are SKIPPED — their clients, terminal sandboxes, background processes, and child subagents are all in active use by the running turn. Evicting them would tear down those resources mid-turn and crash the request.

### 1.2 Hermes — API server route table

From `gateway/platforms/api_server.py:3400-3423` (route registration, verbatim):

```
GET    /health
GET    /health/detailed
GET    /v1/health
GET    /v1/models
GET    /v1/capabilities
POST   /v1/chat/completions
POST   /v1/responses
GET    /v1/responses/{response_id}
DELETE /v1/responses/{response_id}
GET    /api/jobs              # cron CRUD
POST   /api/jobs
GET    /api/jobs/{job_id}
PATCH  /api/jobs/{job_id}
DELETE /api/jobs/{job_id}
POST   /api/jobs/{job_id}/pause
POST   /api/jobs/{job_id}/resume
POST   /api/jobs/{job_id}/run
POST   /v1/runs               # async, returns 202 + run_id
GET    /v1/runs/{run_id}      # poll status
GET    /v1/runs/{run_id}/events     # SSE
POST   /v1/runs/{run_id}/approval   # resolve exec approval
POST   /v1/runs/{run_id}/stop       # interrupt
```

Default bind: `127.0.0.1:8642` (`api_server.py:57-58`). Refusing to bind a non-loopback host without `API_SERVER_KEY` is hard-coded (`api_server.py:3433-3440`):

```py
if is_network_accessible(self._host) and not self._api_key:
    logger.error(
        "[%s] Refusing to start: binding to %s requires API_SERVER_KEY. "
        "Set API_SERVER_KEY or use the default 127.0.0.1.",
        self.name, self._host,
    )
    return False
```

Auth is a single bearer token compared via `hmac.compare_digest` (`api_server.py:647` reads `API_SERVER_KEY` env, default empty). No per-user / per-tenant scope.

### 1.3 Hermes — `POST /v1/runs` lifecycle

Reading `api_server.py:2862-3157`:

1. Generate `run_id = f"run_{uuid.uuid4().hex}"` (`:2940`)
2. Compute `session_id = body.get("session_id") or stored_session_id or run_id` (`:2941`)
3. Compute `approval_session_key = gateway_session_key or session_id or run_id` (`:2942`)
4. Create per-run asyncio queue, register in `self._run_streams[run_id]` (`:2945-2947`)
5. Set status `queued`, return 202 with `{"run_id": run_id, "status": "started"}` (`:2967-2973, 3152-3156`)
6. In background: `_create_agent(ephemeral_system_prompt=instructions, session_id, gateway_session_key, …)` (`:2978-2984`)
7. Register approval notifier keyed on `approval_session_key` (`:3026`)
8. Call `agent.run_conversation(user_message, conversation_history, task_id=session_id or run_id)` (`:3027-3031`)
9. Stream events into the queue; SSE handler at `/v1/runs/{run_id}/events` drains it (`:3173-3220`, keepalive `: keepalive\n\n` every 30s on idle)
10. On approval needed: emit `{"event": "approval.request", "run_id", "choices": ["once","session","always","deny"]}`; set status `waiting_for_approval` (`:2987-3003`)
11. Approval resolution via `POST /v1/runs/{run_id}/approval` with body `{choice}` → `tools.approval.resolve_gateway_approval(approval_session_key, choice)` (`:3223-3299`)

Approval choice aliases: `approve` / `approved` / `allow` → `once` (`:3243`). Allowed values: `once`, `session`, `always`, `deny` (`:3245`).

The SSE response sets `X-Accel-Buffering: no` (`:3196`) — important because nginx/CloudFront default to buffering text/event-stream and would otherwise break streaming.

### 1.4 Hermes — `tui_gateway/ws.py` is a real WebSocket RPC

From `tui_gateway/ws.py:1-22` (module docstring, verbatim):

> Reuses :func:`tui_gateway.server.dispatch` verbatim so every RPC method, every slash command, every approval/clarify/sudo flow, and every agent event flows through the same handlers whether the client is Ink over stdio or **an iOS / web client over WebSocket**.
>
> Wire protocol: Identical to stdio: newline-delimited JSON-RPC in both directions. The server emits a `gateway.ready` event immediately after connection accept, then echoes responses/events for inbound requests. No framing differences.

Mounted in the dashboard at `/api/ws` (`hermes_cli/web_server.py:3384` per earlier grep). So Hermes has two distinct remote-callable surfaces:

| Surface | Transport | Auth | Wire format | Use case |
|---|---|---|---|---|
| `gateway/platforms/api_server.py` | HTTP + SSE | `API_SERVER_KEY` bearer | OpenAI-compat JSON + SSE events | External LLM clients, our backend |
| `tui_gateway/ws.py` mounted in dashboard | WebSocket | dashboard session (TBD) | newline-delimited JSON-RPC | TUI, iOS, web client |

**CORRECTION** — In conversation I claimed Hermes only had HTTP+SSE and "no WebSocket bridge." That was wrong. `tui_gateway/ws.py` is a genuine WS-RPC server that exposes every TUI command remotely. Whether we use HTTP+SSE or WS-RPC for the backend↔container channel is an *architectural choice* available to us, not a constraint.

### 1.5 OpenClaw — runtime model and protocol

OpenClaw runs a single Node daemon per container. The daemon hosts a WebSocket server (`openclaw/src/gateway/server/ws-connection.ts:3,123,198,226` imports `WebSocketServer` from `ws`, calls `wss.on("connection", …)`) plus the 71 RPC method modules registered under `openclaw/src/gateway/server-methods/`. Same shape as Hermes' `gateway/run.py` — a long-lived daemon multiplexing many sessions.

Protocol version is pinned (`openclaw/src/gateway/protocol/version.ts:1-3`):

```ts
export const PROTOCOL_VERSION = 4 as const;
export const MIN_CLIENT_PROTOCOL_VERSION = 4 as const;
export const MIN_PROBE_PROTOCOL_VERSION = 4 as const;
```

Three top-level frame types (`openclaw/src/gateway/protocol/schema/frames.ts:138-176`), as a discriminated union with `type` as discriminator:

| Frame | Shape | Direction |
|---|---|---|
| `req` | `{type:"req", id, method, params?}` | client → server |
| `res` | `{type:"res", id, ok: bool, payload?, error?}` | server → client |
| `event` | `{type:"event", event, payload?, seq?, stateVersion?}` | server → client |

`HelloOk` handshake (`frames.ts:73-125`) returns server `features.methods[]` + `features.events[]`, a config `snapshot`, and an `auth` object with `deviceToken` + role + scopes + optional `deviceTokens[]` for multi-device. Connect parameters carry `device.{id, publicKey, signature, signedAt, nonce}` for ed25519 device-pairing (`frames.ts:43-66`).

RPC method registration is per-file: each `server-methods/*.ts` exports a `GatewayRequestHandlers` map keyed by method name like `"agents.list"`, `"agents.create"` (`openclaw/src/gateway/server-methods/agents.ts:452,469,580,683`), `"sessions.send"`, `"sessions.steer"` (`sessions.ts:1633,1645`), and so on across 71 files. The full method surface includes (from the file list at `server-methods/`): agent CRUD + jobs + timestamps, chat, channels, commands, config, connect, cron, devices, diagnostics, doctor, environments, exec-approval(s), health, logs, models, native-hook-relay, nodes, plugin-approval + host-hooks, push, restart, secrets, send, sessions, skills + config-mutations + uploads, system, talk(-client/-session/-shared), tasks, tools-catalog + effective + invoke, tts, update, usage, validation, voicewake, web, wizard.

This is structurally identical to what `tui_gateway/server.dispatch` does on the Hermes side — a JSON-RPC server with many registered methods, with `req`/`res`/`event` framing. The big differences:

| | OpenClaw | Hermes `tui_gateway/ws.py` | Hermes `api_server.py` |
|---|---|---|---|
| Transport | WebSocket | WebSocket | HTTP + SSE |
| Frame style | `{type:"req"/"res"/"event", id, …}` | newline-delimited JSON-RPC | OpenAI Chat Completions JSON + SSE `data:` lines |
| Handshake | `ConnectParams` w/ device pairing (ed25519) | `gateway.ready` event on accept | none — bearer per request |
| Auth | device token + role + scopes (multi-device aware) | dashboard session (TBD) | `API_SERVER_KEY` bearer (single, global) |
| Method registration | 71 `server-methods/*.ts` files | `tui_gateway/server.dispatch` | aiohttp route table |
| Approval queue | `exec-approval(s)` RPC methods, push events | inherited from tui_gateway server | `POST /v1/runs/{id}/approval`, SSE `approval.request` event |

### 1.6 Runtime model summary (apples-to-apples)

Both runtimes are: one long-lived daemon per container, hosting N sessions multiplexed inside, with session state cached in memory, an RPC method surface for the orchestrator, and a push channel for streaming agent events. The differences are in transport + framing, not in process model. **OpenClaw uses one WS-RPC channel; Hermes offers a WS-RPC channel (tui_gateway) AND an HTTP+SSE channel (api_server). Either could replace `connection_pool.py`.**

---

## 3. Config schema & isolation

**CORRECTION** — I earlier claimed "each agent in OpenClaw's `agents.list[]` can in principle pick its own provider/plugins/MCP config." That was wrong. The actual schema permits per-agent overrides for *some* dimensions (system prompt, skill subset, tool allow/deny, model, workspace, sandbox) but **not** for MCP server lists or plugin registrations — those are container-wide in both runtimes. The per-agent isolation surface is roughly equivalent in OpenClaw and Hermes; the ergonomics differ.

### 3.1 OpenClaw top-level keys

From `openclaw/src/config/zod-schema.ts` (line numbers cite the `agents: AgentsSchema` and sibling keys):

| Key | Line | Per-agent overridable? | Notes |
|---|---|---|---|
| `agents` (= `{defaults, list[]}`) | 663 | partial — `list[]` is per-agent | Defined in `zod-schema.agents.ts:7-13` |
| `cron` | 684 | ❌ container-wide | One job table, one scheduler |
| `mcp` | 1078 | ❌ container-wide | MCP servers registered once per container |
| `skills` | 1079 | ❌ container-wide registration; ✅ per-agent name selection | `entries` registers skills; per-agent `skills: z.array(z.string())` picks names |
| `plugins` | 1115 | ❌ container-wide | Plugin registration is container-wide |
| `channels`, `models`, `tools`, `bindings`, `broadcast`, `audio`, `messages`, `commands`, `approvals`, `session`, `memory` | (various, all in `zod-schema.ts`) | ❌ container-wide | Top-level config keys |

### 3.2 OpenClaw per-agent slot (`AgentEntrySchema`)

From `openclaw/src/config/zod-schema.agent-runtime.ts:933-1006`, each entry in `agents.list[]` can override:

```ts
{
  id: string,
  default?: boolean,
  name?: string,
  workspace?: string,           // ✅ per-agent workspace dir
  agentDir?: string,
  systemPromptOverride?: string, // ✅ per-agent system prompt
  agentRuntime: AgentRuntimePolicySchema,
  embeddedHarness: AgentEmbeddedHarnessSchema,
  model?: AgentModelSchema,      // ✅ per-agent model
  models?: Record<string, {…}>,  // ✅ per-agent per-model overrides
  thinkingDefault?: …,
  verboseDefault?: …,
  toolProgressDetail?: …,
  reasoningDefault?: …,
  fastModeDefault?: boolean,
  skills?: string[],             // ✅ per-agent skill SUBSET (names — registration is container-wide)
  memorySearch: MemorySearchSchema,
  humanDelay?: HumanDelaySchema,
  tts: TtsConfigSchema,
  skillsLimits: …,
  contextLimits: …,
  contextTokens?: number,
  heartbeat: HeartbeatSchema,
  identity: IdentitySchema,      // ✅ per-agent identity files
  groupChat: GroupChatSchema,
  subagents?: {…},
  runRetries?: …,
  embeddedPi?: …,
  sandbox: AgentSandboxSchema,   // ✅ per-agent sandbox config
  params?: Record<string, unknown>,
  tools: AgentToolsSchema,       // ✅ per-agent tool allow/deny
  runtime: AgentRuntimeSchema,
}
```

The per-agent `tools: AgentToolsSchema` (`agent-runtime.ts:703-732`) is **not** the place where MCP servers or plugins are declared — it's `{...CommonToolPolicyFields, elevated, exec, fs, loopDetection, message, sandbox}` where `CommonToolPolicyFields` is `{profile, allow, alsoAllow, deny, byProvider, toolsBySender}` (line 660-667). I.e. per-agent tool config is purely **allow/deny filtering** on the container's registered tools.

### 3.3 Hermes per-call init kwargs

`hermes-agent/agent/agent_init.py` argument list (lines 87-140, selected):

```py
def init_agent(
    agent: Any,
    ...
    enabled_toolsets: List[str] = None,    # ✅ per-call tool allowlist
    disabled_toolsets: List[str] = None,   # ✅ per-call tool denylist
    ...
    ephemeral_system_prompt: str = None,   # ✅ per-call system prompt
    ...
    session_id: str = None,                # ✅ per-call transcript scope
    ...
    platform: str = None,
    user_id: str = None,
    chat_id: str = None,
    thread_id: str = None,
    gateway_session_key: str = None,       # ✅ per-call long-term memory + approval scope
    skip_context_files: bool = False,      # ✅ skip SOUL.md/AGENTS.md auto-injection
    load_soul_identity: bool = False,
    skip_memory: bool = False,
    ...
    credential_pool=None,
    ...
):
```

These are the equivalent levers OpenClaw exposes via `AgentEntrySchema`, just supplied per-invocation rather than declared per-slot in config. The orchestrator (us) holds the per-agent-identity → kwargs mapping rather than serializing it into `config.yaml`. Section `gateway/platforms/api_server.py:851-894` (`_create_agent`) shows how the API server constructs an agent per-request using these kwargs, with `enabled_toolsets` pulled from `platform_toolsets.api_server` in config.yaml plus per-call overrides.

### 3.4 Container-wide scoping in Hermes

From the same `config.yaml`-level fields:

| Key | File | Container-wide? |
|---|---|---|
| `mcp_servers` | `hermes_cli/mcp_config.py:8,81` | ✅ yes — one set per HERMES_HOME |
| `skills/` directory | `~/.hermes/skills/` (`hermes_bootstrap.py:288`) | ✅ yes — one set per HERMES_HOME, agent picks subset |
| `cron/jobs.json` | `~/.hermes/cron/jobs.json` (`cron/jobs.py:1-46`) | ✅ yes — one table per HERMES_HOME |
| `personalities` dict | `hermes_cli/config.py:1428-1431` | per-call: orchestrator supplies system prompt directly via `ephemeral_system_prompt` |
| `auth.json` + `~/.codex/auth.json` | `agent/credential_sources.py:294-298` | ✅ yes — one credential pool per HERMES_HOME |
| `TERMINAL_SANDBOX_DIR` | `tools/environments/base.py:85-87` | parent shared per HERMES_HOME, sub-dirs per agent instance |
| `state.db` (sessions) | `hermes_state.py:34` | ✅ one DB per HERMES_HOME, rows scoped by session_id |

### 3.5 Isolation matrix (corrected)

| Dimension | OpenClaw (per-agent) | Hermes (per-call init) |
|---|---|---|
| System prompt | ✅ `systemPromptOverride` | ✅ `ephemeral_system_prompt` |
| Identity files (SOUL/IDENTITY/USER) | ✅ `IdentitySchema` + `agents/<id>/agent/` dir | ⚠️ `SOUL.md` is HERMES_HOME-wide; orchestrator can `skip_context_files=True` + `load_soul_identity=True` or inject identity via system prompt |
| Tool selection (allow/deny) | ✅ `tools` allow/deny + `byProvider` | ✅ `enabled_toolsets` / `disabled_toolsets` |
| Skill subset | ✅ `skills: string[]` | ✅ via `enabled_toolsets` (skills are a toolset family) |
| Model override | ✅ `model: AgentModelSchema` | ✅ `model` kwarg |
| Workspace dir | ✅ `workspace` | ⚠️ sub-dir under shared `TERMINAL_SANDBOX_DIR`; per-session sub-dir convention, not config-declared |
| Sandbox policy | ✅ `AgentSandboxSchema` | ⚠️ environment backend selected once per container via `tools/environments/`; per-run isolation via the chosen backend |
| Long-term memory scope | implicit per-agent (memory plugin keyed by agent_id) | ✅ `gateway_session_key` (Honcho et al keyed on this) |
| Approval queue | ✅ per-agent | ✅ `approval_session_key = gateway_session_key or session_id or run_id` |
| MCP server *registrations* | ❌ container-wide | ❌ container-wide |
| Plugin *registrations* | ❌ container-wide | ❌ container-wide |
| Cron job table | ❌ container-wide | ❌ container-wide |
| Credential pool / BYOK | ❌ container-wide | ❌ container-wide |

**The per-agent isolation surface is structurally equivalent.** OpenClaw declares overrides in config; Hermes accepts them at agent-init time. The container-wide registrations (MCP, plugins, cron, credentials) are identical: shared by every agent in the container. Either runtime can host Friday + Atlas + N marketplace agents under the same isolation guarantees we have today.

---

## 7. Paperclip adapter integration

### 7.1 Today: `openclaw_gateway` adapter shape

The Paperclip-side adapter package exports (`paperclip/packages/adapters/openclaw-gateway/src/index.ts:1-3`):

```ts
export const type = "openclaw_gateway";
export const label = "OpenClaw Gateway";
```

The full `adapterConfig` contract is documented in `index.ts:6-55` and includes:

| Field | Required? | Purpose |
|---|---|---|
| `url` | yes | `ws://` or `wss://` endpoint |
| `headers` | no | handshake headers (e.g. `x-openclaw-token`) |
| `authToken` | no | shared gateway token |
| `password` | no | gateway shared password |
| `clientId`/`clientMode`/`clientVersion` | no | gateway client identity |
| `role` | no | gateway role (default `operator`) |
| `scopes` | no | scopes array (default `["operator.admin"]`) |
| `disableDeviceAuth` | no | skip ed25519 signed device payload (default false) |
| `payloadTemplate` | no | extra fields merged into gateway agent params |
| `timeoutSec` / `waitTimeoutMs` | no | timeouts |
| `autoPairOnFirstConnect` | no | auto-run device.pair.list/approve on pairing-required (default true) |
| `paperclipApiUrl` | no | absolute Paperclip base URL exposed to the agent |
| `claimedApiKeyPath` | no | path to claimed-API-key JSON file (default `~/.openclaw/workspace/paperclip-claimed-api-key.json`) |
| `sessionKeyStrategy` | no | `issue` (default), `fixed`, `run` |
| `sessionKey` | no | fixed session key when strategy=`fixed` |
| `agentId` | no | gateway agent id |

The adapter is implemented in `paperclip/packages/adapters/openclaw-gateway/src/server/execute.ts` (1496 lines). Entry point `execute(ctx: AdapterExecutionContext)` (line 1048) does:

1. Validate URL is `ws:`/`wss:` (lines 1049-1079)
2. Resolve auth (token / password / device-pairing identity) (lines 1090-1104, 1248-1304)
3. Open WS, perform `connect` handshake with `PROTOCOL_VERSION=4`, ed25519 device signature (lines 1258-1311)
4. Send RPC `req` `{method: "agent", params: {message, sessionKey, idempotencyKey: ctx.runId, paperclip: <context>}}` (line 1313)
5. Stream `event`/`agent` frames into `assistantChunks[]`, watch for lifecycle errors (lines 1192-1238)
6. If status != "ok", call RPC `agent.wait` to block until completion (line 1342)
7. Parse `usage`, `model`, `costUsd`, `runtimeServices[]` from response `meta` (lines 1399-1413)
8. Return `AdapterExecutionResult` with `summary`, `provider`, `model`, `usage`, `resultJson`

### 7.2 Today: Isol8 backend synthesis

`apps/backend/core/services/paperclip_adapter_config.py:34-77` writes a 4-field config:

```py
OPENCLAW_GATEWAY_TYPE: Final[str] = "openclaw_gateway"

def synthesize_openclaw_adapter(*, gateway_url, service_token, user_id) -> dict:
    validate_gateway_url(gateway_url)  # allowlist: wss://ws[-{env}].isol8.co or ws://localhost:N
    return {
        "url": gateway_url,
        "authToken": service_token,
        "sessionKeyStrategy": "issue",
        "sessionKey": user_id,
    }
```

`sessionKeyStrategy: "issue"` means each Paperclip issue → its own OpenClaw session keyed `agent:<agent_id>:paperclip:issue:<issue_id>` (per the docstring at lines 18-26). The fallback `sessionKey: user_id` handles wakes without an issueId (manual heartbeat runs, sibling-agent wakes).

`paperclip_provisioning.py:349,380` calls this when seeding Friday + Atlas at owner provision time.

### 7.3 What `hermes_gateway` would need

The structural shape can mirror `openclaw_gateway` 1:1. The key changes are transport-level — `ws:` → `https:`, WS-RPC → HTTP+SSE, device-pairing handshake → bearer header.

Proposed `paperclip/packages/adapters/hermes-gateway/src/index.ts` (or `@isol8/adapter-hermes-gateway` as a Paperclip plugin — see registry external-loader at `paperclip/server/src/adapters/registry.ts:430-453`):

```ts
export const type = "hermes_gateway";
export const label = "Hermes Gateway";
```

Proposed adapterConfig fields (subset of openclaw_gateway, plus HTTP-specific):

| Field | Notes |
|---|---|
| `url` | `https://` or `http://` — Hermes API server (`/v1` prefix appended internally) |
| `authToken` | bearer for `Authorization: Bearer …` (matches Hermes `API_SERVER_KEY`) |
| `timeoutSec` / `waitTimeoutMs` | same as openclaw_gateway |
| `sessionKeyStrategy` / `sessionKey` | same semantics — emitted as `X-Hermes-Session-Key` header |
| `payloadTemplate` | extra fields merged into `/v1/runs` body (e.g. `model` override) |
| `paperclipApiUrl` | as today — exposed in wake text |
| ~~`disableDeviceAuth`, `password`, `clientId`, `role`, `scopes`, `autoPairOnFirstConnect`, `headers.x-openclaw-*`~~ | not applicable — Hermes has no device-pairing handshake; auth is a single bearer |

Proposed `execute()` flow:

1. Validate URL is `http:`/`https:`
2. Build wake-text + paperclip context (reuse `@paperclipai/adapter-utils` helpers — `buildPaperclipEnv`, `renderPaperclipWakePrompt`, `stringifyPaperclipWakePayload`)
3. `POST /v1/runs` with body `{input: wakeText, instructions: payloadTemplate.systemPrompt, session_id, metadata: {paperclip: paperclipPayload}}`, header `X-Hermes-Session-Key: sessionKey`, header `Authorization: Bearer authToken`. Returns 202 + `{run_id, status: "started"}`. (Source: `hermes-agent/gateway/platforms/api_server.py:3152-3156`.)
4. Open SSE to `GET /v1/runs/{run_id}/events`
5. Stream events — extract `message.delta` events into `assistantChunks[]`, handle `approval.request` events (block run, surface to operator, then `POST /v1/runs/{run_id}/approval`)
6. On terminal event (status `completed`/`failed`), poll `GET /v1/runs/{run_id}` for final status + usage (`api_server.py:3158-3171`)
7. Return `AdapterExecutionResult` with summary, model, usage, resultJson

The adapter file is materially smaller than `openclaw-gateway/execute.ts` (~1500 lines) because:
- No WS frame multiplexing (HTTP+SSE handles correlation via `run_id`)
- No ed25519 device-pairing handshake (single bearer)
- No `connect` / `agent` / `agent.wait` RPC orchestration (one `POST` + one SSE subscription)
- No reconnect logic (EventSource auto-reconnects with `Last-Event-ID` if we send `id:` lines, though Hermes' current SSE handler at `api_server.py:3201-3220` doesn't appear to emit IDs — verify before relying on auto-resume)

Estimated adapter package size: ~400-600 lines of TypeScript.

### 7.4 Backend synthesis for `hermes_gateway`

`apps/backend/core/services/paperclip_adapter_config.py` would grow a peer function:

```py
HERMES_GATEWAY_TYPE: Final[str] = "hermes_gateway"

def synthesize_hermes_adapter(*, hermes_api_url, service_token, user_id) -> dict:
    validate_hermes_url(hermes_api_url)  # allowlist regex for https://hermes[-env].isol8.co or http://localhost
    return {
        "url": hermes_api_url,
        "authToken": service_token,
        "sessionKeyStrategy": "issue",
        "sessionKey": user_id,
    }
```

`paperclip_provisioning.py` becomes runtime-aware: a switch on the container's `runtime_kind` selects which synthesize_*_adapter to call when seeding Friday/Atlas. The Paperclip-facing agent record stays the same shape (one `adapter_type` field + one `adapter_config` object); only the contents differ.

### 7.5 Open questions for the spike

1. **Hermes SSE `Last-Event-ID` support** — does `api_server.py:3201-3220` emit `id:` lines per event? If not, intermittent disconnects mean restart-from-zero rather than resume-from-offset. Need to verify or contribute upstream.
2. **`/v1/runs` body schema for `metadata.paperclip`** — Hermes API server accepts arbitrary body fields but does it propagate `metadata` into the AIAgent so tools can read it? Or does the wake text alone need to carry the paperclip context (as the openclaw adapter does via `buildWakeText`)?
3. **Long-running runs vs ALB idle timeout** — single SSE response for a 30-minute agent turn vs ALB's 60s default. Bump idle timeout OR rely on EventSource reconnect (requires Last-Event-ID, see #1).
4. **Approval round-trip latency** — openclaw_gateway uses the WS for inline approval back-and-forth in <100ms. HTTP `POST /v1/runs/{run_id}/approval` from a separate request adds setup latency per approval. Likely not user-visible, but measure during spike.
5. **`headers` field on adapterConfig** — drop entirely, or keep for forward-compat with Hermes's `Authorization` + optional `X-Hermes-Session-Key`? Recommend keep for symmetry with openclaw_gateway.

---

## 7B. Hermes kanban vs Paperclip task model

The user specifically called this out. Reading the source confirms: **Hermes' kanban is not a competitor to Paperclip — it operates one layer deeper.** This section explains the layering and what we should (and shouldn't) integrate.

### 7B.1 What Hermes' kanban actually is

From `hermes-agent/hermes_cli/kanban_db.py:1-69` (module docstring, verbatim selections):

> SQLite-backed Kanban board for multi-profile, multi-project collaboration. In a fresh install the board lives at `<root>/kanban.db` where `<root>` is the **shared Hermes root**… Profiles intentionally collapse onto a shared board: it IS the cross-profile coordination primitive.
>
> Multiple boards (projects): users can create additional boards to separate unrelated streams of work… Each board is a directory under `<root>/kanban/boards/<slug>/` with its own `kanban.db`, `workspaces/`, and `logs/`.

Schema (`kanban_db.py:755-862`):

```sql
CREATE TABLE tasks (
  id, title, body, assignee, status, priority,
  created_by, created_at, started_at, completed_at,
  workspace_kind ('scratch'|'worktree'|'dir'), workspace_path,
  claim_lock, claim_expires,             -- CAS-claimed by worker
  tenant, result, idempotency_key,
  consecutive_failures, worker_pid,      -- circuit breaker
  last_failure_error, max_runtime_seconds, last_heartbeat_at,
  current_run_id,                        -- pointer into task_runs
  workflow_template_id, current_step_key, -- v2 stage routing
  skills, max_retries
);

CREATE TABLE task_links (parent_id, child_id);  -- DAG
CREATE TABLE task_comments (task_id, author, body, created_at);
CREATE TABLE task_events (task_id, run_id, kind, payload, created_at);
CREATE TABLE task_runs (...status: running|done|blocked|crashed|timed_out|failed|released);
CREATE TABLE kanban_notify_subs (...);
```

Statuses: `triage, todo, ready, running, blocked, done, archived` (`kanban_db.py:93`). Claim TTL: 15 minutes default (`kanban_db.py:100-102`). Atomicity via SQLite WAL + `BEGIN IMMEDIATE` + CAS on `status` and `claim_lock` (`kanban_db.py:61-68`).

### 7B.2 Worker lanes — the dispatcher contract

From `hermes-agent/website/docs/user-guide/features/kanban-worker-lanes.md:14-21` (verbatim):

```
Hermes Kanban  =  canonical task lifecycle + audit trail
Worker lane    =  implementation executor for one assigned card
Reviewer       =  human or human-proxy that gates "done"
GitHub PR      =  upstreamable artifact (optional, for code lanes)
```

> Hermes Kanban owns lifecycle truth — `ready` → `running` → `blocked` / `done` / `archived`. Worker lanes execute work but never own that truth; everything they do flows back through the kanban kernel via the `kanban_*` tools (or, for non-Hermes external workers, via the API).

Default worker spawn (`:33-46`): `hermes -p <assignee> chat -q <prompt>` with env vars `HERMES_KANBAN_{TASK,DB,BOARD,WORKSPACES_ROOT,WORKSPACE,RUN_ID,CLAIM_LOCK,PROFILE,TENANT}`. Non-Hermes lanes register a plugin with a `spawn_fn` callable.

### 7B.3 Agent-callable kanban tools

`hermes-agent/tools/kanban_tools.py` (1139 lines) exposes these tools to the agent loop: `kanban_show`, `kanban_list`, `kanban_complete(summary, metadata)`, `kanban_block(reason)`, `kanban_heartbeat`, `kanban_comment`, `kanban_create`, `kanban_unblock`. Plus the `/tasks/{id}/decompose` endpoint (`plugin_api.py:1649`) — auto-decomposition into subtasks via an auxiliary LLM.

### 7B.4 Dashboard plugin REST + WS surface

`plugins/kanban/dashboard/plugin_api.py` registers a FastAPI router under `/api/plugins/kanban` with 30+ endpoints (see lines 341-1811): board CRUD, task CRUD + bulk + bulk-status, comments, links, dispatch, reclaim, specify, reassign, home-channel subscribe/unsubscribe per platform, stats, assignees, log tail, decompose, orchestration policy GET/PUT, and a WebSocket at `/events`.

### 7B.5 Paperclip's task model (what we use today)

`apps/backend/routers/teams/issues.py:20-162` exposes the Paperclip-issue surface to our frontend:

```
GET    /issues                       (list, with filters)
GET    /issues/labels
GET    /issues/{issue_id}
POST   /issues                       (create)
PATCH  /issues/{issue_id}            (update fields)
POST   /issues/{issue_id}/archive
POST   /issues/{issue_id}/unarchive
POST   /issues/{issue_id}/mark-read
POST   /issues/{issue_id}/mark-unread
GET    /issues/{issue_id}/comments
POST   /issues/{issue_id}/comments
```

Backed by per-tenant Aurora (Paperclip BFF), not SQLite. The Paperclip company is the multi-tenant boundary; each company has its own issue table.

### 7B.6 Side-by-side comparison

| Dimension | Hermes Kanban | Paperclip Issues |
|---|---|---|
| Storage | Per-container SQLite (`<root>/kanban.db` per board) | Per-tenant Aurora (per-company in `paperclip-stack.ts`) |
| Multi-tenant | ❌ One HERMES_HOME, multi-board within | ✅ Per-company isolation |
| Audience | Worker-pool inside one container | Human-facing Inbox + governance |
| Atomicity | SQLite WAL + CAS on `status`/`claim_lock` | Postgres transactions |
| Worker claim model | `claim_lock`+`claim_expires` (15min TTL) + heartbeats | External — adapter `execute()` returns when work done |
| Lifecycle statuses | triage/todo/ready/running/blocked/done/archived | open/in_progress/closed (Paperclip-defined) |
| Auto-decompose | ✅ `/tasks/{id}/decompose` (auxiliary LLM) | ❌ human-authored subtasks only |
| Cross-task DAG | ✅ `task_links(parent_id, child_id)` first-class | references in body only |
| Retry / circuit-breaker | ✅ `consecutive_failures` + `max_retries` + `task_runs` history | ❌ adapter-owned |
| Per-attempt history | ✅ `task_runs` rows per claim | ❌ |
| Channel notifications | per-task `kanban_notify_subs` (notify on completion) | per-agent channel binding (wake on incoming message) |
| UI | dashboard plugin (Vite/React `web/src/pages/`) | our `apps/frontend/src/app/teams/` |
| Cross-tenant search | ❌ N/A | ✅ Paperclip company scope |

### 7B.7 The layering — not competitors

These operate at different layers:

```
┌─────────────────────────────────────────────────────────────┐
│  Paperclip (multi-tenant, human-facing)                     │
│    - Issues + threads + messages + agents + channels        │
│    - Per-company governance, members, roles                 │
│    - Backed by Aurora                                       │
└─────────────────────────────────────────────────────────────┘
                            ↓ adapter.execute()
                            ↓ ("agent X, process issue Y")
┌─────────────────────────────────────────────────────────────┐
│  Per-user container (one runtime — OpenClaw or Hermes)      │
│                                                             │
│   ┌─────────────────────────────────────────────────────┐   │
│   │  Agent runtime (OpenClaw OR Hermes)                 │   │
│   │   - Receives "process issue Y"                      │   │
│   │   - Runs conversation loop, calls tools             │   │
│   │   - Returns summary + status                        │   │
│   │                                                     │   │
│   │   Hermes-only: agent can use kanban_* tools to      │   │
│   │   internally decompose issue Y into subtasks A/B/C, │   │
│   │   coordinate sub-workers via CAS, retry on failure  │   │
│   │   — all INSIDE the container.                       │   │
│   └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

Hermes kanban is *not* trying to replace what Paperclip does. Hermes' own docs call out the layering explicitly: "Hermes Kanban owns lifecycle truth" for **its own tasks** (sub-divisions of work the agent does internally), while "Worker lanes execute work" routed to the kanban by external systems (which is what Paperclip is doing when it tells the agent to process an issue).

### 7B.8 Integration options for dual-harness

Three options for how Hermes' internal kanban surfaces back to Paperclip:

**(a) Ignore — same as OpenClaw today.** Paperclip says "process issue Y"; the Hermes container runs the agent; the agent may or may not use kanban internally; only the final summary + status comes back via the adapter `execute()` result. Internal kanban state stays internal. **This is the right default.** Matches how OpenClaw works (no internal task board exposed); zero new integration code; gives Hermes-flavored agents a real capability over OpenClaw-flavored ones without forcing UX changes.

**(b) Bridge subtasks back as Paperclip child-issues.** When Hermes' kanban creates a subtask via `/tasks/{id}/decompose` or `kanban_create`, also POST a Paperclip child-issue linked to the parent. Symmetric edits in either direction. **High complexity (two-way sync, conflict resolution, lifecycle alignment); defer to v2 if user demand emerges.**

**(c) Replace Paperclip issues with Hermes kanban.** Wrong direction — Hermes kanban is single-tenant per HERMES_HOME, can't serve as a per-company Inbox. Don't.

### 7B.9 Bonus capability for the dual-harness pitch

Hermes' dispatcher can spawn external workers via plugin lanes (`kanban-worker-lanes.md:46-47`):

> For non-Hermes lanes (registered via a plugin), the plugin supplies its own `spawn_fn` callable that gets `task`, `workspace`, and `board` and returns an optional pid for crash detection.

That means in theory Paperclip could register itself as a non-Hermes lane — Hermes' internal kanban could route tasks back out to Paperclip-managed work. We do **not** want this direction in the foreseeable future (it inverts the orchestration: Paperclip becomes the executor and Hermes the dispatcher, instead of vice versa). Worth noting it exists in case the product direction ever flips.

### 7B.10 Net for the runtime selector UX

On the user-facing side, a Hermes container can offer features OpenClaw can't:
- **Agents that can durably decompose long-running work** without losing state on crash — the kanban's `task_runs` + CAS reclaim + `consecutive_failures` circuit breaker give agents a way to handle tasks that need 30 minutes of work, can crash mid-flight, and resume cleanly.
- **First-class DAG of subtasks** (parent/child links) for complex projects.
- **Auto-decomposition via auxiliary LLM** as a built-in capability.

These don't *require* exposing the kanban to Paperclip — they're internal agent superpowers that make Hermes-backed agents more capable, while the Paperclip integration shape stays identical. That's a clean dual-harness story: "Hermes container = better agent runtime for complex work; OpenClaw container = lighter for simple chat."

---

## 4. Identity & seed files (Friday, Atlas)

### 4.1 Today: OpenClaw per-agent identity

`apps/backend/core/services/friday_identity.py:1-27` (module docstring) describes the current scheme. Pre-staged BEFORE the container boots:

| File | Purpose | Source |
|---|---|---|
| `IDENTITY.md` | Name, Vibe, Emoji, Avatar (40-60 lines) | `_IDENTITY_TEMPLATE` (line 46) |
| `SOUL.md` | Long-form identity prompt — "who you are" (~600 lines) | `_SOUL_TEMPLATE` (line 63) |
| `USER.md` | About the human/org owner — varies by owner kind | `_USER_TEMPLATE_PERSONAL` (line 147) / `_USER_TEMPLATE_ORG` (line 169) |
| `BOOTSTRAP.md` | First-message behavior — how Friday should open | `_BOOTSTRAP_TEMPLATE` (line ~200) |
| `.openclaw/workspace-state.json` | `{setupCompletedAt: now}` | written to prevent OpenClaw from deleting BOOTSTRAP.md on first boot (`friday_identity.py:11-19`) |

OpenClaw later fills (when the container actually boots): `AGENTS.md`, `MEMORY.md`, `TOOLS.md`, `HEARTBEAT.md` — but only for files we didn't pre-stage (`writeFileIfMissing` honors any file we wrote first per the docstring).

These files live under the per-agent EFS dir: `/mnt/efs/users/{owner_id}/agents/{agent_uuid}/agent/`. Friday and Atlas each have their own dir. They are addressable as separate agents in OpenClaw's `agents.list[]` slots.

### 4.2 Hermes' identity surfaces

Hermes has three distinct mechanisms that together cover what OpenClaw's per-agent files do:

#### a) `SOUL.md` at HERMES_HOME root

One file per HERMES_HOME at `~/.hermes/SOUL.md` (`hermes_cli/config.py:409-413`). Auto-seeded from `hermes_cli/default_soul.py:3-11` on first run (default content is a generic ~10-line "you are Hermes Agent" prompt). User-edited to customize. Auto-injected into the system prompt unless `skip_context_files=True` is set on the AIAgent init (`agent_init.py:182-184`).

Plus `AGENTS.md` and `.cursorrules` auto-injected from cwd alongside SOUL.md (`hermes_cli/_parser.py:211`).

This is the closest analog to OpenClaw's SOUL.md — but it's container-shared, not per-agent.

#### b) `personalities` dict in `config.yaml`

From `hermes_cli/config.py:1428-1431`:

```yaml
# Custom personalities — add your own entries here
personalities: {}
```

And the default at line 970:

```yaml
personality: "kawaii"
```

Each personality is either a string (the system prompt) or a dict with `{system_prompt, tone, style, description}` (per `cli.py:7231-7240` `_resolve_personality_prompt`). Switching personalities via `/personality <name>` writes `agent.system_prompt` to config.yaml (`cli.py:7305`) and forces agent re-init.

This is a system-prompt overlay, not a full identity scaffold. No per-personality MEMORY.md, no per-personality TOOLS.md.

#### c) Per-call `ephemeral_system_prompt`

`agent_init.py:200`: `agent.ephemeral_system_prompt = ephemeral_system_prompt`. The orchestrator passes a system prompt at every agent init — used during execution but **NOT** saved to trajectories (per `agent_init.py:157` docstring).

This is the most flexible — no on-disk artifact, identity injected per-request. For an orchestrator like our backend that already manages agent identities, this is the natural integration point.

#### d) Profiles (full sub-HERMES_HOMEs)

`hermes_cli/profiles.py:1-22` (module docstring):

> Each profile is a fully independent HERMES_HOME directory with its own config.yaml, .env, memory, sessions, skills, gateway, cron, and logs. Profiles live under `~/.hermes/profiles/<name>/` by default.

Each profile gets its own SOUL.md. Profiles are switched at process boot via `hermes -p <name>` or the active-profile sticky file (`~/.hermes/active_profile`). **One active profile per process** — to have Friday and Atlas as separate profiles we'd need either two daemons on different ports, OR run-time profile selection per request (not currently supported).

### 4.3 Mapping Friday + Atlas to Hermes

Four design options, ordered by how closely they mirror OpenClaw today:

#### Option A — Personalities + `ephemeral_system_prompt` (recommended)

- SOUL.md at HERMES_HOME stays generic ("you are an Isol8 agent")
- `config.yaml#personalities` declares Friday and Atlas with their rendered system prompts (combining today's IDENTITY + SOUL + USER + BOOTSTRAP into a single per-personality `system_prompt` string)
- Our backend's `friday_identity` writer becomes a `hermes_friday_identity` writer that produces the personality dict instead of separate IDENTITY/SOUL/USER files
- On each `/v1/runs` call, the orchestrator passes the personality's system prompt as `ephemeral_system_prompt` (and `X-Hermes-Session-Key` to scope memory)

Pros: minimal new infra, identity lives in our backend (centrally managed, easy to update without container restart), maps cleanly to Hermes' per-call kwargs model.
Cons: loses the "files on disk that Friday can edit her own identity" property — IDENTITY.md becomes a frontend abstraction over a backend config row. Hermes' built-in memory (Honcho) covers the "agent learns about itself over time" use case differently.

#### Option B — Per-personality directory under HERMES_HOME

Treat each personality as a sub-directory with its own AGENTS.md / MEMORY.md / etc. Hermes doesn't natively support this — there's `personalities` (config-only) or `profiles` (full HERMES_HOME). We'd add a convention (`~/.hermes/personalities/friday/{AGENTS.md, MEMORY.md, …}`) and write a custom auto-injection hook.

Pros: closest mirror of OpenClaw's per-agent dir model. Friday and Atlas can write to their own MEMORY.md independently.
Cons: requires forking Hermes or writing a custom plugin to load per-personality context files. Adds drift surface area.

#### Option C — Friday and Atlas as separate Hermes profiles

`~/.hermes/profiles/friday/SOUL.md`, `~/.hermes/profiles/atlas/SOUL.md`, etc. Strongest isolation.

Pros: identical to OpenClaw's per-agent dir; each has its own everything (config, skills, sessions, memory).
Cons: requires two daemons per container (different ports), doubles container resource use, complicates the `connection_pool` → which daemon do we route to?

#### Option D — Pure per-call (no on-disk identity at all)

The orchestrator (our backend) holds Friday's full identity prompt in DDB; passes it as `ephemeral_system_prompt` on every call. Hermes' SOUL.md stays at the default. Nothing about Friday is persisted on the container.

Pros: simplest, most reversible, cleanest dual-harness abstraction.
Cons: Friday's "memory of her own identity" (the SOUL.md she could edit) goes away entirely. Memory of the user, conversation history, skills — all still work via Hermes' built-ins. Only the self-modifiable identity is lost.

### 4.4 Recommendation for the dual-harness spike

**Option A.** Reasons:

1. It's the smallest delta from today's UX. Users interact with "Friday the agent"; the orchestration layer handles the file-vs-config difference.
2. It matches how the Hermes API server is designed to be driven (per-call kwargs).
3. It's reversible — if we later want richer per-agent identity, we can move to Option B by writing a small Hermes plugin without breaking the integration.
4. It works identically for marketplace-deployed agents (each becomes a personality at deploy time).

The friday_identity writer splits into two functions in our backend:
- `friday_openclaw.write_identity_files(workspace, owner_profile)` — current behavior, for OpenClaw containers
- `friday_hermes.write_personality_config(workspace, owner_profile)` — new, for Hermes containers — produces a `personalities.friday` entry in `config.yaml`

Both compile from the same canonical `OwnerProfile` data we already build in `friday_identity._resolve_owner_profile`. The templates that produce per-agent prompt text can be shared between the two writers (one rendered into IDENTITY/SOUL/USER markdown files for OpenClaw, the other concatenated into a single personality `system_prompt` string for Hermes).

### 4.5 Open questions for the spike

1. **Can Hermes' personality system_prompt be ~600 lines?** Hermes' default personality prompts (`hermes_cli/config.py:1428-1431`) are typically a few sentences. Loading a 600-line system prompt is fine for any LLM, but verify Hermes doesn't truncate or apply weird prompt-builder transformations to personality strings vs ephemeral_system_prompt.
2. **Does `ephemeral_system_prompt` *replace* or *append to* the personality prompt?** From `agent_init.py:157` ("ephemeral_system_prompt: System prompt used during agent execution but NOT saved to trajectories") it sounds like a replacement, but verify against `agent/prompt_builder.py` behavior.
3. **AGENTS.md auto-injection** — Hermes loads AGENTS.md from cwd; in a container started by our orchestrator, what *is* cwd? Verify we can control it via the `workspace` field on `POST /v1/runs`.

---

## 5. Provider auth (Bedrock, ChatGPT OAuth, BYOK)

### 5.1 Today: how we wire each provider into OpenClaw

| Provider | Tokens live in | Pre-staged to container how |
|---|---|---|
| Bedrock | AWS IAM (ECS task role) | No on-disk credentials — `openclaw.json#models[bedrock]` config + IAM role attached to task definition |
| ChatGPT OAuth | DDB `oauth-chatgpt` table, encrypted | `pre_stage_auth_profile_store` writes `<EFS>/users/{user_id}/agents/main/agent/auth-profiles.json` in OpenClaw's `AuthProfileStore` JSON shape (`workspace.py:700-795`) |
| BYO API key | DDB `api-keys` table, Fernet-encrypted via `core/encryption.py` | Decrypted and inlined into `openclaw.json#models[...].apiKey` at config-write time (`core/containers/config.py:write_openclaw_config`) |

ChatGPT OAuth file shape (`workspace.py:746-765`):

```json
{
  "version": 1,
  "profiles": {
    "openai-codex:codex-cli": {
      "type": "oauth",
      "provider": "openai-codex",
      "access": "<access_token>",
      "refresh": "<refresh_token>",
      "expires": <ms_epoch>,
      "email": "<optional>"
    }
  }
}
```

File owned `uid=1000, gid=1000` so OpenClaw's in-container refresh logic can rotate it in-place (`workspace.py:780-786`). The `_AUTH_STORE_VERSION = 1` and `_CODEX_CLI_PROFILE_ID = "openai-codex:codex-cli"` constants must be bumped in lockstep with `openclaw-version.json#upstream` if upstream changes the schema (`workspace.py:686-697`).

### 5.2 Hermes' equivalent

#### Bedrock
`hermes-agent/agent/bedrock_adapter.py` uses the AWS SDK's default credential chain. Drop the same IAM-roled ECS task definition in and Bedrock works identically. No on-disk creds.

#### ChatGPT OAuth — `auth_mode: "chatgpt"`
`hermes_cli/auth.py:2697,2902,4572,5680` defines `auth_mode: "chatgpt"`. Tokens land in two places (`agent/credential_sources.py:294-298,321`, verbatim):

> Codex tokens live in TWO places: our auth store AND ~/.codex/auth.json. `refresh_codex_oauth_pure()` writes both every time, so clearing only the Hermes auth store is not enough — `_seed_from_singletons()` would re-import from `~/.codex/auth.json` on the next `load_pool()` call and the removal would be instantly undone.

The two-file dance is structurally identical to OpenClaw's (a Hermes auth store + a shared Codex CLI file) — only the paths differ:

| | OpenClaw | Hermes |
|---|---|---|
| Primary store | `<agentDir>/auth-profiles.json` (per-agent) | `~/.hermes/auth.json` (HERMES_HOME-wide) |
| Shared Codex CLI file | `~/.codex/auth.json` (canonical Codex CLI location) | `~/.codex/auth.json` (same) |
| Profile key | `"openai-codex:codex-cli"` | `"openai-codex"` (per credential_sources nomenclature) |
| Refresh owner | OpenClaw runtime | Hermes' `refresh_codex_oauth_pure()` |
| Base URL | `https://chatgpt.com/backend-api/codex` (`auth.py:74` `DEFAULT_CODEX_BASE_URL`) | same |

The actual on-disk shape for Hermes' `auth.json` needs verification before writing the spike — Hermes' `agent/credential_pool.py` + `credential_sources.py` define how it parses the file. From the symbol surface, it's likely a similar `{providers: {...}}` shape, but I haven't verified field names. (Open question for the spike.)

#### v0.14.0 OAuth Provider Proxy (potentially game-changing)
Hermes v0.14.0 (released 2026-05-16) shipped an "OAuth Provider Proxy" that wraps Claude Pro / ChatGPT Pro / SuperGrok as an OpenAI-compatible HTTP endpoint. If this proxy can be hosted alongside the Hermes container (or pointed at a centralized internal proxy), the per-container file dance goes away — the container sees a plain bearer-authed HTTP endpoint, and OAuth lifecycle moves to the proxy.

**Implication for the spike:** worth a half-day to evaluate whether the OAuth Provider Proxy can serve our hosted use case (per-user token isolation, refresh under our control). If yes, this is materially better than the two-file dance for both OpenClaw and Hermes paths long-term.

#### BYOK
Hermes accepts BYOK via either `~/.hermes/.env` (`hermes_cli/config.py:4-6` defines `~/.hermes/.env` as the secrets location) or `custom_providers` key in `config.yaml` (`agent/credential_sources.py:11`). For our hosted use, the `.env` write at provision time is closest to today's OpenClaw flow: decrypt the user's key from DDB → write `OPENAI_API_KEY=…` / `ANTHROPIC_API_KEY=…` to `~/.hermes/.env` on EFS.

### 5.3 Mapping for the dual-harness backend

Refactor `pre_stage_auth_profile_store` (which is OpenClaw-specific in its file paths + JSON shape) into a runtime-aware writer:

```py
# new abstraction
class RuntimeAuthWriter(Protocol):
    async def pre_stage_chatgpt_oauth(self, *, user_id: str, oauth_tokens: dict) -> None: ...
    async def pre_stage_byok(self, *, user_id: str, provider: str, api_key: str) -> None: ...
    async def remove_chatgpt_oauth(self, *, user_id: str) -> None: ...

class OpenClawAuthWriter(RuntimeAuthWriter):
    # writes <EFS>/users/{user_id}/agents/main/agent/auth-profiles.json
    # inlines BYOK into openclaw.json at config-write time

class HermesAuthWriter(RuntimeAuthWriter):
    # writes <EFS>/users/{user_id}/hermes/auth.json
    # writes <EFS>/users/{user_id}/hermes/.codex/auth.json
    # writes <EFS>/users/{user_id}/hermes/.env with BYOK lines
```

Caller (`oauth_service.py`, `billing_service.py`, `config.py`) selects the writer via the container row's `runtime_kind`.

### 5.4 Open questions for the spike

1. **Hermes `auth.json` exact schema** — read `hermes_cli/auth.py` `refresh_codex_oauth_pure()` to verify field names. Mock the write, boot a Hermes container, verify it picks up the tokens without an interactive prompt.
2. **Token refresh ownership** — if Hermes refreshes the access token in-place, our backend's DDB row becomes stale. Either accept the divergence (DDB is "the seed", on-disk is "the current"), or have Hermes write back via a webhook (it has webhook support — `gateway/platforms/webhook.py`).
3. **`auth.json` vs `~/.codex/auth.json` precedence** — if both files exist and disagree, which wins? Per Hermes' "removed entry resurrects from shared file" behavior (`credential_sources.py:294-308`), the shared file is the durable source. Write both at provision time but treat the Hermes-side as authoritative.
4. **OAuth Provider Proxy evaluation** — separately, evaluate v0.14.0's OAuth Provider Proxy as a unified replacement for both the OpenClaw and Hermes two-file dances. If it works in a hosted-multi-tenant setup, it's a strict simplification.

---

## 6. Marketplace artifact mapping

### 6.1 Today: OpenClaw-shaped artifact

A marketplace listing has three S3 artifacts at `s3://$MARKETPLACE_ARTIFACTS_BUCKET/listings/{id}/v{n}/`:

1. **`workspace.tar.gz`** — the seller's agent EFS dir, tarred with deterministic mtime=0, uid/gid=0 (`agent_export.py:174-183`). Filters: `_OPENCLAW_RUNTIME_STATE_DIRS = {"sessions", "agent"}` (line 88), dotfiles, symlinks, junk dirs.
2. **`manifest.json`** — file count, safety flags, agent_id, slice version.
3. **`openclaw-slice.json`** — the config slice. Shape (`catalog_slice.py:258-294`):

```json
{
  "agent": {  // from openclaw.json#agents.list[N], user-specific fields stripped
    "id": "<seller's agent id, replaced at deploy>",
    "name": "...",
    "systemPromptOverride": "...",
    "model": "...",
    "skills": ["..."],
    "tools": {"allow": ["..."], "deny": ["..."]},
    "sandbox": {...},
    "identity": {...},
    ...
  },
  "plugins": { /* copy of openclaw.json#plugins */ },
  "tools": { /* copy of openclaw.json#tools */ },
  "mcp": {  // optional, when seller selected specific MCPs
    "servers": {
      "<name>": { /* scrubbed entry — apiKey, env, headers, webhookToken zeroed */ }
    }
  },
  "skills": {  // optional, when seller selected specific skills
    "entries": {"<name>": {/* scrubbed */}},  // ENTRIES-bucket
    "allowBundled": ["<name>", ...]            // BUNDLED-bucket
  },
  "cron_jobs": [ /* publisher's cron jobs, filtered + scrubbed */ ]
}
```

Deploy flow (`catalog_service.deploy_from_artifact`, `catalog_service.py:97-211`):

1. Generate `new_agent_id = "agent_<uuid12>"` (line 118)
2. Extract `workspace.tar.gz` → `<EFS>/users/{owner}/.openclaw/workspaces/{new_agent_id}/` (line 120-124)
3. Build agent entry: deep-copy slice.agent, override `id = new_agent_id`, `workspace = ".openclaw/workspaces/{new_agent_id}"` (lines 135-137)
4. Atomically (under config file lock): deep-merge `agent_entry` into `openclaw.json#agents.list`, merge `plugins_patch` into `#plugins`, merge `mcp_patch` into `#mcp.servers`, merge `skills_patch` into `#skills` (lines 155-161)
5. Write template-tracking sidecar with slug + version + deployed_at (lines 163-171)
6. Append `cron_jobs` to owner's `cron/jobs.json`, regenerating IDs and wiring in the new agent_id (lines 180-191)

Rollback (lines 192-220): on any post-extract failure, `cleanup_agent_dirs` removes the EFS workspace AND `remove_from_openclaw_config_list` removes the agent entry to avoid dangling registrations.

### 6.2 Hermes-shape equivalent

The artifact structure is reusable, only the slice contents change:

1. **`workspace.tar.gz`** — the agent's working directory. Hermes doesn't have per-agent EFS dirs (`agents/<uuid>/` is OpenClaw's convention) — instead it has `~/.hermes/workspace/` (the agent's working dir for files it edits) + `~/.hermes/sandboxes/` (per-run sandbox dirs). The publishable equivalent is `~/.hermes/workspace/<seller's-namespace>/`, tarred deterministically.

2. **`manifest.json`** — same shape; just add `runtime_kind: "hermes"`.

3. **`hermes-slice.json`** (new) — config slice for Hermes:

```json
{
  "personality": {  // becomes config.yaml#personalities[name]
    "name": "<seller's personality name>",
    "system_prompt": "...",
    "tone": "...",      // optional
    "style": "...",     // optional
    "description": "..."
  },
  "mcp_servers": {  // becomes config.yaml#mcp_servers
    "<name>": { /* scrubbed */ }
  },
  "skills": [  // becomes config.yaml#skills entries (or installed in ~/.hermes/skills/ via skills_hub)
    {"name": "...", "source": "github:user/repo", "version": "..."}
  ],
  "cron_jobs": [  // becomes entries appended to ~/.hermes/cron/jobs.json
    {"schedule": "0 9 * * *", "prompt": "...", "skills": [...]}
  ],
  "toolset_overrides": {  // becomes config.yaml#platform_toolsets.api_server.enabled (additive)
    "enabled": ["..."],
    "disabled": ["..."]
  }
}
```

Deploy flow (proposed `hermes_catalog_service.deploy_from_artifact`):

1. Generate `new_personality_name = "agent_<uuid12>"` (or use seller's name with collision suffix)
2. Extract `workspace.tar.gz` → `<EFS>/users/{owner}/hermes/workspace/{new_personality_name}/`
3. Atomically write to config.yaml (under file lock): add `personalities.{new_personality_name} = slice.personality`, merge `mcp_servers` into `config.yaml#mcp_servers`, install skills via `hermes skill install` semantics
4. Append cron jobs to `~/.hermes/cron/jobs.json`
5. Write sidecar (same as today) so we can track template version per deployed personality

Differences from OpenClaw deploy:
- No per-agent registration "slot" — personality is a config dict entry, not an agents.list[] element
- Skills install via Hermes skills_hub (federated GitHub-backed) rather than the bundled-skill registration model OpenClaw uses
- Sandbox config doesn't transfer per-personality — it's container-wide in Hermes (`TERMINAL_SANDBOX_DIR`)

### 6.3 Cross-runtime deploy: reject by default

If a buyer with `runtime_kind="hermes"` tries to deploy a listing with `runtime_kind="openclaw"` (or vice versa), the deploy endpoint should **reject with a clear error**, not attempt translation. Reasons:

- Skills installation is structurally different (entries dict vs hub-installed)
- Tools allow/deny lists may reference container-specific tool names (Hermes' `kanban_*` tools, OpenClaw's `talk` tool)
- MCP server config shape is mostly compatible but transport defaults differ (HTTP MCPs work either way; stdio MCPs need different commands)
- Agent identity (system prompt + skill subset) translates cleanly, but the surrounding "agent slot" semantics don't

A future v2 could add an opt-in translator (`POST /deploy?translate=true`) for listings where the seller-declared compatibility says "this works on both." Most agents probably are translatable — the core system prompt + skill selection + MCP usage is shared — but forcing it without seller intent risks surprising behavior.

### 6.4 Marketplace listing schema additions

`marketplace-listings` DDB table needs:
- `runtime_kind: "openclaw" | "hermes"` (required for new listings; existing listings default to `"openclaw"`)
- Listing search at `marketplace_search.py` filters by buyer's container `runtime_kind` so the catalog only shows compatible listings

The Status state machine (draft → review → published → retired) stays unchanged.

Admin approval queue (`routers/marketplace_admin.py`) stays unchanged — admin still vets all submissions regardless of runtime.

### 6.5 Open questions for the spike

1. **Hermes skill installation in a container** — `hermes skill install github:user/repo` works on a desktop. In our hosted container, does it have outbound git/GitHub access? Audit-deny lists for safety scanning of installed skills?
2. **Cron-job ID regeneration** — Hermes' cron table uses different ID conventions than OpenClaw's. Verify `_remap_cron_jobs_for_deploy` analog works against Hermes' job schema.
3. **Tarball runtime-state filter** — `_OPENCLAW_RUNTIME_STATE_DIRS = {"sessions", "agent"}` filters OpenClaw's per-agent runtime state. Hermes' equivalent is `~/.hermes/{sessions, checkpoints, cron/output, .anthropic_oauth.json, state.db}` — all OUTSIDE the publishable `workspace/` dir. So Hermes' tarball can simply be `workspace/<seller>/` raw, with fewer filters needed. Verify nothing leaks via `~/.hermes/memories/` (per-personality memory might or might not be exportable).
4. **Skill bundle scrubbing** — `marketplace_safety.scan_tarball` works on file content patterns (API keys, AWS access keys, etc.). Should still work on Hermes tarballs without changes since the patterns are content-shape based, not runtime-shape based.

---

## 8. Frontend & infra blast radius

The blast radius for dual-harness is much smaller than for migration. The key insight: today's gateway/frontend code is tightly coupled to OpenClaw's WS-RPC protocol, but the dual-harness model adds a parallel path rather than replacing the existing one. Existing OpenClaw containers + their frontend wiring stay untouched.

### 8.1 Backend files with OpenClaw coupling

`grep "openclaw" apps/backend/core apps/backend/routers` returns **57 files**. Categorized:

**Hard-coded to OpenClaw protocol (would need a runtime-aware abstraction):**
- `core/gateway/connection_pool.py` (1761 lines) — `from websockets import connect as ws_connect`, `GATEWAY_PORT = 18789`, OpenClaw protocol v4 frame handling
- `core/containers/config.py` (`write_openclaw_config`, `read_openclaw_config_from_efs`) — JSON writer for `openclaw.json` schema
- `core/services/config_patcher.py` — deep-merge into `openclaw.json` (used by marketplace deploy and admin updates)
- `core/services/friday_identity.py` — writes IDENTITY/SOUL/USER markdown to `agents/<id>/agent/`
- `core/containers/workspace.py:686-795` `pre_stage_auth_profile_store` — OpenClaw AuthProfileStore JSON shape

**Hard-coded to OpenClaw filesystem layout:**
- `core/services/agent_export.py` — `_OPENCLAW_RUNTIME_STATE_DIRS = {"sessions", "agent"}`, `agents/<id>/agent/` path conventions
- `core/services/catalog_service.py` — `.openclaw/workspaces/{new_agent_id}` deploy path
- `core/services/catalog_slice.py` — slice extraction from `openclaw.json` shape
- `routers/workspace_files.py` — file paths assume OpenClaw's per-agent dir layout

**OpenClaw-specific RPC methods (would each need a Hermes equivalent):**
- `routers/container_rpc.py` — proxies `agents.list`, `agents.create`, `sessions.list`, `chat.send`, etc. via `connection_pool`
- `routers/control_ui_proxy.py` — proxies OpenClaw's built-in control UI SPA (Hermes has no equivalent SPA; has the `web/` dashboard instead)
- `routers/hooks_proxy.py` — OpenClaw hooks
- `routers/updates.py` — `openclaw_id`-keyed pending-update queue
- `routers/debug.py` — `PATCH /debug/provision` rewrites `openclaw.json`

**Brand-only references (no behavioral coupling):**
- `main.py:366,370,393,563,594` — OpenAPI advertises "OpenClaw-powered" in description fields
- Various admin redaction lists, log prefixes, metric names

### 8.2 Frontend WS-coupled files

| File | LoC | Coupling shape |
|---|---|---|
| `apps/frontend/src/hooks/useGateway.tsx` | 514 | WebSocket client provider — reconnect loop, ping/pong (300s timeout), event pub-sub, RPC req/res matching |
| `apps/frontend/src/hooks/useAgentChat.ts` | 1103 | Chat surface — chunk/tool_start/tool_end/done event handling, thinking blocks, cancellation via `chat.abort` RPC |
| `apps/frontend/src/hooks/useGatewayRpc.ts` | (small) | SWR wrapper for RPC calls via `useGateway` — read + mutation |
| `apps/frontend/src/hooks/useAgents.ts` | (small) | `agents.list/create/update/delete` via RPC |
| `apps/frontend/src/hooks/useExecApprovals.tsx` | — | Exec-approval queue subscriber |
| `apps/frontend/src/hooks/useSystemHealth.ts` | — | Gateway state for health indicator |
| `apps/frontend/src/components/chat/AgentChatWindow.tsx` | — | Consumes `useAgentChat` |
| `apps/frontend/src/components/chat/ChatLayout.tsx` | — | Consumes `useAgentChat` + `useGateway` |
| `apps/frontend/src/components/chat/ProvisioningStepper.tsx` | — | Subscribes to gateway connection state |

Today these talk to `wss://ws[-env].isol8.co` (the WebSocket API Gateway). For a Hermes-backed container, the frontend would need a parallel transport. Two options:

**(a) Frontend stays WS, backend translates.** The backend's chat router becomes the abstraction boundary. Frontend sends `chat.send` over WS → backend's `connection_pool` (now a `RuntimeBackend` interface) either forwards to OpenClaw's WS-RPC or translates to Hermes' `POST /v1/runs` + SSE. Same frontend code path either way.

**(b) Frontend learns about runtime.** Frontend reads container's `runtime_kind` on connect; for Hermes containers, opens an SSE connection to `https://hermes-ws[-env].isol8.co/v1/runs/{id}/events` directly. Bypasses backend for streaming.

Recommend **(a)** — keeps the frontend runtime-agnostic, all complexity stays in the backend abstraction.

### 8.3 CDK infra (per `apps/infra/lib/stacks/api-stack.ts`)

WebSocket-specific resources, line cited:

| Resource | Line | Purpose |
|---|---|---|
| `WebSocketApi`, `WebSocketStage` | 432, 455 | API Gateway V2 WebSocket API |
| `CfnVpcLink` | 120 | VPC Link V1 for WebSocket APIGW (required — V2 only supports NLB) |
| `isol8-${env}-ws-connections` table | 253 | DDB connectionId → user_id map |
| Lambda authorizer | 281-293 | Clerk JWT validation at WS connect |
| Network Load Balancer | (network-stack.ts) | Required by VPC Link V1 for WS APIGW |

For dual-harness:
- **OpenClaw containers continue to use this entire stack as-is.** No infra changes for the existing path.
- **Hermes containers add a parallel ingress:** Either an additional ALB host route (`https://hermes-ws[-env].isol8.co` → Hermes container's API server) or backend-mediated (frontend WS → backend → backend opens HTTP+SSE to Hermes container). Recommend backend-mediated for option (a) above.

**No infra is being torn down in the dual-harness model.** This is a key distinction from the migration framing — we keep the existing WS plumbing alive for OpenClaw, add backend-side HTTP+SSE client for Hermes, and ship.

### 8.4 The `RuntimeBackend` abstraction interface

To support both cleanly, the backend gateway layer needs a runtime-aware abstraction. Proposed in `apps/backend/core/runtime/`:

```py
class RuntimeBackend(Protocol):
    """One implementation per supported runtime (OpenClaw, Hermes)."""

    async def connect(self, user_id: str) -> RuntimeConnection: ...
    async def list_agents(self, user_id: str) -> list[Agent]: ...
    async def create_agent(self, user_id: str, spec: AgentSpec) -> Agent: ...
    async def send_chat(
        self,
        user_id: str,
        agent_id: str,
        message: str,
        session_key: str,
        on_event: Callable[[ChatEvent], Awaitable[None]],
    ) -> ChatResult: ...
    async def abort_chat(self, user_id: str, run_id: str) -> None: ...
    async def resolve_approval(self, user_id: str, run_id: str, choice: ApprovalChoice) -> None: ...

class OpenClawBackend(RuntimeBackend):
    # Wraps today's connection_pool.py — WS-RPC to wss://ws[-env].isol8.co

class HermesBackend(RuntimeBackend):
    # New: HTTP+SSE client to https://hermes-ws[-env].isol8.co
    # POST /v1/runs, GET /v1/runs/{id}/events (SSE), POST /v1/runs/{id}/approval
```

Callers (`routers/container_rpc.py`, `routers/websocket_chat.py`, `routers/control_ui_proxy.py`'s replacement, etc.) switch on the container row's `runtime_kind` to select the backend.

**Files that need refactoring under this abstraction:**
- `core/gateway/connection_pool.py` → move to `core/runtime/openclaw/connection_pool.py`; expose via `OpenClawBackend`
- `core/runtime/hermes/client.py` (new) — HTTP+SSE client
- `core/runtime/__init__.py` (new) — factory: `get_runtime_backend(runtime_kind: str) → RuntimeBackend`
- `core/containers/config.py` → split into `openclaw_config.py` + `hermes_config.py` (yaml writer)
- `core/services/friday_identity.py` → split into `friday_openclaw.py` + `friday_hermes.py`
- `core/services/agent_export.py` → split or add `runtime_kind` switching
- `core/services/catalog_service.py` → switch `_apply_deploy_mutation` based on container's runtime
- ~25 router files that today call `gateway_pool.request(...)` → call `runtime_backend.send_chat(...)` etc.

Estimated backend refactor: ~3-5 weeks of focused engineering, mostly mechanical interface extraction. The actual *new* code (HermesBackend, hermes_config writer, hermes-flavored friday_identity) is much less than the refactor surface.

### 8.5 What the user sees during the rollout

- **Today's users on OpenClaw:** nothing changes. Same WS endpoint, same chat UI, same agents.
- **New users post-rollout (default OpenClaw):** still get OpenClaw containers; same experience.
- **Beta opt-in users:** `runtime_kind = "hermes"` set on their container row at provisioning. Same frontend chat UI; backend routes through `HermesBackend` instead.
- **Eventual default flip (if Hermes wins):** new accounts default to `runtime_kind = "hermes"`. Existing OpenClaw users stay on OpenClaw forever (no forced migration) until they explicitly opt to recreate the container.
- **Eventual sunset of OpenClaw (only if Hermes decisively wins):** announce, give long migration window, eventually deprecate the OpenClaw backend. Reversible up to that point.

---

## 9. Spike scope & open questions

### 9.1 Minimum viable spike

Goal: ship one Hermes-backed container behind a beta flag, end-to-end, with one seeded personality and the Paperclip integration working — without disturbing any OpenClaw user.

Workstreams, in build order:

**1. `RuntimeBackend` abstraction in the backend (largest piece)**
- Define the interface (`core/runtime/__init__.py`, `core/runtime/types.py`)
- Wrap today's `connection_pool.py` as `OpenClawBackend` — no behavior change for existing users
- Stub `HermesBackend` with mock implementations
- Refactor callers in `routers/container_rpc.py`, `routers/websocket_chat.py`, `routers/control_ui_proxy.py` to use the interface
- All tests pass with OpenClaw path unchanged

**2. `HermesBackend` HTTP+SSE client**
- `core/runtime/hermes/client.py` — POST `/v1/runs` + SSE `/v1/runs/{id}/events` + POST `/v1/runs/{id}/approval` + POST `/v1/runs/{id}/stop`
- Bearer auth via container's per-user `API_SERVER_KEY` (provisioned in Secrets Manager)
- ALB idle-timeout handling (bump to 5min OR reconnect via `Last-Event-ID` — open question #1 below)
- Map Hermes SSE events (`message.delta`, `approval.request`, status transitions) to our internal `ChatEvent` shape

**3. Hermes container provisioning**
- Add `runtime_kind: "openclaw" | "hermes"` to `containers` DDB row
- Extend `ecs_manager.provision_user_container` to launch Hermes Docker image when `runtime_kind="hermes"`
- New CDK resources: Hermes ECR repo (or use upstream image), task definition with Hermes-flavored env vars (`HERMES_HOME=/mnt/efs/users/{user_id}/hermes`, `API_SERVER_KEY` from Secrets Manager)
- EFS access point convention: `/users/{user_id}/hermes/` matches `~/.hermes` inside container
- ALB host route: `https://hermes-ws[-env].isol8.co/{user_id}/v1/*` → user's Hermes container (or just put the container behind Cloud Map and have the backend dial directly via service discovery, same as today for OpenClaw)

**4. Hermes config writer**
- `core/runtime/hermes/config_writer.py` — writes `~/.hermes/config.yaml` analogous to `write_openclaw_config`
- Handles: `personalities` (Friday + Atlas), `model`, `mcp_servers`, `platform_toolsets.api_server`, `cron` config
- BYOK injected as `~/.hermes/.env` lines

**5. Hermes auth writer**
- `core/runtime/hermes/auth_writer.py` — writes `~/.hermes/auth.json` + `~/.hermes/.codex/auth.json` for ChatGPT OAuth
- ChatGPT OAuth refresh ownership question (#3 below)

**6. Hermes Friday seeding**
- `core/services/friday_hermes.py` — produces the `personalities.friday` config entry from the canonical `OwnerProfile`
- Reuses template strings from `friday_identity.py` (which today render IDENTITY/SOUL/USER markdown files; here they concatenate into a single personality `system_prompt`)
- Same for Atlas

**7. `hermes_gateway` Paperclip adapter (new npm package)**
- `@isol8/adapter-hermes-gateway` (or upstream as `paperclip-adapter-hermes-gateway`)
- Mirrors `openclaw-gateway`'s shape: `index.ts` declares `type`, `label`, `agentConfigurationDoc`; `server/execute.ts` implements the HTTP+SSE flow
- ~400-600 lines TypeScript (vs ~1500 for openclaw-gateway, because no WS frame multiplexing, no ed25519, no `connect`/`agent`/`agent.wait` orchestration)
- Loaded by Paperclip via external plugin loader (`paperclip/server/src/adapters/registry.ts:430-453`)

**8. Backend Paperclip adapter synthesis for `hermes_gateway`**
- `core/services/paperclip_adapter_config.py` adds `synthesize_hermes_adapter()` peer to `synthesize_openclaw_adapter()`
- `paperclip_provisioning.py` switches on `runtime_kind` to call the right synthesizer at Friday/Atlas seed time

**9. Beta-flag plumbing**
- Frontend opt-in toggle ("Try Hermes runtime — beta") in account settings
- Backend gate: container creation flow reads the flag, sets `runtime_kind` on the new container row
- Migration path: existing users with OpenClaw containers cannot flip in place (would require re-provision); they can recreate their container via debug `DELETE /provision` + `POST /provision` to land on the new runtime

**10. Marketplace `runtime_kind` filter**
- Add `runtime_kind: "openclaw"` column to `marketplace-listings` DDB table (default for existing rows)
- `marketplace_search.py` filters listings by buyer's container `runtime_kind`
- Cross-runtime deploy rejected with clear error message

### 9.2 What's explicitly NOT in scope

- Hermes-flavored listings publish flow (sellers publishing for Hermes) — defer to v2 once we have signal on Hermes adoption
- Cross-runtime artifact translation — defer indefinitely; build only if demand emerges
- Replacing the WebSocket API Gateway / VPC Link V1 / NLB ingress — not needed in dual-harness model
- Exposing Hermes' internal kanban to Paperclip — explicitly Option (a) from §7B.8
- Hermes-flavored desktop app — desktop stays OpenClaw-bound for now; Hermes runtime users get the web app only until desktop integration is built
- Per-personality / per-marketplace-agent isolation beyond what Hermes natively provides — accept the container-wide MCP/cron/skill sharing (same as OpenClaw today)

### 9.3 Estimated effort

| Workstream | Effort (eng-weeks) |
|---|---|
| 1. RuntimeBackend abstraction + OpenClaw wrap | 2-3 |
| 2. HermesBackend HTTP+SSE client | 1-2 |
| 3. Hermes container provisioning + CDK | 1-2 |
| 4. Hermes config writer | 0.5-1 |
| 5. Hermes auth writer | 0.5-1 |
| 6. Hermes Friday seeding | 0.5 |
| 7. hermes_gateway Paperclip adapter | 1-2 |
| 8. Backend adapter synthesis | 0.5 |
| 9. Beta-flag plumbing | 0.5 |
| 10. Marketplace runtime_kind filter | 0.5 |
| **Total** | **8-13 eng-weeks** |

One engineer, focused, ~2-3 calendar months from start to "first internal user runs Friday on Hermes." Add another ~4 weeks of buffer for the open questions surfacing real complications during integration.

### 9.4 Risks

1. **Hermes release cadence.** v0.10→v0.14 in 30 days. Each upgrade audit costs time (cf. OpenClaw drift memories). We trade a known drift risk for an unknown one with faster cadence. Pinning a stable version + lagging upstream by 1-2 releases is the standard mitigation; budget 1-2 days per upstream bump for the same audit pattern we run on OpenClaw today.
2. **`hermes_gateway` upstream contribution.** If we want to upstream the Paperclip adapter package (rather than maintain it as a private `@isol8/*` fork), Paperclip review cycle is non-deterministic. Plan for the private-fork path; upstreaming is a bonus.
3. **OAuth Provider Proxy maturity.** v0.14.0 is brand new. If it has bugs in hosted multi-tenant scenarios, the file-dance fallback works but is the same complexity as today.
4. **Hermes "kanban inside the container" interactions.** If a Hermes agent uses kanban tools to write to its internal SQLite DB and that DB grows unboundedly, we need eviction policies. Not a blocker — Hermes' own per-task TTL handles this — but verify in load testing.
5. **Two systems to maintain forever.** If Hermes doesn't decisively win, we end up maintaining both indefinitely. Mitigate by setting a 6-month "decide and consolidate" review point at spike kickoff.

### 9.5 Open questions for the spike (consolidated)

From across all sections:

| # | Question | Section |
|---|---|---|
| 1 | Does Hermes' SSE emit `id:` lines (Last-Event-ID resume support)? If not, ALB idle-timeout disconnects can't auto-resume. | §7.5 |
| 2 | Does `POST /v1/runs` body's `metadata` field propagate into AIAgent so tools can read paperclip context? | §7.5 |
| 3 | ChatGPT OAuth token refresh ownership — who writes back when Hermes refreshes in-place? Webhook to our backend? | §5.4 |
| 4 | Hermes `auth.json` exact field schema — verify before writing the writer | §5.4 |
| 5 | Can Hermes personality `system_prompt` be ~600 lines without truncation? Behavior delta vs `ephemeral_system_prompt`? | §4.5 |
| 6 | OAuth Provider Proxy (v0.14.0) — usable in hosted multi-tenant? Could simplify both OpenClaw and Hermes paths. | §5.4 |
| 7 | Hermes skill installation in a container — needs outbound git/GitHub; safety scanning for installed skills | §6.5 |
| 8 | Hermes' AGENTS.md/SOUL.md/cwd interaction — can our orchestrator control cwd via `POST /v1/runs` `workspace` field? | §4.5 |

---

## Appendix: file inventory

### Primary sources cited

**Hermes Agent** (`hermes-agent/`):
- `gateway/run.py` (17,235 lines) — daemon entry, agent cache (lines 1264-1276, 14195-14274)
- `gateway/platforms/api_server.py` (3500+ lines) — OpenAI-compat HTTP server, route table at lines 3400-3423, run lifecycle at 2862-3157, approval at 3223-3299
- `gateway/platforms/api_server.py:780-794` — `X-Hermes-Session-Key` semantics docstring
- `tui_gateway/ws.py` — WebSocket transport for tui_gateway JSON-RPC
- `agent/agent_init.py:87-220` — AIAgent init kwargs
- `agent/credential_sources.py:294-298` — Codex tokens two-file dance
- `hermes_cli/auth.py:74,2697,2902,4572,5680` — ChatGPT OAuth mode
- `hermes_cli/config.py:409-413,970,1428-1431` — SOUL.md seed, default personality, personalities dict
- `hermes_cli/default_soul.py:1-11` — default SOUL.md content
- `hermes_cli/kanban_db.py:1-69,93,755-862` — kanban storage docstring + schema
- `hermes_cli/profiles.py:1-22` — profiles module docstring
- `plugins/kanban/dashboard/plugin_api.py` — 30+ REST endpoints + WS at /events
- `tools/kanban_tools.py` (1139 lines) — agent-callable kanban
- `tools/environments/base.py:85-87` — TERMINAL_SANDBOX_DIR
- `website/docs/user-guide/features/kanban-worker-lanes.md` — worker lane contract
- `Dockerfile`, `docker-compose.yml` — container deployment

**OpenClaw** (upstream clone, gitignored, sibling to repo):
- `src/gateway/protocol/version.ts:1-3` — `PROTOCOL_VERSION = 4`
- `src/gateway/protocol/schema/frames.ts:1-176` — req/res/event frame discriminated union, ConnectParams ed25519 device payload
- `src/gateway/server/ws-connection.ts:3,123,198,226` — WebSocketServer setup
- `src/gateway/server-methods/*.ts` (71 files) — RPC method handlers
- `src/config/zod-schema.ts:663,684,1077,1078,1079,1115` — top-level config keys (agents, cron, memory, mcp, skills, plugins)
- `src/config/zod-schema.agents.ts:7-13` — AgentsSchema (defaults + list)
- `src/config/zod-schema.agent-defaults.ts:51-297` — full per-agent defaults schema
- `src/config/zod-schema.agent-runtime.ts:660-732,933-1006` — CommonToolPolicyFields, AgentToolsSchema, AgentEntrySchema

**Paperclip** (`paperclip/`):
- `packages/adapters/openclaw-gateway/src/index.ts:1-55` — adapter contract + agentConfigurationDoc
- `packages/adapters/openclaw-gateway/src/server/execute.ts:1048-1496` — execute() implementation
- `packages/adapters/openclaw-gateway/src/server/execute.ts:1071-1077` — URL protocol guard
- `server/src/adapters/builtin-adapter-types.ts:4-16` — 11 built-in adapter types
- `server/src/adapters/registry.ts:99-145,288-377,430-453` — hermes_local registration + external plugin loader

**Isol8 backend** (`apps/backend/`):
- `core/gateway/connection_pool.py` (1761 lines) — `GATEWAY_PORT = 18789`, ws_connect, OpenClaw WS-RPC pool
- `core/services/paperclip_adapter_config.py:34-77` — `synthesize_openclaw_adapter`
- `core/services/friday_identity.py` (~700 lines) — IDENTITY/SOUL/USER/BOOTSTRAP templates + writer
- `core/containers/workspace.py:686-795` — `pre_stage_auth_profile_store` (ChatGPT OAuth)
- `core/services/agent_export.py:88` — `_OPENCLAW_RUNTIME_STATE_DIRS`
- `core/services/catalog_service.py:97-211` — `deploy_from_artifact`
- `core/services/catalog_slice.py:220-298` — `extract_agent_slice`
- `apps/infra/lib/stacks/api-stack.ts:120,253,281-293,432,455,559` — WS API Gateway + VPC Link V1 + ws-connections DDB + Lambda authorizer + WebSocketApi
- `routers/teams/issues.py:20-162` — Paperclip issue surface

### Files counted

- 57 files in `apps/backend/core` + `apps/backend/routers` mention OpenClaw (grep). Most are brand-references; the behaviorally coupled subset is ~15 files (§8.1).
- 71 OpenClaw server-method modules (one per RPC namespace)
- 21 Hermes messaging platform adapters in `gateway/platforms/` (plus 5 plugin-installed)
- 79 Hermes built-in agent tools in `tools/`
- 8 Hermes memory backend providers
- 8 Hermes code-execution sandbox backends (Docker, SSH, Daytona, Modal, Singularity, Vercel Sandbox, local, managed Modal)

