# Pluggable Agent Runtime via ACP — Design

**Status:** Verified design (fork source audited at tag `v2026.6.1-isol8.1`); ready for implementation planning
**Date:** 2026-06-11 (rev 2, 2026-06-12 — verified against `Isol8AI/agent` source)
**Decision recap:** Per-agent runtime · generalize to *any* ACP agent · credentials resolved per provider · spec before code.

---

## 1. Goal

Let an Isol8 user choose, **per agent**, which agent runtime ("harness") executes that
agent's turns:

- `openclaw` — the native embedded loop we run today (default; unchanged).
- **ACP harnesses** via the bundled `acpx` backend: `claude` (Claude Code), `codex`,
  `gemini`, `copilot`, `cursor`, `droid`, `opencode`, `qwen`, … and any custom adapter
  ("Hermes" etc.) added by **config alone**.

Nothing above the container changes: gateway protocol, Paperclip seed agents, marketplace
artifact format, billing, and the frontend all keep seeing an ordinary OpenClaw agent.

This is "Path B" from the harness-pluggability discussion. Path A (swap the container image
per harness) is explicitly **not** pursued — it forks the marketplace and re-implements the
wire protocol per harness.

### 1.1 Driving use case — Jarvis, the marketplace SWE agent

The concrete reason we're building this: **Jarvis is a software-engineering agent on the
prod container, to be published on the marketplace. Jarvis should be able to spawn Claude
Code / Codex / Gemini to do coding tasks.** Everything below is shaped by that.

**Critical distinction — Jarvis uses delegation, NOT runtime-replacement.** There are two
different ACP mechanisms and Jarvis wants the first:

| Mechanism | What it does | Who it's for |
|---|---|---|
| **Delegation** — `sessions_spawn({runtime:"acp", agentId:"claude"})` | A **native** agent spawns a coding harness as a tracked sub-session for a task, results delivered back. The agent stays itself. | **Jarvis.** He keeps his native OpenClaw identity, memory, channels, and orchestration, and *rents* Claude Code/Codex/Gemini per coding task. |
| **Runtime-replacement** — `agents.list[].runtime.type="acp"` | The agent **is** the harness — every one of its turns runs inside Claude Code instead of the native loop. | An agent that should *be* Claude Code end-to-end. NOT Jarvis — this would erase his orchestrator identity. |

Jarvis is a **native** agent (`runtime` absent/embedded). He gains the spawn-a-harness
capability automatically: `isAcpRuntimeSpawnAvailable({config})`
(`src/acp/runtime/availability.ts`) is true when `acp.enabled` and the acpx backend is
healthy, and when true the subagent system-prompt builder sets `acpEnabled: true`
(`src/agents/subagent-spawn.ts:1406`), which teaches Jarvis he can dispatch
`sessions_spawn({runtime:"acp", …})`. **So enabling ACP in Jarvis's container is the entire
backend ask** — no per-agent runtime field is needed for the primary use case. (The
runtime-replacement field from §2.1 remains available for a future "be Claude Code" agent,
but it is secondary.)

This matches OpenClaw's canonical pattern exactly: the platform agent orchestrates
(identity, channels, memory, approvals, delivery) and delegates the actual coding to a
best-in-class coding harness. The fork docs' own example trigger is literally *"Run this as
a one-shot Claude Code ACP session and summarize the result."*

## 2. Verified: how the fork already does all of this

All claims below were verified by reading the fork source at the **pinned shipped tag**
`v2026.6.1-isol8.1` (the same code our extended image is built FROM). This entire design is
**zero fork patches** — Isol8-side config + image baking only.

### 2.1 The per-agent runtime field exists upstream

`src/config/types.agents.ts:16-34` — each `agents.list[]` entry takes:

```jsonc
{
  "id": "agent_…",
  "runtime": {                 // optional; absent == native embedded loop
    "type": "acp",
    "acp": {
      "agent": "claude",       // harness id (acpx alias)
      "backend": "acpx",
      "mode": "persistent",    // or "oneshot"
      "cwd": "/path"           // optional; defaults to the agent's workspace
    }
  }
}
```

Override precedence for bound ACP sessions (per `docs/tools/acp-agents.md`):
`bindings[].acp.*` → `agents.list[].runtime.acp.*` → global `acp.*` defaults.
Dispatch is policy-checked against `acp.allowedAgents` and this mapping.

**Spec rev note:** rev 1 proposed inventing a `runtime: "acp:claude-code"` string field.
Dropped — we adopt the upstream object shape verbatim.

### 2.2 The acpx backend is bundled, with Claude Code built in

`extensions/acpx/` in the fork (`@openclaw/acpx 2026.6.1`) is the official ACP runtime
plugin. Its `package.json` **vendors the adapters as dependencies**:

- `@agentclientprotocol/claude-agent-acp@0.39.0` — **Claude Code ACP adapter** (with usage
  accounting: input/output/cache tokens accumulated per session — see
  `claude-agent-acp-completion.test.ts`)
- `@zed-industries/codex-acp@0.15.0` — Codex adapter
- `acpx@0.10.0` — the process manager (spawn, leases, reaping)

Built-in harness aliases (per `docs/tools/acp-agents-setup.md`): `claude`, `codex`,
`copilot`, `cursor`, `droid`, `gemini`, `iflow`, `kilocode`, `kimi`, `kiro`, `openclaw`,
`opencode`, `qwen`.

**Custom harnesses ("Hermes") are config-only** — `plugins.entries.acpx.config.agents`
(`src/config-schema.ts`): `agents: { "<id>": { command, args? } }`, merged as overrides
into the built-in registry (`service.ts:88`). *Rev-1 Unknown #1 is resolved: config-only,
no fork patch.*

### 2.3 How a turn enters ACP (three paths)

The reply pipeline hands every turn to the acpx plugin's `reply-dispatch` hook
(`tryDispatchAcpReplyHook` → `tryDispatchAcpReply` → `AcpSessionManager.resolveSession`).
A session dispatches to ACP when it has ACP session meta, created by:

1. **Conversation binding** — `/acp spawn claude --bind here` (or config
   `bindings: [{type:"acp", agentId, match:{channel,…}}]`). All subsequent messages in the
   bound conversation route to the ACP session; OpenClaw auto-ensures the session exists.
   **Verified for our exact entry point**: the fork's live test
   `gateway-acp-bind.live.test.ts` drives gateway `chat.send` (same RPC our
   `connection_pool.py` proxies) → bind → follow-up turns run in the harness → replies
   stream back as normal gateway events. Spawned key shape: `agent:<id>:acp:<uuid>`.
2. **`sessions_spawn({runtime:"acp", agentId:"claude", task})`** — a tool call available
   to native agents. **This is the "Friday delegates to Claude Code" path** — a native
   orchestrator spawns a Claude Code session as a tracked background task with delivery
   back to the requesting conversation. First-class, no platform work needed beyond
   enabling ACP.
3. **Per-agent runtime defaults** (§2.1) — `agents.list[].runtime.type="acp"` makes
   OpenClaw resolve that config agent to its harness for spawn/bound flows
   (`src/agents/acp-spawn.ts:460`).

The Isol8 backend already consumes the output: `stream:"acp"` events →
`agent_activity` (`connection_pool.py:758`, tested at `test_chat_event_transform.py:199`).

### 2.4 Permissions (containers are non-interactive)

acpx plugin config (`src/config-schema.ts`):

- `permissionMode`: `approve-all` | `approve-reads` (default) | `deny-all`
- `nonInteractivePermissions`: `fail` (default) | `deny`

ACP sessions have no TTY. **With the defaults, any write/exec prompt aborts the session.**
For Isol8 v1: `permissionMode: "approve-all"` + `nonInteractivePermissions: "deny"` —
the container is the user's own isolated sandbox, equivalent trust to the native loop's
`allowCommands`. A later phase can wire `src/acp/permission-relay.ts` into Isol8's existing
`ApprovalCard` flow for interactive approval parity.

### 2.5 Tool bridges (off by default — keep them off in v1)

`pluginToolsMcpBridge` / `openClawToolsMcpBridge` expose OpenClaw plugin tools and core
tools to the harness via injected MCP servers. Default off; leave off for v1 (smaller
blast radius), revisit when an ACP agent needs memory/cron.

### 2.6 Credentials — "does it work with Sign in with ChatGPT?"

Per the fork docs: *"the harness owns its provider login… Vendor auth still has to exist
on the host for that harness."* The spawned adapter inherits gateway env
(`src/acp/client.ts` → `resolveAcpClientSpawnEnv`, with provider-key strip rules).

- **"Sign in with ChatGPT" is OpenAI-only → it drives `codex`, not `claude`.** The fork
  even ships a dedicated `codex-auth-bridge.ts` (759 lines): Codex ACP runs with an
  isolated `CODEX_HOME`; trusted-project + model-routing config are copied from the host
  while **auth stays on host config** — i.e. the ChatGPT-OAuth credentials Isol8 already
  provisions for the native `openai-codex` provider serve the Codex ACP harness too.
- **Claude Code** (`@agentclientprotocol/claude-agent-acp`) authenticates via Anthropic:
  `ANTHROPIC_API_KEY` env (BYO-key path) or `CLAUDE_CODE_USE_BEDROCK=1` + AWS creds
  (Bedrock-managed path — the ECS task role supplies creds via the container metadata
  endpoint automatically; zero new secrets).
- **Gemini CLI**: `GEMINI_API_KEY` or Google OAuth.

| Isol8 provider path (`billing_accounts.provider_choice`) | Unlocks harness | Credential delivery |
|---|---|---|
| Bedrock managed | `claude` | `CLAUDE_CODE_USE_BEDROCK=1` + task-role IAM (no secret) |
| ChatGPT OAuth (personal + org via connector) | `codex` | existing OAuth pre-staged to EFS; codex-auth-bridge reads host config |
| BYO Anthropic key | `claude` | `ANTHROPIC_API_KEY` (Fernet-decrypted → per-user task env) |
| BYO OpenAI key | `codex` | `OPENAI_API_KEY` |
| (new, fast-follow) Sign in with Claude | `claude` | Pro/Max OAuth in `~/.claude` — headless feasibility unverified |

Extend `provision_gate.py` so "agent wants harness X, owner's provider can't auth it"
surfaces the structured blocked state instead of a runtime spawn error.

---

## 3. Isol8-side work (all of it)

### 3.0 Enablement model — setting-driven, credential-gated

**Setting-based, not config-based.** The source of truth for "which harnesses are enabled"
is a **per-owner setting** stored alongside `provider_choice` (the `billing_accounts` /
owner record), and `config.py` *projects* it into `openclaw.json` (`acp.allowedAgents` +
`plugins.entries.acpx`). It must not live only in the container config, for the same reason
nothing else does: `config_reconciler` / `config_patcher` can rewrite `openclaw.json`, and a
reprovision regenerates it from scratch — a config-only flag would silently drop on the next
Track-1 patch or reprovision. Settings survive; config is derived.

**Availability is gated by credentials, and that gate IS the matrix you described.** A
harness can only authenticate if the owner has that harness's model-provider credentials, so
harness availability maps 1:1 onto `provider_choice`:

| Owner's `provider_choice` | Harness it can enable | Default | Why |
|---|---|---|---|
| **ChatGPT OAuth** ("signed in", personal **and** org) | **Codex** | on | Codex auths via the OAuth tokens we already pre-stage onto EFS (codex-auth-bridge reads host auth); no Anthropic/Google creds present |
| **Bedrock managed** | **Claude Code** | on | `CLAUDE_CODE_USE_BEDROCK=1` + task-role IAM; no OpenAI/Google creds present |
| **BYO key** | **whichever keys exist** → Codex (OpenAI key), Claude Code (Anthropic key), Gemini (Google key); **both/all if multiple keys** | the auth-available set | Harness auth = the BYO key for that provider |

So: signed-in → Codex, Bedrock → Claude, BYOK → choice/both. The UI offers **only** the
harnesses the owner can authenticate; `provision_gate.py` enforces the same server-side so a
listing/agent can't request an unauthable harness.

**ChatGPT OAuth is available to orgs as well as personal owners** (PR #864, merged
2026-06-12 — the prior personal-only guard is gone). Orgs use an **org connector**: an org
admin completes the device-code flow once, the tokens are bound to the org workspace and
pre-staged onto the owner's EFS. That EFS auth is exactly what the Codex ACP adapter's
`codex-auth-bridge` reads, so **Codex works for an org identically to a personal owner** —
no BYO OpenAI key required. (Binding a ChatGPT account to a shared org container stays an
admin-only action, per the gate's `action_admin_only=owner_id.startswith("org_")`.)

**Shape of the setting.** A small allowlist on the owner record, e.g.
`enabled_harnesses: ["claude"]`, **defaulted** from `provider_choice` (zero-config: you get
the harness you can auth) and optionally **narrowable** by the user (e.g. a BYOK owner with
both keys who wants Jarvis to use only Codex). `config.py` renders `allowedAgents` =
`enabled_harnesses ∩ credential-available`. This is the only new stored state; everything
else is projection.

### 3.1 Image (`apps/infra/openclaw/Dockerfile`)

- `@anthropic-ai/claude-code` and `@google/gemini-cli` are **already baked** as global
  bins (Layer 8).
- **acpx IS pruned from the base image — confirmed, the bake is required.** Verified against
  the fork's root `package.json` `files`: `!dist/extensions/acpx/**` lists acpx in the
  excluded/opt-in set (same bucket as `slack`, `discord`, `amazon-bedrock`, `codex`), and
  `scripts/prune-docker-plugin-dist.mjs` strips `dist/extensions/<id>` for any excluded
  plugin not in `OPENCLAW_EXTENSIONS`. The fork's `docker-release.yml` passes no
  `OPENCLAW_EXTENSIONS`, so `ghcr.io/isol8ai/agent:2026.6.1-isol8.1` ships **without**
  `dist/extensions/acpx`.
- **Done (this branch):** added Layer 12 to the extended Dockerfile that bakes
  `@openclaw/acpx@2026.6.1` root-owned into `/app/dist/extensions/acpx` with co-located
  runtime deps (`claude-agent-acp`, `codex-acp`, the `acpx` platform binary), mirroring the
  Layer-10/11 slack/discord/bedrock pattern. No `--ignore-scripts` (acpx's postinstall
  fetches a platform binary; CI builds per-arch). Inert until `openclaw.json` enables it.

### 3.2 Config generation (`apps/backend/core/containers/config.py`)

Emit, when any agent (or the owner's entitlement) enables ACP:

```python
"acp": {
    "enabled": True,
    "backend": "acpx",
    "allowedAgents": [...],          # gated by owner's provider/entitlement
    "runtime": {"ttlMinutes": ...},
},
"plugins": {"entries": {
    "acpx": {"enabled": True, "config": {
        "permissionMode": "approve-all",
        "nonInteractivePermissions": "deny",
        # "agents": {...}  # custom harness overrides (Hermes etc.)
    }},
    ...existing entries...
}},
```

plus the per-agent `runtime` object (§2.1) stamped onto `agents.list[]` entries, and
`CLAUDE_CODE_USE_BEDROCK` / `ANTHROPIC_API_KEY` on the per-user task definition
(`ecs_manager._build_register_kwargs_from_base`) according to provider choice.

### 3.3 Backend API + frontend

- Agents RPC/router: accept + validate `runtime` (must be absent or
  `{type:"acp", acp:{agent ∈ allowedAgents}}`).
- `provision_gate.py`: provider↔harness compatibility check (§2.6).
- Frontend: runtime selector in `AgentCreateForm`/`AgentDialogs` (sibling of
  `ModelSelector`, options gated by provider), display on `AgentDetailPanel`,
  `ProviderIcons` marks.

### 3.4 Marketplace — publishing Jarvis (the real test)

Jarvis is the first listing whose *capability* depends on container-level config, not just
his agent slice. Two things the deploy path must get right:

1. **The delegation capability is container-level, not agent-level.** Jarvis's ability to
   spawn coding harnesses comes from `acp.enabled` + `plugins.entries.acpx.enabled` in the
   buyer's `openclaw.json`, not from a field on his agent entry. So
   `catalog_service.deploy_from_artifact`'s slice deep-merge **must turn ACP on in the
   buyer's container** when deploying a listing that needs it. Add an `acp`/`acpx` block to
   Jarvis's `openclaw-slice.json` (already a deep-merged surface) and flag the listing as
   requiring ACP so deploy enables it. Every container's image already carries the adapters
   (§3.1), so there is no per-buyer image work — only this config merge.

2. **Which harnesses Jarvis can actually spawn depends on the buyer's provider.** A
   Bedrock-managed buyer → Claude Code works out of the box (`CLAUDE_CODE_USE_BEDROCK=1` +
   their task-role IAM). A ChatGPT-OAuth buyer → Codex works, Claude Code needs a key. So
   `allowedAgents` for a deployed Jarvis should be **computed from the buyer's
   `provider_choice`**, and Jarvis's prompt/tooling should degrade gracefully when a
   harness isn't auth-available (provision-gate matrix, §2.6). The listing page can state
   "best with Bedrock or a BYO Anthropic key."

Otherwise the marketplace plumbing is unchanged: `runtime` (for any future
runtime-replacement agent) rides in the slice as a plain field; export/deploy already
handle it. Friday remains unpublishable; Jarvis is a separate, publishable agent.

### 3.5 Paperclip seed agents

Friday/Atlas stay native. Friday additionally gains delegation for free: `sessions_spawn`
with `runtime:"acp"` is available to native agents once ACP is enabled (§2.3 path 2).
No change to `paperclip_provisioning.provision_org`.

---

## 4. Rollout phases

1. **Phase 0 — container spike (no user-facing change).** On a dev container with acpx
   baked + `acp.enabled` + `permissionMode=approve-all` + `CLAUDE_CODE_USE_BEDROCK=1`:
   drive `chat.send` → `/acp spawn claude --bind here` → follow-up turn, mirroring
   `gateway-acp-bind.live.test.ts`. Confirms: image bake, Bedrock auth headless, event
   rendering in our UI, and token/metering visibility.
2. **Phase 1 — Claude Code via Bedrock, per-agent.** §3.1–3.3 wired; Bedrock-managed
   owners only.
3. **Phase 2 — BYO Anthropic key + Codex via existing ChatGPT OAuth** (codex-auth-bridge).
4. **Phase 3 — Gemini, custom harness config surface ("Hermes"), marketplace runtime UI,
   optional permission-relay → ApprovalCard.**

Each phase independently shippable and reversible (absent `runtime` ⇒ native).

## 5. Testing

- Backend unit: `config.py` emits `acp` + `plugins.entries.acpx` iff enabled; agent entry
  `runtime` validation; provision-gate provider↔harness matrix.
- Container integration (gated live): the Phase-0 flow as a repeatable check.
- Marketplace: export→deploy round-trip preserves `runtime`.
- Metering: ACP turns report token usage (`claude-agent-acp` accumulates usage per
  session; verify it surfaces through `sessions.list` for `record_usage`).

## 6. Remaining open questions

1. **Metering parity** — does ACP session usage surface through `sessions.list` the same
   way native runs do? (Phase-0 item; potential GA blocker for Bedrock metering.)
2. Main-session UX: do we bind each ACP agent's default conversation at provision time
   (config `bindings[]`) or lazily on first message? (Phase-1 design detail.)
3. Hot-switching `runtime` on an existing agent — allowed (tears down live ACP session)
   or create-time only?
4. "Sign in with Claude" subscription OAuth headless in a container — spike or skip.
