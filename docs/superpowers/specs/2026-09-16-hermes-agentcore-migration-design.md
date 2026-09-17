# Hermes on AgentCore — migration design

**Status:** DRAFT for owner review (2026-09-16). Supersedes the scope sections of
`2026-09-16-hermes-on-agentcore-evaluation.md` (§4, §5, §7); that document stays as the
evaluation record. The spike plan `../plans/2026-09-16-hermes-agentcore-spike.md` is
retired by this design: dev is past it.

**One sentence:** every Lightbulb workspace moves from a per-user OpenClaw container on ECS
Fargate + EFS to one Amazon Bedrock AgentCore runtime running Hermes Agent behind a small
Python bridge, keeping every user-visible behaviour we built into OpenClaw and deleting the
OpenClaw fork, the plugin, litestream, the EFS layout and the config writer.

## 1. Where dev stands (measured 2026-09-16)

The bridge (`apps/infra/hermes/bridge.py`, branch `spike/hermes-agentcore`) answers the
existing `lightbulb-channel` ingress, RPC and hook-token contracts inside an AgentCore microVM
for the dev workspace `org_3JCfoyOVF0AjDYjOnIZz5cK8XJL`:

| measured | value |
|---|---|
| human message → CEO reply in `#general`, cold microVM | 15 s |
| same, warm session | 4 s |
| six agents in one process, resident memory | 306 MB (peak 391 MB) |
| profile state on disk after seven turns | 952 KB |
| S3 restore / save per agent profile | 1.3 s / 0.6 s |
| Hermes system prompt + tool schemas per turn | ≈ 12k input tokens |
| backend changes required | none |

Bedrock rides through the bedrock-gate (`AWS_ENDPOINT_URL_BEDROCK_RUNTIME` +
`AWS_BEARER_TOKEN_BEDROCK`; Hermes's Bedrock provider is boto3 `converse_stream`, not the
Anthropic SDK), the gate ledger admits and debits the owner's AIP, and run ledger rows close
`completed` with usage. What is NOT there yet is listed per milestone in §6.

## 2. Decisions (owner rulings, 2026-09-16)

1. Drop the pass/fail spike framing; work directly in dev (CLI first, code later). Shared dev
   resources may be modified; the reference OpenClaw workspace `org_3JCpE2…` stays untouched.
2. v1 scope = **full parity, including the desktop node and the browser panel, before any
   customer workspace moves**. Node matters far more than browser.
3. Node scope = **everything the OpenClaw node exposes** (exec, screen, camera, notify,
   notifications, location, device helpers).
4. **Option 2**: Hermes on AgentCore with the node rebuilt around the bridge. Not
   OpenClaw-on-AgentCore. Later the same evening: the node transport is **lazy attach through
   the backend** (§4.4), not a WebSocket into the microVM.
5. Every behaviour built into OpenClaw for integration or UX (proactive follow-ups and
   threading, delivery ack, local-sessions tool, hire card, RPC surface, config semantics)
   is **kept**; only OpenClaw internals drop. The owner asked that `hermes claw migrate` be
   used as the ground truth for what carries over; the research (§4.8) found it archives most
   of what we need, so the migration job is ours and the tool's mapping table is only a guide.
6. Prod stays on ECS until the cutover; customers migrate once, straight to Hermes.

## 3. Inventory: what we built into OpenClaw, and where it lives on Hermes

| Built behaviour (today) | On Hermes | Milestone |
|---|---|---|
| Room ingress, structured room context, reply threading (`threadRootMessageId ?? messageId`, DMs linear), `message.id` + thread root in context, delivery ack | Bridge (ingress done; threading + context next) | M1 |
| Run supervisor: `run_active` and `room.run.frame` frames to AppSync, heartbeats, started/ended with usage, abort/steer, the cancelled/failed error frames | Bridge (lifecycle done; frames, abort, steer next) | M1 |
| `message` tool (post to a room, thread param), hire-suggestion payload, `lightbulb_local_sessions` tool, `agent_end` usage for non-room runs | One Hermes tool plugin `lightbulb` | M1 |
| RPC surface the control panels call (health, agents, sessions, skills, config, usage) | Bridge | M1 |
| Agent mail (`lightbulb-email:` hook sessions), Slack app | Keep (both enter through the backend); mail gets an `eventKind` on the same inbound door | M1 |
| Paperclip-driven runs (Teams issues, routines, nightly CEO) through the `openclaw_gateway` adapter over the relay | Rebuild: a Paperclip adapter posting to a backend door that invokes the bridge (`eventKind: paperclip_run`) | M1 |
| Trial park/wake ("Agents asleep", wake budget) driven by ECS stop/start | Rebuild as a backend judgement from `last_interactive_at`; gates already run at dispatch | M4 |
| Desktop node: exec, screen, camera, notify, notifications, location, device | Backend terminates the node protocol; one `lightbulb_node` plugin (terminal backend + `nodes` tool) over one hook-token route | M2, M3 |
| Config writer (`write_openclaw_config`): providers/AIPs + costs, model policy, per-agent roster/identity, exec policy + tool deny, MCP servers + OAuth refresh, env.vars, integrations, memory, ACP coding harnesses | Per-profile `config.yaml` + `.env` writer | M4 |
| Bundled skills (lightbulb, paperclip, shopify) + Linux skill binaries | Keep; one pass swaps tool names; binaries bake into the Hermes image | M1, M4 |
| ChatGPT OAuth, BYO keys | Keep the product flows; tokens land in the profile as Hermes provider auth | M4 |
| Browser panel, Shopify login door | AgentCore Browser: Hermes browser tool over CDP, panel embeds the live view | M5 |
| Marketplace export/deploy | S3 profile prefix instead of EFS paths | M6 |
| Fork patches (redaction fix, transcript/tool-result internals, SQLite lifecycle, tree-sitter, workshop, unused multiplayer set) | Drop (OpenClaw internals); one check that Hermes request dumps never hold the gate bearer | M4 |
| litestream, local-sqlite boot script, EFS symlink spine + lock sidecars, chokidar polling, gateway image, the fork | Delete at cutover | M6 |

## 4. Architecture

### 4.1 Runtime shape
One AgentCore runtime per workspace (image `lightbulb/hermes-agent-arm64`, execution role =
the workspace's owner role, VPC mode, lifecycle 4 h idle / 8 h max). One session per
workspace (session id derived from the owner id, as `runtime_transport.session_id_for`
already does). All the workspace's agents are Hermes profiles under
`HERMES_HOME/profiles/<agentId>` in one bridge process; a turn constructs one `AIAgent` under
`set_hermes_home_override(profile)` and runs `run_conversation`. Nothing session-bound runs
before the first invocation, so AgentCore's warm pool (≈10 pre-started instances per runtime
version) constructs nothing.

### 4.2 The bridge
`apps/infra/hermes/bridge.py`, one process on :8080:
- `GET /ping`, `POST /invocations` carrying the `{path, method, headers, body}` envelope the
  backend's `runtime_transport` sends; answers `/plugins/lightbulb-channel/inbound/<agentId>`
  (202 then the turn in a thread), `/plugins/lightbulb-channel/rpc`, `/runs/{id}/{abort|steer}`.
- Hook-token callbacks to the backend: `run.started` / `run.heartbeat` (30 s) / `run.ended`
  with usage, `agent-messages`, `usage`.
- AppSync run frames published with the execution role (SigV4, one POST per frame): lifecycle,
  tool and final-text frames first; per-token `chunk`/`thinking` deltas and batching only once
  Hermes exposes a stream callback and someone asks for streaming text.
- Profile sync: restore from S3 on an agent's first turn per process, save after each turn
  (SQLite through the backup API, WAL/SHM skipped).
- Node commands go out as HTTP calls to the backend (§4.4); the spike's `/ws` echo and the
  diagnostic hatch are deleted before the founder moves.
The contracts it must satisfy are listed in §5.

### 4.3 The `lightbulb` Hermes tool plugin
One plugin under the image's plugin dir, enabled for every profile: `send_room_message(roomId,
text, threadRootMessageId?)` (the `message`-tool equivalent the follow-ups rule needs),
`hire_suggestion(...)` emitting the structured payload the HireCard parses, the local-sessions
tool (`resources | read | send | receipt`, Bearer = the per-turn signed token), and the
non-room usage report, plus the redaction prefix pattern for Paperclip `pcp_` keys. Tools
call the backend over the hook-token routes; payload shapes are in §5.

### 4.4 Node: lazy attach through the backend (owner decision 2026-09-16)
(Frame-level contract, exec surface and risks: research report 02.)

**What the desktop actually runs.** Two node clients per Mac, both dialing the `bv-` relay
with a device token: our Rust client (`node_client.rs`, `clientId openclaw-macos`) with the
six device commands (`screen.snapshot/record`, `camera.*`, `system.notify`,
`notifications.*`, `location.get`, `device.*`), and the **upstream OpenClaw node host**
vendored as a sidecar (`clientId node-host`, `openclaw node run`) which owns the exec
surface: `system.run`, `system.run.prepare`, `system.which`, `system.execApprovals.*`,
`mcp.tools.call.v1`, `fs.listDir`, `terminal.upload`, `browser.proxy*`, `file.*`, `dir.*`.
The upstream client is the stricter contract: server-first `connect.challenge {nonce, ts}`,
the v3 signature string, a `hello-ok` with `protocol: 4`, `server`, `features`,
`snapshot {presence, health, stateVersion, uptimeMs}`, `auth {role, scopes}` and
`policy {maxPayload, maxBufferedBytes, tickIntervalMs}` (never `auth.deviceToken`), a tick
watchdog (close 4000 at twice `tickIntervalMs`), a 15 s post-connect deadline, and
`paramsJSON` as a JSON *string* on `node.invoke.request`. Nothing the desktop declares uses
`node.invoke.progress`, so the protocol is request/response with a timeout.

**Design.**
- The desktop is unchanged: same binaries, same pairing, same relay door.
- The **backend terminates the node protocol.** `gateway_relay.py` already verifies the device
  JWT (`machine_auth.py`) and writes a presence row; for an AgentCore workspace it now answers
  `connect.challenge` / `connect` / `hello-ok`, keeps ticking, holds the socket, and uses the
  paired device's declared `commands` as the allowlist. `relay_hooks.on_node_ready` /
  `on_node_closed` become no-ops for AgentCore workspaces (they do container RPCs and an EFS
  config patch that do not exist on Hermes).
- The **bridge never opens a WebSocket.** The Hermes `lightbulb_node` terminal environment
  provider (the sanctioned plugin slot: `register_terminal_environment_provider`,
  `is_available`, `create_environment` → an object with `execute(command, timeout) ->
  {"output", "returncode"}` and `cleanup()`, `is_remote=True`, `is_container=False`, selected
  per profile via `terminal.backend`, ContextVar-scoped so six agents in one microVM can
  differ) makes one HTTP call per command on the hook-token rail the run callbacks already
  use: `POST /api/v1/internal/node/invoke/{ownerId} {command, paramsJSON, timeoutMs}`. The
  backend forwards it as `node.invoke.request` on the held socket and returns the
  `node.invoke.result` payload. Device commands are a `nodes` tool registered by the same
  `lightbulb_node` plugin over the same route.
- **Cross-instance routing:** the desktop's socket is pinned to the backend task that
  accepted it, and the bridge's call can land on any of the 2–4 tasks. The presence row in
  `ws-connections` (written by the accepting task, refreshed every 60 s) gains the task's
  address, and the internal route forwards to it.
- `strip_env_keys` on the provider strips the gate bearer and the Paperclip key from every
  subprocess the agent spawns (replaces `paperclip-exec-creds`). File sync is not needed for
  exec; `file.*` and `dir.*` results ride the same route.

**What this deletes** versus piping the socket into the microVM: the presign, the
`InvokeAgentRuntimeWithWebSocketStream` IAM action, the 32/64 KB frame cap and chunking
(screenshots and file reads are megabytes), AgentCore's 60-minute forced close of streaming
connections (an hourly fleet-wide reconnect), the cold-start race (a cold session accepts a
socket in 27.8 s, past the node host's 15 s challenge deadline), and the standing memory charge
of a session held awake by ticks.

**Kept as a proven fallback:** the AgentCore WebSocket door works for our VPC-mode runtime
(2026-09-16 23:00Z: a hand-rolled SigV4 query presign, the owner's session id, a cold connect
in 27.8 s, echoes, and an HTTP invocation on the same session landing in the same bridge
process). If streaming node commands ever matter, that is the transport, with the relay
poking `/ping` before the upgrade to dodge the challenge deadline.

### 4.5 Config: the per-profile writer replaces `write_openclaw_config`
(Detail and the block-by-block table: research report 01.)

Facts that shape it (from the Hermes v0.21.3 source):
- **A profile is a complete `$HERMES_HOME`**, not a fragment: its `config.yaml` is merged
  over `DEFAULT_CONFIG` alone, with **no inheritance** from the root home. Every generated
  profile config must therefore be complete, produced from one template plus per-agent
  overrides, exactly as `build_openclaw_config_dict` does today.
- Config is read **once per `AIAgent` construction** and cached on file mtime, so rewriting
  `profiles/<id>/config.yaml` takes effect on the agent's next turn. No watcher, no restart.
- The **managed overlay** (`managed_scope.py`) wins at the leaf after env expansion. Our
  non-negotiables go there so an agent that edits its own config cannot undo them:
  `bedrock.discovery.enabled: false` (default true, would `AccessDenied` on the owner role),
  `telemetry.shared_metrics.*` off, `updates` off, the approvals deny backstop. This closes
  the "chat mutates the backend-owned file" class of bug structurally.
- Per-profile `.env` is the secret store (read through the fail-closed secret scope under
  `gateway.multiplex_profiles`, never unioned into the process env): the gate bearer,
  `AWS_ENDPOINT_URL_BEDROCK_RUNTIME`, region, the agent's Paperclip key, and `env.vars`.
  `config.yaml` carries `${VAR}` references only. This also gives **per-agent attribution**
  that one shared container env could not.
- Hermes's per-home registries (plugin managers, Bedrock clients keyed by home, MCP registry
  scope) are the load-bearing rail for many profiles in one process.

What the writer emits (report 01 §6, in priority order): `model: {default}` with the CEO on
the premium tier and specialists on standard; `.env` as above; every `auxiliary.<task>`
pinned to `provider: bedrock` + explicit model with `fallback_chain: []` (today's auto-chain
probes OpenRouter/Nous; `auxiliary.free_only: true` as the last line); `SOUL.md` per agent
from the starter roster / Friday identity; `mcp_servers` translated from
`integration_mcp_servers`; `toolsets` / `agent.disabled_toolsets` (the `TOOL_DENY_*` port)
with `approvals.mode: off`, `unattended_mode: approve` and an `approvals.deny` backstop;
`memory.*` limits and `skills.*`; `sessions.*` retention, `agent.run_budget_seconds`,
`max_concurrent_sessions`; logging/telemetry/updates in the managed overlay.

What is dropped, not ported: every `_emit_81_shape` gate, `meta.lastTouchedVersion`, the
Chrome recipe, the node-pairing trust store, the lightbulb channel + bindings, the hooks
ingress — about two thirds of the current writer.

**Secrets hygiene.** Hermes writes `sessions/request_dump_*.json` (full request body, header
masked, text redacted) **unconditionally on API errors** with no config switch: the profile
sync excludes that glob and `logs/`; `HERMES_DUMP_REQUESTS` is never set; one deliberate
failing call in dev reads a dump to confirm `redact_sensitive_text` covers the gate bearer
and `pcp_` keys (OpenClaw's defaults missed `pcp_` in August), else a one-line fork patch or
a `pre_llm_call` hook. `strip_env_keys` on the `lightbulb_node` terminal provider strips the
bearer and Paperclip key from every agent-spawned subprocess, replacing `paperclip-exec-creds`.

Write path: the backend writes profile config to the S3 prefix at provision, on hot patches
and on token refresh; the bridge pulls the profile before a turn. No file watchers, no EFS
locks.

### 4.6 Providers
(Detail: research report 06.)
- **Bedrock managed (default):** `provider: bedrock`, `model: {default: <AIP ARN>}` per
  profile with the tier mapping (CEO premium, specialists standard, the `haiku` AIP for the
  cheap/auxiliary tasks where the workspace has it; trials carry the `sonnet` AIP only). The
  gate routing rides two process-wide env vars,
  `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` and `AWS_BEARER_TOKEN_BEDROCK` (read from the raw
  process env by `bedrock_adapter.py`, not through the per-profile secret scope). That is
  correct for our shape, one bridge process per workspace, and is the reason never to
  multiplex two workspaces into one process.
- **ChatGPT OAuth:** Hermes's `openai-codex` provider implements natively what we hand-rolled
  with the EFS sentinel and boot-script re-import: one shared grant, N profiles, the root
  writes the refreshed token back. The backend stages the decrypted token into the root
  home's codex auth file at restore; the bridge syncs the refreshed file back. One caveat:
  Hermes's Profiles docs claim profiles do not inherit the root grant while the source does;
  a hands-on test decides before M4 closes. One account per workspace is unchanged.
- **BYO keys:** `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` in the profile `.env`, scoped per
  profile by Hermes's secret scope; provider `openai` / `anthropic` with the direct endpoints.
- **Integration MCP servers:** `integration_mcp_servers` translate to `mcp_servers` entries
  (stdio with env, or remote with headers); OAuth-backed remote servers keep their token file
  under `$HERMES_HOME/mcp-tokens/` (schema to confirm). MCP discovery and spawn are cached
  per process keyed by profile home, so servers stay warm across turns within a session and
  cold-start once per microVM. Token refresh: the backend rewrites the profile's `.env` /
  token file in S3 and the bridge pulls it before the turn.
- **Memory:** no one-to-one mapping. Hermes's default memory is a curated summary in the
  system prompt plus FTS over sessions, not the per-message vector index `memorySearch` gave
  OpenClaw. Decision for M4: ship Hermes's native memory first (it is what the product's
  "the agent grows with you" behaviour is built on) and evaluate an external memory plugin
  only if recall regressions show up in dev use.

### 4.7 Browser and the Shopify door
(Detail: research report 05.) Today the panel is a real VNC relay (`routers/browser_view.py`,
noVNC in `BrowserDockPanel.tsx`) into a headful Chrome baked into every container; none of
that exists on AgentCore.

- **AgentCore Browser** is a separate microVM per session: 15 min default / 8 h max timeout,
  500 concurrent per tool, `PUBLIC` network only, billed like Runtime. The agent drives it over
  the automation stream (CDP `wss://…/browser-streams/<browser>/sessions/<id>/automation`,
  SigV4 **headers** from `BrowserClient.generate_ws_headers()`); the human sees it through the
  DCV live-view stream, embedded with the first-party `BrowserLiveView` React component from
  the `bedrock-agentcore` npm package and a backend-minted presigned URL. "Take control" =
  `UpdateBrowserStream(automationStream=DISABLED)`, hand back = `ENABLED`. Logins persist only
  through **Browser Profiles** (`CreateBrowserProfile` → explicit `SaveBrowserSessionProfile`
  → start with `profileConfiguration`); nothing is saved automatically.
- **Hermes side:** Hermes's main browser driver shells out to the `agent-browser` CLI, and
  **`agent-browser` ships a first-party AgentCore provider** (`AGENT_BROWSER_PROVIDER=agentcore`
  or `-p agentcore`; SigV4 from `AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN` env; it starts the
  cloud session itself; `AGENTCORE_REGION`, `AGENTCORE_BROWSER_ID`, `AGENTCORE_PROFILE_ID`
  for persistence, `AGENTCORE_SESSION_TIMEOUT`; prints the live-view URL to stderr). Decision:
  drive it through that provider, with the bridge exporting the execution role's rotating
  credentials into the subprocess env per turn and capturing the session id from the
  provider's output so the backend can mint the panel's live-view URL. Hermes's raw
  `browser_cdp` escape hatch (plain `websockets.connect`, no headers) stays disabled.
- **Backend/frontend:** the workspace's browser session lives as four attributes on the
  containers row (`browser_id`, `browser_session_id`, `browser_profile_id`,
  `browser_session_expires_at`); `POST /browser/session` starts or reuses the session and
  returns a presigned live-view URL + viewport; `BrowserDockPanel` swaps noVNC for
  `BrowserLiveView` with a refresh-before-expiry timer (the DCV view already takes human
  input; a take-control switch waits until an agent/human input conflict is observed);
  `browser_view.py` and `browserViewUrl.ts` are deleted; the `bv-` CloudFront door stays for
  the gateway and local-session relays.
- **Shopify door:** the CLI's OAuth redirect is loopback-bound (`http://127.0.0.1:13387/…` on
  the host running `shopify store auth`), and the AgentCore Browser is not that host. The
  `shopify.ts` handler ports verbatim into the bridge (it is dependency-injected) and the CLI
  config dir joins the S3-synced profile, but the callback needs one of: (a) the desktop node
  listening on the Mac's `127.0.0.1:13387` and relaying the callback to the bridge (the human
  completes OAuth in their own browser; a natural M2 use), or (b) a Shopify-supported
  non-loopback redirect. M5 starts with that spike; nothing else in the Shopify rail changes.

### 4.8 State and migration
Profile directories are the unit of state (memory, `state.db`, skills, SOUL/AGENTS, caches).

**`hermes claw migrate` is not the migration tool** (report 01 §1): it is a single-user
desktop import that maps about eight scalar defaults, **archives** `agents.list`, bindings,
plugins, hooks, cron and the whole gateway block as JSON "for manual recreation", never reads
sessions, transcripts, credentials or agent state, has no notion of profiles or of our
`workspace-<uuid>` layout, refuses to apply on any conflict, and imports no AWS keys. At most
it is a developer convenience for SOUL/MEMORY/skills on an empty home.

So the migration is ours: a one-shot per-workspace job (backend script, reading the EFS
home the way `agent_export` does today) that materializes each Paperclip agent's profile from
the config writer (§4.5) plus the carried user data: `workspace-<agentId>/SOUL.md` and
`AGENTS.md`, `MEMORY.md` / `USER.md` into `memories/`, daily memory files, per-agent and
shared skills into `skills/` (exec-approval allowlists are not carried: Hermes runs unattended
behind the deny backstop, §4.5). OpenClaw session transcripts and the
state DB are **not** carried (they are OpenClaw runtime state; the room history lives in
DynamoDB and is replayed as `recentHistory` on every turn). The job writes the profiles to
`<owner>/hermes/<agentId>/` in the replicas bucket (§4.9) and flips the row.

### 4.9 Provisioning, secrets, wake and park on Hermes
(Detail: research report 04, "Provisioning flow on Hermes".)
- `ensure_user_ready` and `_provision_or_reconcile` stay the entry points; step 0 (the Paperclip
  seed in `provision_runner._seed_owner_paperclip`, plus `arm_nightly_ceo`) is reused verbatim.
- Step 1 becomes a **fresh-provision path**, not a patch of `agentcore_runtime.ensure_runtime`,
  which is a migration tool (it requires an existing ECS access point and a stopped row).
  The new path reuses `ensure_agentcore_trust`, `ensure_owner_aips` and `mint_gate_token`,
  drops the EFS `filesystemConfigurations`, builds the bridge's env, writes the profile set
  to S3 through the config writer, calls `CreateAgentRuntime` (synchronous, seconds; no Step
  Functions watchdog, keep the claim lease), and writes `runtime_host`, `agentcore_runtime_arn`
  and `status: running` on the row so every existing seam works unchanged.
- **Secrets:** AgentCore `environmentVariables` are plaintext to anyone with `GetAgentRuntime`.
  The runtime env carries only identifiers; the bridge fetches the hook token, gateway token,
  gate bearer, BYO keys and integration credentials from Secrets Manager at boot with the
  execution role. The gate-token supersede drain (`bedrock_gate_tokens.py`), which today reads
  ECS task definitions to decide when an old token is safe to revoke, learns to read the
  runtime's version instead.
- **State bucket:** the existing replicas bucket under the prefix `<owner>/hermes/<agentId>/`
  (the owner role already grants `<owner>/*`; the spike uses it); the CDK variable is renamed
  to say what it holds, no new bucket. An `AGENTCORE_HERMES_IMAGE` variable and a build
  workflow for `apps/infra/hermes/` are added.
- **Wake and park:** AgentCore parks an idle microVM invisibly, and `stop_user_service` is a
  no-op for AgentCore rows, so `is_parked_trial` never fires today. On Hermes "asleep" is a
  backend judgement from `last_interactive_at` against the runtime's idle timeout, the trial
  wake budget and the trial predicate are enforced at dispatch (they already run in the
  backend), and the "Agents asleep" card reads that judgement. No ECS stop, no `stop_reason`.
- **Paperclip runs:** today Paperclip drives an agent's run by connecting to the OpenClaw
  gateway as an operator through the relay (`openclaw_gateway` adapter). On Hermes the
  Paperclip adapter posts the run to a backend door that invokes the bridge with
  `eventKind: paperclip_run` and returns the output; which Paperclip adapter type carries this
  (an upstream Hermes adapter or our own) is decided in M1 after reading the fork.

## 5. Contracts the bridge and plugin must satisfy
Field-level detail with file:line citations is research report 03; this is the checklist
the bridge's tests are written from.

**Ingress** (`POST /plugins/lightbulb-channel/inbound/<agentId>`, gateway bearer): body
version 1 with `eventId`, `eventKind` (`user_request` | `room_event`), `targetAgentId`, `room`,
`sender`, `message` (incl. `id`, `threadRootMessageId`, attachments), `mentions`,
`participants`, `recentHistory` (trimmed from the oldest until the body fits 1 MiB),
`sessions[]` (live shared terminals, strict keys) plus the `X-Lightbulb-Local-Session-Token`
header, `revision`, `run {runId, workspaceId, frameChannel, frameEndpoint}`. Reply `202
{eventId, runId, status: accepted}`; a replayed `runId` answers 200 `replayed`; the ingress
effect runs once per eventId. Known bug to fix on the way: the canonical model allows
`application/pdf` attachments but the plugin parser accepts four image types, so a PDF in
history 400s the whole ingress.

**Context and threading:** the agent receives the room context (room, sender, mentions,
participants, sessions, eventKind, `message.id`, `message.threadRootMessageId`); session key
`agent:<agentId>:lightbulb:<kind>:<roomId>`; reply thread root = `threadRootMessageId ??
messageId` for channels and group DMs, linear for DMs (DM addressing lives in the backend's
`select_dispatch_targets`, not in the runtime). Room-event turns run behind the ambient
gate (the cheap "should I speak" LLM policy) before the full turn.

**Outbound:** `agent-messages` body `{eventId: <ingress eventId>:<agentId>:reply, roomId,
agentId, text, replyToMessageId, threadRootMessageId, runId}`; the receipt is the durable
idempotency row; the `send_room_message` tool targets `lightbulb:<roomId>` with an optional
thread root; the hire-suggestion result is the structured `isol8.hire_suggestion` payload
`HireCard` parses.

**Run lifecycle:** `run.started` at **accept** time (a queued turn must not look
unstarted), `run.heartbeat` every 30 s (sweeper closes a run 120 s after dispatch with no
signal, or 90 s after the last signal), `run.ended {outcome, errorKind?, usage}` where
`usage` is required unless the outcome is `ignored`/`gate_error`; callbacks retry three
times on 5xx only and never throw. Frames go to `run.frameEndpoint` on `run.frameChannel`
as `room.run.frame` events, signed with the execution role (on AgentCore the credentials
come from the instance metadata endpoint, tried last today): `run_active {active:true}` first;
`chunk`, `thinking`, `tool_start`, `tool_progress`, `tool_end`, `tool_error`,
`command_output`, `agent_activity` during the turn (streaming frames are dropped on a
non-200, lifecycle frames retried); then a final full-text `chunk`, `done` (or `error
{message}`), `run_ended {outcome}` and `run_active {active:false}`; every frame carries
`runId`/`agentId` and the run's `threadRootMessageId`. Abort (`POST …/runs/{runId}/abort`)
answers `202 {runId, state: aborting}` or `404 not_found`, ends the run `aborted` (zero usage
if it never launched) and emits `error {message: "Agent run was cancelled"}` + `run_ended`;
**there is no "Run stopped by <user>" text today**, so attributing the stop to a human is a
small new frame field, not parity. Steer (`…/steer {text}`) queues text into the running
turn or `404`s for a queued one; on Hermes the fallback is a second inbound turn on the
same session.

**RPC** (`POST /plugins/lightbulb-channel/rpc {method, params, timeoutMs}` → `{ok, payload}`
or `{ok:false, error:{code, message}}`, `AGENT_TIMEOUT` → HTTP 504): the plugin is a shim over
OpenClaw core, so the bridge implements every method itself. Load-bearing: `health` (the one
readiness check in the product: 200 only when ingress can be accepted, 2 s budget),
`config.get/patch/set` (provisioning repair + every control panel), `agents.create` (async
operation on the backend side; the bridge answers synchronously) / `agents.update` /
`agents.delete`, `channels.status`, `models.authStatus`. UI-only, stubbable first:
`tools.catalog`, `skills.*`, `sessions.*`, `commands.list`, `doctor.memory.*`, `models.list`,
`chat.history`, `agent.identity.get`. `browser.request` (cookie/storage snapshot) is retired
with the container browser. Admin ad-hoc dispatch stays an escape hatch.

**Local sessions tool:** `lightbulb_local_sessions` with actions `resources | read | send |
receipt`, Bearer = the per-turn signed token bound to owner/room/agent/eventId/sessionKey,
usable only inside the turn it arrived with; owed: the `kind` field on session summaries and
a `send` that can interrupt.

**Non-room usage:** `POST /internal/messaging/usage/{ownerId}` with `sessionKey, agentId,
runId` and the usage block for cron/heartbeat/Paperclip runs.

**Other plugin routes:** none on this branch (the Shopify routes live on the Shopify branch
and are rebuilt in M5).

## 6. Milestones and pass criteria

| # | Milestone | Passes when (dev, spike org, real product path) |
|---|---|---|
| M1 | Room-turn parity | threaded reply lands under the request; `run_active` + `room.run.frame` frames visible to a web client mid-turn (streaming chunks, tool frames, final resync, `done`); Stop ends the run `aborted` with the cancelled error frame pair; a follow-up posted with the `send_room_message` tool threads correctly; local-sessions tool reads a shared session; every load-bearing RPC method answers and the UI-only ones are stubbed; auxiliary calls no longer probe openrouter/nous; PDF attachments in history no longer 400 the ingress |
| M2 | Node exec (hard gate) | paired Mac runs `system.run` from a Hermes agent turn through the backend's node route, output returned, timeouts honoured, exec approvals honoured; the vendored node host stays connected ten minutes without a reconnect; works when the bridge's call lands on a backend task other than the socket's |
| M3 | Device commands | every command in the inventory (§3) works from a turn |
| M4 | Config, providers, provisioning | a fresh workspace provisions a Hermes runtime with the profile writer; Bedrock, ChatGPT OAuth and BYO each answer a turn; MCP integrations refresh tokens; hot patch applies on the next turn |
| M5 | Browser + Shopify door | panel shows the workspace's AgentCore Browser; agent drives it; Shopify login completes and survives a session roll |
| M6 | Migrate, roll out, delete | founder workspace migrated with the migration job and used for a day; prod dispatch; per-workspace flips; deletion list merged |

## 7. Coordination with in-flight work
- Freeze new features in the OpenClaw `lightbulb-channel` plugin (follow-ups #2708 was the last).
- Skills text keeps landing; tool names become neutral where they mention OpenClaw tools.
- Backend, frontend, mobile and desktop streams are unaffected: join links (#2712), membership cap (#2696), activeRuns (#2699), session sharing (#2689–#2692) all carry over.
- Shopify: PR #2710's backend/frontend proceeds; its plugin door is rebuilt in M5.
- Requirements from peers folded in: threading + `message.id` in context + send-to-room tool (proactive), `body.sessions` + signed token + `kind` passthrough (session sharing), run frames + an attributed stop message (activeRuns; new, since no such text exists today), run-command + browser + persisted CLI config (Shopify).

## 8. Rollout
Spike org (now) → founder org (M6 start) → one prod `workflow_dispatch` carrying the backend
and CDK → per-workspace flips (`runtime_host=agentcore`, runtime ARN) by the migration job →
deletion list. Prod stays on ECS until then.

## 9. Deletion list at cutover
(File-by-file tables with line cites: research report 04.) "Cutover" is the point every
workspace is on Hermes; until then the `_hosted_on_agentcore` no-op branches stay.

- **Backend, whole files:** `core/containers/workspace.py` (EFS I/O), `config.py`'s
  `openclaw.json` writer and reader (`build_openclaw_config_dict`, `build_provider_block`,
  `_build_exec_policy`, version-shape helpers; the node-pairing helpers move to the backend's
  node broker first), `config_patcher.py`, `efs_file_lock.py`, `core/openclaw_version.py`,
  `routers/browser_view.py`, `core/services/browser_state.py`, `update_service.py` (task-def
  image/resize queue).
- **Backend, partial:** about 60 % of `ecs_manager.py` (access points, task definitions,
  `create_user_service`, Cloud Map discovery, `is_healthy`, config-lock rollout,
  `reprovision_for_user`, sizing, Spot fallback), `provision_dispatch.py`'s subprocess and
  Step Functions watchdog machinery (keep the claim lease), `provision_runner.py` step 1 (keep
  step 0's Paperclip seed), `owner_facts.py`'s EFS write (facts move into the profile), the
  EFS-shaped parts of `paperclip_openclaw_pairing.py`, `agent_export.py`, `catalog_service.py`,
  `marketplace_hire.py` after each feature's Hermes home exists.
- **Infra:** in `container-stack.ts` the EFS filesystem and security group, Cloud Map namespace
  and service, the ECS task definition and the whole local-sqlite/litestream boot chain, the
  task-events log group, the `openclawExtendedRepo` and builder role; in `service-stack.ts`
  the `OPENCLAW_*` version-pin env block and `LITESTREAM_BUCKET`; `openclaw-version.json`;
  `.github/workflows/build-openclaw-image.yml`; `apps/infra/openclaw/` entirely (Dockerfile,
  `local-sqlite/`, `plugins/`, `chrome-extensions/`, `sage/`, `warp/`) once each baked plugin's
  feature has its Hermes home; the OpenClaw gateway image and its `AGENTCORE_GATEWAY_IMAGE`
  variable; the fork `TheLightbulbCompany/agent`.
- **Skills:** `apps/infra/openclaw/skills/` moves to the Hermes image, it is not deleted.
- **Frontend:** `browserViewUrl.ts`, `BrowserDockPanel.tsx` (replaced), `useBrowserToolAutoOpen.ts`,
  the admin image-update actions once AgentCore versioning replaces them; the hard-coded
  `.openclaw/workspace-{id}` path in `AgentCreateForm.tsx`.
- **CLAUDE.md:** the sections that describe ECS containers, EFS, litestream, the config
  patching notes, the container update system and the OpenClaw image pipeline are rewritten.

## 10. Risks and open questions
Each with a verification step; the research reports carry the detail.
- Node (report 02): the backend's node-protocol server must be tested against the vendored
  upstream node host, not only our Rust client → run the sidecar against a loopback proxy in
  front of the backend and require a clean `hello-ok`, a `system.run` round trip and ten
  minutes without a reconnect; `node.invoke.request` must carry `paramsJSON` as a string and
  the `hello-ok` must match the checklist → two pure-dict unit tests; cross-instance
  forwarding → integration test with two backend tasks.
- Provisioning and park (report 04): trial park never fires on AgentCore rows today → the
  backend judgement in §4.9 lands before any trial workspace moves; the gate-token drain reads
  ECS task definitions → port before the founder moves; plaintext runtime env → secrets fetched
  at boot from the first milestone on; `AgentCreateForm.tsx` hard-codes an EFS path → fix in M4.
- `/ws` under `networkMode: VPC` → **verified** (§4.4), kept as fallback only.
- Config (report 01): error-path request dumps cannot be disabled → sync-exclude + one
  deliberate failing call to read what redaction leaves; `pcp_` key coverage in
  `redact_sensitive_text` → **resolved**: `agent/redact.py` has no `pcp_` prefix but exposes a
  plugin prefix-pattern registry (`_PLUGIN_PREFIX_PATTERNS`), so the `lightbulb` plugin
  registers the Paperclip key shape; `Bearer <opaque>` header values are already folded to
  `Bearer [redacted]`;
  ACP coding harnesses (`sessions_spawn({runtime:"acp"})`, Claude Code delegation) have no
  Hermes equivalent → grep prod logs for live use before deciding on an MCP-shaped
  replacement; `bedrock.discovery.enabled` → pin false and watch for `ListFoundationModels`
  AccessDenied; `model` must be written as `{default: …}` (asserted in the writer's test);
  channel bot tokens duplicated across profiles are parked by multiplexing → the bridge, not
  Hermes's gateway, owns channel ingress, so this class does not apply (write it down).
- MCP servers per turn → **resolved**: cached per process by profile home (report 06).
- One ChatGPT grant for six profiles → supported in source, contradicted by the docs →
  hands-on test in M4 (report 06).
- Hermes memory vs `memorySearch` recall → ship native first, measure in dev (report 06).
- Browser (report 05): `agent-browser`'s native AgentCore provider → **confirmed to exist**;
  open: whether it can attach to a session the backend started, and whether its SigV4 signer
  accepts the container credential provider or needs env keys exported per turn; the VPC interface endpoint
  `com.amazonaws.<region>.bedrock-agentcore` must exist in our VPC (data plane; control plane
  needs NAT); the `BrowserLiveView` component's CSP needs (`wss://bedrock-agentcore…`, DCV
  client assets) → throwaway page; presigned live-view URL TTL → SDK default; 15 min session
  timeout vs panel lifecycle → start-on-open + profile save; the Shopify loopback callback →
  M5 spike (node relay vs Shopify redirect options).

## 11. Sources
AgentCore WebSocket streaming: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-websocket.html ·
Hermes terminal environment plugins: https://hermes-agent.nousresearch.com/docs/developer-guide/terminal-environment-plugin ·
Hermes issue #11014 (no node upstream): https://github.com/NousResearch/hermes-agent/issues/11014 ·
OpenClaw nodes: https://docs.openclaw.ai/nodes , /nodes/node-exec , /nodes/device-commands ·
AWS Hermes-on-AgentCore sample: https://github.com/aws-samples/sample-host-hermesagent-on-amazon-bedrock-agentcore
