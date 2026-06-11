# Pluggable Agent Runtime via ACP — Design

**Status:** Draft / spec-first (no code yet)
**Date:** 2026-06-11
**Author:** harness-model-selection working branch
**Decision recap:** Per-agent runtime · generalize to *any* ACP agent · Bedrock/BYO/OAuth credentials resolved per provider · ship spec before code.

---

## 1. Goal

Let an Isol8 user choose, **per agent**, which agent runtime ("harness") executes that
agent's turns:

- `openclaw` — the native OpenClaw loop we run today (default; unchanged).
- `acp:claude-code` — Anthropic's Claude Code, driven over ACP.
- `acp:codex` — OpenAI's Codex, driven over ACP.
- `acp:gemini` — Google's Gemini CLI, driven over ACP.
- `acp:<id>` — any future ACP agent ("Hermes" etc.) with **zero new platform plumbing**.

The whole point of going the ACP route (rather than swapping the container image) is that
**nothing above the container changes**: the gateway protocol, Paperclip seed-agent wiring,
the marketplace artifact format, billing, and the frontend all keep seeing an ordinary
OpenClaw agent. "Which harness" collapses to a single config field on the agent.

This is "Path B" from the harness-pluggability discussion. Path A (swap the whole image per
harness) is explicitly **not** pursued — it forks the marketplace and re-implements the wire
protocol per harness.

---

## 2. Key finding — OpenClaw is already an ACP host

This design is cheap because the OpenClaw fork (`Isol8AI/agent`) already ships a mature ACP
host. Verified in the fork source (`src/acp/**`, `src/agents/acp-spawn.ts`,
`src/auto-reply/reply/commands-acp/**`):

| Capability | Evidence in fork |
|---|---|
| ACP control plane that spawns + drives a sub-agent | `src/acp/control-plane/manager.js` (`getAcpSessionManager`), `src/agents/acp-spawn.ts` (`spawnAcpDirect`) |
| Pluggable backend registry, selected by id | `src/acp/runtime/registry.ts` — `registerAcpRuntimeBackend({id, runtime, healthy})`; default backend `acpx` |
| Per-session runtime options (model / mode / cwd / permission profile / timeout) | `src/acp/control-plane/runtime-options.js`, surfaced via `/acp` commands |
| Spawns a real Zed ACP adapter today | `src/gateway/gateway-acp-spawn-defaults.live.test.ts` launches `@zed-industries/codex-acp/bin/codex-acp.js` with `agent: "codex"` |
| Injects provider credentials into the spawned agent via env | `src/acp/client.ts` → `resolveAcpClientSpawnEnv` passes `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / … (with strip rules) |
| Config schema for ACP | `src/config/types.acp.ts` — `AcpConfig { enabled, backend, fallbacks, defaultAgent, allowedAgents, maxConcurrentSessions, stream, runtime }` |

And the **Isol8 backend already consumes ACP output**: `connection_pool.py:758` folds
`stream: "acp"` agent events into the `agent_activity` envelope, tested at
`tests/unit/core/test_chat_event_transform.py:199`. So the inbound rendering path is done.

**Net:** Claude Code's adapter (`@zed-industries/claude-code-acp`) is the sibling of the
`codex-acp` package the fork already spawns. Adding it is the same mechanism, a different
command + credential source.

### Unknowns to confirm before/early in implementation

1. **`acpx` agent→command map.** The `acpx` backend is a bundled plugin (not in the core
   sparse checkout). We must confirm where it maps an agent id (`claude-code`, `codex`,
   `gemini`) to a spawn command/args, and whether new agents are added via config or require
   a plugin change. *This is the single load-bearing implementation unknown.*
2. **`claude-code-acp` headless auth.** Confirm the adapter package name/version and that it
   runs non-interactively in our Debian container with each of: `ANTHROPIC_API_KEY`,
   `CLAUDE_CODE_USE_BEDROCK=1` + IAM, and "Sign in with Claude" OAuth.
3. **Per-agent model selection** semantics inside each adapter (Claude Code model flag vs our
   `ModelSelector`).

---

## 3. The `runtime` field (per-agent)

A new optional field on the **agent entry** (the unit the marketplace already snapshots),
not on the owner/container row — so it is marketplace-portable and lets one container mix a
native Friday with an `acp:claude-code` worker.

```jsonc
// agents.list[] entry in openclaw.json (and in the marketplace openclaw-slice)
{
  "id": "agent_…",
  "workspace": ".openclaw/workspaces/agent_…",
  "runtime": "openclaw",          // NEW. default. omitted == "openclaw"
  // or "acp:claude-code" | "acp:codex" | "acp:gemini" | "acp:<id>"
}
```

- **Default / back-compat:** absent ⇒ `openclaw`. Every existing agent keeps working with no
  migration.
- **Storage:** lives in the agent entry on EFS `openclaw.json`, written by `config.py`. The
  per-agent choice is set through the existing agents RPC (`agents.create` / `agents.update`)
  and rendered into config. No new DDB table.
- **Validation:** `runtime` must be `openclaw` or `acp:<id>` where `<id> ∈ allowedAgents`
  (see §5). Unknown ⇒ reject at the RPC boundary, fall back to `openclaw`.

### Where it routes

When `runtime == "acp:<id>"`, the agent entry must mark the agent so OpenClaw dispatches its
turns through the ACP control plane instead of the native loop, and `config.acp.defaultAgent`
(or a per-agent ACP binding) resolves to `<id>`. Exact mechanism depends on Unknown #1 — the
spec assumes one of: (a) a per-agent ACP binding the fork already supports
(`src/acp/persistent-bindings.*`), or (b) a small fork patch to read `runtime` off the agent
entry and route accordingly. Confirm before coding §4.

---

## 4. Config generation (`apps/backend/core/containers/config.py`)

Today `build_openclaw_config_dict()` emits **no** `acp` block. Changes:

1. **Emit a top-level `acp` block** when any agent in the container uses an `acp:*` runtime:
   ```python
   "acp": {
       "enabled": True,
       "backend": "acpx",
       "defaultAgent": <derived>,            # if exactly one acp agent
       "allowedAgents": ["claude-code", "codex", "gemini"],  # gated by entitlement
       "runtime": {"ttlMinutes": <idle ttl>},
       "stream": {"deliveryMode": "live"},
   }
   ```
   When no agent uses ACP, omit the block entirely (no behavior change for existing users).

2. **Stamp `runtime` onto each agent entry** in `agents.list[]` from the per-agent choice.

3. **Provider/credential env** — `build_provider_block()` already wires the native provider;
   for ACP agents the credentials are injected as **env vars on the container task**
   (see §6), which `src/acp/client.ts` forwards into the spawned adapter. `config.py` records
   *which* provider each ACP agent uses so the task-def env can be assembled.

No change to the gateway/`amazon-bedrock`/`openai-codex` native provider blocks — native
agents are untouched.

---

## 5. Credentials & providers — answering "does it work with Sign in with ChatGPT?"

**Short answer:** "Sign in with ChatGPT" is **OpenAI-only**. It authenticates the **Codex**
ACP agent, *not* Claude Code. Claude Code uses Anthropic auth — API key, Bedrock, or its own
"Sign in with Claude" (Pro/Max) OAuth. Because we generalized to any ACP agent, credentials
are resolved **per agent → per provider**, and they map almost 1:1 onto Isol8's three
existing provider paths (`billing_accounts.provider_choice`, enforced by
`core/services/provision_gate.py`).

| Isol8 provider path (today) | Native OpenClaw uses | Unlocks ACP agent | Credential delivered to adapter |
|---|---|---|---|
| **Bedrock managed** | `amazon-bedrock/claude-*` | `acp:claude-code` | `CLAUDE_CODE_USE_BEDROCK=1` + container IAM role (no secret) |
| **ChatGPT OAuth** (`oauth_service.py`, personal owners only) | `openai-codex/gpt-5.5` | `acp:codex` | Existing ChatGPT OAuth tokens → Codex `auth.json` / `OPENAI_API_KEY`-equiv |
| **BYO key — Anthropic** (`key_service.py`, Fernet) | `anthropic/claude-*` | `acp:claude-code` | `ANTHROPIC_API_KEY` env |
| **BYO key — OpenAI** | `openai/gpt-*` | `acp:codex` | `OPENAI_API_KEY` env |
| **(new) Sign in with Claude** | — | `acp:claude-code` | Claude Pro/Max OAuth creds in `~/.claude` (headless — Unknown #2) |
| **(new) Gemini** | — | `acp:gemini` | Google OAuth or `GEMINI_API_KEY` |

Implications:
- **Bedrock-managed owners get Claude Code "for free"** — `CLAUDE_CODE_USE_BEDROCK=1` reuses
  the IAM path and flows through existing Bedrock-credit metering. This is the lowest-friction
  default and the recommended first credential path to wire.
- **ChatGPT-OAuth owners get Codex**, reusing the OAuth we already store. They **cannot** run
  Claude Code on that credential.
- **"Sign in with Claude"** (subscription OAuth) is a *new* provider path; it's attractive but
  headless-in-container feasibility is unverified (Unknown #2) — treat as a fast-follow, not
  the first cut.
- **Provision gate** (`provision_gate.py`) already blocks chat on missing OAuth / exhausted
  credits; extend it so "agent X needs provider Y, owner has Z" surfaces the same structured
  blocked state rather than a runtime spawn failure.

---

## 6. Image (`apps/infra/openclaw/Dockerfile`)

Bake the ACP adapters into the extended image (same place Slack/Discord/Bedrock plugins live),
so **every** container can run any runtime and the marketplace stays single-pool:

- `@zed-industries/claude-code-acp` (+ the Claude Code CLI it wraps)
- `@zed-industries/codex-acp` (+ Codex CLI) — already exercised by the fork's live test
- Gemini CLI ACP adapter (phase 2)
- Confirm the `acpx` backend plugin is present/enabled (it's the spawner).

Pin versions in the image; bump via the normal `build-openclaw-image.yml` → ECR
`isol8/openclaw-extended` flow and the per-env tags in `openclaw-version.json`. No per-user
image variance.

---

## 7. Gateway / backend impact — minimal

- **Inbound events:** already handled — `stream:"acp"` → `agent_activity`
  (`connection_pool.py:758`). May want to enrich the pill (currently one generic "ACP step"
  envelope) once we see real Claude Code session updates, but nothing is *required*.
- **RPC surface:** unchanged. `chat.send`, `sessions.list`, `agents.create/update/list` work
  identically — the ACP agent is invisible above the container.
- **Usage/metering:** token accounting via `sessions.list` should still report for ACP runs;
  **verify** the ACP path populates session token counts (Bedrock metering especially — see
  `2026-06-07-bedrock-metering-enforcement-metronome-design.md`). If ACP turns don't surface
  tokens the same way, that's a metering gap to close before GA.

---

## 8. Marketplace impact — stays one marketplace

- **Export** (`agent_export.py`) keeps `format="openclaw"`; `runtime` rides along as a plain
  field inside `openclaw-slice.json`'s agent entry. No new format, no exporter fork.
- **Deploy** (`catalog_service.deploy_from_artifact`) is unchanged: it deep-merges the slice
  (carrying `runtime`) and the buyer's image already has every adapter baked in (§6), so any
  buyer can run any listing's runtime.
- **Entitlement/compat:** because all adapters are in the shared image, there is **no**
  buyer/seller compatibility gating to build. The only gate is credential availability at run
  time (§5), which the provision gate already models.
- **Safety:** `marketplace_safety.scan_tarball` is unaffected (still scans the workspace).
  Note for review: an `acp:*` agent's behavior is defined by the adapter, not by workspace
  files — the safety story is the same as native (we trust the baked adapters; we scan the
  seller's workspace/skills/MCPs as today).
- **Friday remains unpublishable** (`FRIDAY_PUBLISH_ERROR_MSG`) regardless of runtime.

---

## 9. Paperclip seed agents

Friday/Atlas stay `runtime: openclaw` (native) by default — they are the orchestrator/recruiter
and rely on OpenClaw-native behavior. The per-agent field means a user can *add* an
`acp:claude-code` worker agent alongside them without touching Friday. No change to
`paperclip_provisioning.provision_org` or the `openclaw_gateway` adapter wiring.

---

## 10. Frontend

- Add a **runtime selector** to agent create/edit (`AgentCreateForm.tsx` /
  `AgentDialogs.tsx`), sibling to the existing `ModelSelector`. Options gated by the owner's
  provider/entitlement (don't offer `acp:claude-code` to a ChatGPT-OAuth-only owner, etc.).
- Surface the chosen runtime on `AgentDetailPanel` / `AgentOverviewTab`.
- Reuse `ProviderIcons` for brand marks (Claude / OpenAI / Gemini).

---

## 11. Rollout phases

1. **Phase 0 — verify (no user-facing change).** Resolve Unknowns #1–#3 against the fork +
   a scratch container: bake `claude-code-acp`, hand-write an `acp` config + one agent with
   `runtime: acp:claude-code`, drive a turn end-to-end with `CLAUDE_CODE_USE_BEDROCK=1`.
2. **Phase 1 — Claude Code via Bedrock.** Per-agent `runtime` field, `config.py` `acp` block,
   Dockerfile bake, provision-gate extension, frontend selector. Bedrock-managed owners only.
3. **Phase 2 — BYO Anthropic key + Codex via ChatGPT OAuth.** Wire the per-provider credential
   env map; Codex reuses existing OAuth.
4. **Phase 3 — Gemini + "Sign in with Claude" OAuth + marketplace runtime UI** (listings
   display/advertise their runtime).

Each phase is independently shippable and reversible (absent `runtime` ⇒ native).

---

## 12. Testing

- **Backend unit:** `config.py` emits the `acp` block iff an agent uses `acp:*`; agent entry
  carries `runtime`; provision-gate rejects unsupported provider/runtime combos.
- **Backend unit:** extend `test_chat_event_transform.py` if we enrich the `acp` pill.
- **Container integration (live, gated):** mirror the fork's
  `gateway-acp-spawn-defaults.live.test.ts` pattern — spawn `claude-code-acp`, assert a turn
  streams back through the gateway as chunks + `agent_activity`.
- **Marketplace:** export→deploy round-trip preserves `runtime` in the slice; deployed agent
  runs on a fresh container.
- **Metering:** assert ACP runs report token usage (§7).

---

## 13. Open questions for review

1. **Unknown #1** (acpx agent→command map) — config-only or fork patch? Gates §3/§4.
2. Should `runtime` be settable on an existing agent (hot switch) or only at create time?
   Hot-switch means tearing down a live ACP session on change.
3. Per-agent **model** within a runtime — do we expose Claude Code's model picker, or pin to
   the owner's Bedrock model set?
4. **Metering parity** for ACP turns (Bedrock especially) — blocker for GA or fast-follow?
5. "Sign in with Claude" subscription OAuth in a headless container — worth the spike, or skip
   in favor of Bedrock/BYO?
