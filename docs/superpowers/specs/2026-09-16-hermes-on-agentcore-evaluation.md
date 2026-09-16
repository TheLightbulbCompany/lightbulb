# Hermes Agent as the Lightbulb runtime on AgentCore — evaluation

**Question.** Should Lightbulb's per-workspace runtime be Hermes Agent instead of the OpenClaw gateway, now that the runtime is moving onto Amazon Bedrock AgentCore? Evaluated on how Hermes works and what it means for us long term. The 2026-09-15 rejection ("24–34 eng-weeks, one process per agent") is deliberately NOT the frame: the "one process per agent" premise is no longer true (§1, multiplexing), and the estimate is redone from an inventory (§5).

**Answer.** Hermes is the better structural fit for AgentCore and a lighter thing to carry, and it removes more of our runtime-side machinery than it adds (§4.4: no AppSync publishing from the container, no litestream for chat history, provider setup that maps one-to-one). It is not a free switch: one hard gap (our Bedrock billing gate) and one large rebuild (the local-session relay) are real, and Hermes moves fast enough to be its own risk. Recommendation: a three-day, pass/fail spike (§7) before committing either way; if it passes, plan Hermes as the runtime for the cutover and treat the OpenClaw move as the bridge. Nearly all of the AgentCore work done this week is runtime-agnostic and carries over (§4.1).

Written 2026-09-16, after the first real room turn from an OpenClaw gateway in an AgentCore microVM (spec `2026-09-15-agentcore-replatform-design.md`, P5a).

---

## 1. What Hermes is, in the terms that matter to us

Hermes Agent (Nous Research, MIT, Python 3.11) is a single agent core (`AIAgent` in `run_agent.py`, loop in `agent/conversation_loop.py`) with several entry points around it: a CLI/TUI, a **messaging gateway** (25+ platforms as plugins), an **OpenAI-compatible API server**, an ACP server, a cron scheduler and a batch runner. "Platform differences live in the entry point, not the agent."

| Concern | Hermes | What it means for us |
|---|---|---|
| Process model | One long-lived `hermes gateway` process. Each message creates a fresh agent instance that loads its session from SQLite. | Same shape as today's gateway; no per-turn process spawn unless we want it. |
| Multi-agent | **Profiles**: each is its own `HERMES_HOME` (config, `state.db`, memory, skills, cron). **Multiplexing** (`gateway.multiplex_profiles`, opt-in) serves every profile from ONE process: per-profile adapters, secrets, sessions (`agent:<profile>:…` namespace) and cron on one event loop and one HTTP listener. | A six-agent workspace is six profiles in one gateway. The "one process per agent" premise of the old rejection is gone. Limits: MCP discovery and terminal env are process-global (first profile wins), fine per workspace. |
| State | `~/.hermes/state.db` (SQLite, WAL, FTS5; schema v23): sessions, messages, usage, routing, delivery obligations. Per profile. Designed for "concurrent readers + one writer", 1 s busy timeout, app-level retry. Plus memory files and `skills/`. | The persistence problem we already solved for OpenClaw, smaller and per agent. And possibly avoidable for chat history (§4.4). |
| Memory | Agent-curated memory + FTS session recall; pluggable memory providers (single-select). | Replaces memory-core/wiki/dreaming. `hermes claw migrate` imports OpenClaw memories and skills per user. |
| Skills | `SKILL.md`, agentskills.io standard; self-created after complex tasks; skills hub. | Our bundled `lightbulb`/`paperclip` skills load as-is. |
| Tools | Central registry, 70+ tools / ~28 toolsets; plugins (`~/.hermes/plugins`, project, pip entry points); MCP client; **seven terminal backends** (local, Docker, SSH, Singularity, Modal, Daytona, Vercel Sandbox). | Exec on the microVM = local backend, same posture as OpenClaw exec. Our marketplace/exec-creds plugins become small tool plugins. |
| Custom platform | `BasePlatformAdapter`: `connect()`, `send()`, `handle_message(MessageEvent)`; may run an HTTP server inside the gateway; sessions keyed `(platform, chat_id, user_id)`; one turn per chat with queueing; hooks system. | Our messaging plane is one platform plugin, or unnecessary if the backend drives the Runs API (§4.2). |
| API server | In the gateway process, port 8642, bearer key. `/v1/chat/completions`, `/v1/responses`, **Runs API** (`POST /v1/runs` with `session_id`, `conversation_history`, `Idempotency-Key`; `GET …/events` SSE; `POST …/stop`; approvals), sessions API, Jobs API, `/v1/capabilities`. Multiplexed profiles at `/p/<profile>/`. `X-Hermes-Session-Key` for per-channel memory continuity. | This is our async turn contract (accept → run → events → stop), already built, per agent. |
| Bedrock | Native provider. Claude → Anthropic SDK `AnthropicBedrock` (SigV4; caching, thinking); GPT via Mantle; others via Converse. `AWS_BEARER_TOKEN_BEDROCK` honoured **only on the Mantle route**; no endpoint override on the Claude route. | **The hard gap.** See §4.3. |
| Footprint | Official Docker image ~193 MB; resident memory across 31 live containers: median 282 MB (139–858 MB). Requirements 1 GB RAM min, 2–4 GB recommended. | Versus our gateway image at 1.75 GB compressed (81.5% of AgentCore's 2 GB cap) and a 1 GB Node heap. |
| Maturity | 246k stars, 51k forks, MIT. Releases every ~4 days (v2026.9.14 latest), 100+ commits/14 days, 43k open issues. Nous maintains the Paperclip adapter; upstream Paperclip ships a `hermes-gateway` adapter that re-exports it. AWS published a reference for hosting Hermes on AgentCore. | Ecosystem gravity is real (Paperclip and AWS both treat it as first-class). Velocity cuts both ways: fast fixes, and churn to track. |

## 2. Fit with AgentCore Runtime

What this week taught, mapped onto both runtimes. AgentCore is built for "one agent, one session, invoke → run → respond", and its machinery (warm pool, snapshots, per-session microVMs, memory billed per session) rewards small, quickly-restorable state.

| AgentCore reality | OpenClaw gateway (now) | Hermes |
|---|---|---|
| Image cap 2 GB compressed, hard | 1.75 GB with Chrome and exec deps stripped; no headroom | ~0.2 GB base; with our skill binaries est. 0.6–0.9 GB |
| Cold start | ~80 s measured: ~35 s microVM + gateway boot with 22 plugins + litestream restore of the whole workspace | Python start in seconds; per-profile `state.db` restore is smaller, or none (§4.4); estimate 10–20 s, to be measured |
| Warm pool (10 instances/version run the CMD) | Needed the lazy-spawn contract server (gw-v6) to avoid ten replicators on one prefix | Same rule, same fix (start on first invocation) |
| Session model: long-lived per workspace, single writer | Forced by one gateway DB; one session per workspace, serialized | One session per workspace still (multiplexed gateway); per-profile DBs allow per-agent replication lanes; per-agent runtimes possible later |
| 8 h max lifetime → session roll | Full workspace restore on every roll | Smaller restore |
| Memory billing (continuous per session) | 8 GB microVM, ~1 GB heap gateway idle | ~0.3–0.5 GB idle; a 2 GB microVM would do |
| The one HTTP contract (`/ping`, `/invocations`) | Contract server proxies to :18789 | Same contract server proxies to :8642; the Runs API's SSE events can stream back through the invoke |
| AWS's own reference | `sample-host-openclaw-on-amazon-bedrock-agentcore` | `sample-host-hermesagent-on-amazon-bedrock-agentcore`: Hermes unmodified, `bridge/workspace_sync.py` syncs `state.db`/memory/skills with S3 at start/stop, router Lambda for channels, monkey-patch `anthropic.Anthropic → AnthropicBedrock`. Explicitly does not handle multi-agent or concurrency. |

Verdict on fit: Hermes fits the platform; the OpenClaw gateway runs against its grain and we have been paying for that all week (shim after shim). The one place Hermes does not help is the thing AgentCore is fundamentally not designed for: a stateful, always-there workspace. Both runtimes carry state in SQLite; Hermes makes the state smaller and per-agent, and §4.4 argues most of it need not be in the container at all.

## 3. What Hermes is not

- It is not a multi-agent *workspace*. Rooms, listen modes, mentions, the run ledger, membership, delivery keys, the reply path are ours and stay in the backend either way. Hermes runs turns.
- Its Claude-on-Bedrock path cannot reach our billing gate today (§4.3).
- It has no equivalent of our local-session relay (Claude Code / Codex sessions shown in rooms via the OpenClaw node relay). It has an ACP server and a browser-control WebSocket, not that.
- Multiplexing is new and opt-in ("everything it changes reverts the moment the flag is off"). We would be an early serious user.
- Config is `config.yaml` + profile dirs, read at gateway start; our hot-patch-openclaw.json-on-EFS story becomes "restart the profile's gateway" or an API call.

## 4. What switching actually touches

### 4.1 Carries over unchanged (the AgentCore work is runtime-agnostic)
`runtime_transport` (owner-derived session id, envelope, timeouts), `ensure_runtime` (per-owner role, trust, EFS access point, VPC, lifecycle, row flip), the ECS wake/stop chokepoints, the IAM (caller and execution role, including this week's five denials), the contract server (proxy target becomes :8642), the boot layout and the EFS fence, the messaging plane, the work queue, run ledger, sweeper, AppSync fan-out. Swapping the image and its env is the platform-side cost of a switch.

### 4.2 Rebuild against Hermes (our runtime-side surface today)
| Today (OpenClaw) | On Hermes | Size |
|---|---|---|
| `lightbulb-channel` plugin: `/inbound/<agent>` ingress with the run contract, `/rpc` (health, agents.list, channels.status, models.authStatus, browser.request…), `/runs/{id}/abort\|steer`, run events + reply POSTs to the backend, AppSync frame publishing from the container (MMDS creds) | **Preferred (from AWS's own reference): an in-process bridge.** A ~300-line Python contract server imports Hermes, holds one `AIAgent` per agent profile and runs `run_conversation(message, system, conversation_history)` per invocation; the backend supplies the room context, writes the reply and publishes frames. No gateway, no API server, no platform plugin. Fallback: the Runs API per profile (`/p/<agent>/v1/runs`, SSE events). | 2–3 wk |
| Per-agent identity/memory in one gateway (agents.list) | Six profiles under one `HERMES_HOME`, multiplexed; profile dirs on EFS/overlay like today's `agents/<uuid>/` | 1–2 wk |
| Paperclip `openclaw_gateway` adapter + our pairing/lifecycle code | Upstream `hermes-gateway` adapter (thin re-export of Nous's `hermes-paperclip-adapter/gateway`); our Paperclip fork is pinned before it existed → rebase or port; rework `paperclip_agent_lifecycle` / pairing | 1–2 wk |
| Bedrock via the gate (bearer + baseUrl) | §4.3 | 1 wk + decision |
| `lightbulb-marketplace` (hire suggestion), `aws-exec-profile`, `paperclip-exec-creds` | Tool plugin + terminal env injection via plugin hooks | 1 wk |
| Skills `lightbulb`, `paperclip` | Load as-is (agentskills standard); verify `references/` resolution | days |
| Memory (memory-core, wiki, dreaming) | Hermes memory; `hermes claw migrate --preset user-data` per workspace imports memories + skills; nightly dreaming → cron job or dropped | days + migration run |
| Agent mail `/hooks/agent`, Slack `/slack/events` | Ingress into the same Runs-API path (they are just turns with a different trigger) | 1 wk |
| `write_openclaw_config` (~60 sections) + config patcher | `config.yaml` + profile config generation; restart-to-apply | 1–2 wk |
| Local-session relay (desktop node hosts, Codex/Claude Code sessions in rooms, `gateway_relay` raw WS) | No Hermes equivalent; keep as a backend+desktop feature with a new container-side endpoint, or drop from the container | 2–3 wk |
| Browser | Chrome already leaving (P4); Hermes cloud browser via custom provider → AgentCore Browser integration is custom on either runtime | same either way |
| OpenClaw fork (`isol8.N` patches, 9.4 rebase debt) | Retired. Hermes likely needs one small patch (§4.3) we can upstream. | negative cost |

### 4.3 The hard gap: the Bedrock billing gate
Every inference call must go through the bedrock-gate with a per-owner bearer; owner roles carry no `bedrock:Invoke`. Hermes's Claude route builds `AnthropicBedrock(region)` and its own credential logic treats the bearer as Mantle-only. **Resolved on inspection (2026-09-16 evening): no Hermes patch is needed.** The Anthropic Python SDK's `AnthropicBedrock` takes `api_key` (a bearer, defaulting to `AWS_BEARER_TOKEN_BEDROCK`) and `base_url` (defaulting to `ANTHROPIC_BEDROCK_BASE_URL`), and a bearer skips SigV4 — the same two env vars the ECS task sets today. Hermes passes no static AWS keys into the constructor, so the SDK defaults apply. The catch: the Hermes image bundles SDK 0.87.0, which predates the parameter; current releases are 1.x. So the route is "bump one package in our image"; the spike confirms Hermes tolerates the bump (fallback: a 30-line subclass setting the bearer in `_prepare_request`). The gate proxies `/model/<url-encoded ARN>/invoke` verbatim, exactly as it does for OpenClaw. The gate-side alternative (accept SigV4 from the owner role, map role → owner, no plaintext bearer in the env) remains a good P6 posture change, independent of the runtime.

### 4.4 The owner's questions: auth, providers, multiplayer, integrations, litestream, multi-tenancy

**Auth.** Hermes has two auth surfaces and neither is user-facing for us. The API server takes one bearer key (`API_SERVER_KEY`, per profile under multiplexing, fails closed) — that is the backend↔container credential, the same role our `gateway_token` plays today, minted per workspace and put in the runtime env. Platform adapters have allowlists and DM pairing, which we would not use: humans never talk to the container; they talk to our API with Clerk, and the backend talks to the container. Nothing about Clerk, org membership or room ACLs moves.

**Provider setup.** Our three paths map one-to-one. Bedrock managed and the trial: Hermes's `bedrock` provider with the owner's application-inference-profile ARN as the model — through the gate, per §4.3. ChatGPT OAuth: Hermes ships an "OpenAI Codex" provider, which is the ChatGPT-account login, so the device-code tokens we hold today seed that provider's credential per profile instead of `oauth.json`. BYO key: `anthropic`/`openai` providers with the key from Secrets Manager in the env, as now. Per-request `model`/`provider` overrides on the Runs API mean the backend can pin the model per turn, which today needs a config rewrite.

**Multiplayer.** Unchanged in substance, simpler in mechanism. Rooms, listen modes, mentions, membership, delivery keys and the run ledger are backend-owned already. The one thing the container does today that it need not do on Hermes is publish run frames to AppSync itself (which is why `appsync.ts` needed MMDS credentials this week). If the backend drives the Runs API and relays the SSE events, the container publishes nothing and holds no AppSync or MMDS concern; the backend owns every frame. Session keys `(platform, chat_id, user_id)` map to (lightbulb, room, human), so per-room continuity per agent is native.

**Integrations.** The nine remote-OAuth MCP providers: Hermes is an MCP client, configured per profile, so the same server list we render into `openclaw.json` today renders into the profile config. Two caveats: MCP discovery is process-global under multiplexing ("first profile wins"), which is fine because integrations are per workspace not per agent; and the spec's P6 (credentials to AgentCore Gateway + Identity) applies identically to either runtime. Slack-app events and agent mail stop being gateway "hooks" and become ordinary turns submitted by the backend.

**Litestream, and whether we still need it.** Today litestream exists because the gateway's SQLite files ARE the product's memory of every session, and an ECS task loses its disk. On Hermes the honest split is:
- Chat history: our rooms are the canonical transcript already (DynamoDB, `seq`-ordered). The Runs API accepts `conversation_history` per run, so the backend can hand each turn the room context it needs and the container's session store becomes a cache. Nothing to replicate.
- Agent memory and skills (the things that must survive): small per profile. Options in order of laziness: sync the profile dir with S3 at first invocation and after each turn (what AWS's own sample does with `workspace_sync.py`); or a Hermes memory provider backed by a managed store (AgentCore Memory is the obvious one); or litestream per `state.db` exactly as today.
- Litestream stays only if we want crash-consistent replication of the container's own store. With chat history owned by the backend, that is a much weaker requirement than it is for OpenClaw. The spike decides between S3 sync and litestream by measuring restore time.

**Multiple users in one Hermes.** Yes in the sense that matters, no in the sense that would save money. A Hermes "user" is a chat participant; sessions are keyed by user and memory is per profile (per agent), so all the humans in a workspace already share one Hermes with six agent profiles, exactly like today's one gateway per workspace. What we should not do is put several *workspaces* in one Hermes process: multiplexing isolates profiles by context-local scope, not by OS boundary, the terminal tool runs as one OS user for every profile, and MCP discovery is process-global. The security boundary between customers is the microVM, one per workspace, which is also AgentCore's billing unit. So: one Hermes per workspace, N agent profiles inside it, any number of human members.

## 5. Effort and cost, honestly

Finishing the OpenClaw path (P4 browser, P5b/P5c sleep doors and cutover, P6 credentials): **~6–9 eng-weeks**, known items.

Hermes to parity from where we are: spike 3 days → §4.2 items **~10–15 eng-weeks serial**, of which the local-session relay and the Paperclip fork adoption are the uncertain halves; §4.4 removes work (no container-side AppSync, likely no litestream for history) that is not yet netted out of that range. Net: Hermes costs roughly **4–8 more weeks** than finishing OpenClaw, and retires the OpenClaw fork burden (the 9.4 rebase alone carried a 225-file patch set).

Run cost per workspace on AgentCore: memory is the continuous charge. At ~0.4 GB idle versus ~1+ GB, a Hermes session on a 2 GB microVM is roughly a quarter of the idle-awake cost of the 8 GB one we run now; CPU cost is per active turn either way. Not measured; the spike measures it.

## 6. Long-term view

- **Direction of the ecosystem.** Paperclip's own adapter is Nous-maintained; AWS published a Hermes-on-AgentCore reference; Hermes ships an OpenClaw migration. We are choosing between a runtime the platform and our orchestration layer both already speak, and one we patch to fit.
- **What we keep either way.** The product is the workspace, not the runtime. Rooms, the run contract, billing, Paperclip, session sharing all live in our backend. The runtime is replaceable, and this week proved the replacement seam works.
- **Risk in Hermes.** Release-every-four-days velocity with 43k open issues means breaking changes; multiplexing is young; Python runtime versus a team fluent in the Node plugin world; one fork patch to carry until upstreamed.
- **Risk in staying.** A runtime that fights the platform (every AgentCore feature needs a shim), an image at the cap with no room for growth, a heavier fork, and 80 s cold starts that the platform's fast-start machinery can never fix.

## 7. Decision test: a three-day spike, pass/fail

Same shape as the spike that greenlit AgentCore. One runtime, the throwaway dev workspace, criteria fixed before starting:

1. Hermes image (Hermes + our skill binaries) on AgentCore: **cold start per turn ≤ 20 s**, warm turn ≤ 5 s, image ≤ 1 GB compressed.
2. **Six profiles multiplexed** in one microVM, resident ≤ 1 GB, two profiles running turns concurrently.
3. **A room turn end to end** through our messaging plane: backend → invoke → `/p/<agent>/v1/runs` with the room's `conversation_history` → SSE events relayed → reply in the room, run started/ended in the ledger.
4. **Bedrock through the gate** with the owner's application-inference-profile ARN and bearer, by route §4.3(1) or (2); usage settles in the gate ledger. ChatGPT OAuth seeded into the Codex provider for one profile.
5. **State**: profile memory + skills survive a session roll via S3 sync (or litestream, whichever restores faster); the warm pool never starts a gateway.
6. `hermes claw migrate` on a real dev workspace's `~/.openclaw`: memories and skills present in the profile; the `lightbulb`/`paperclip` skills load; the nine MCP integrations configured from our existing rows.

Pass → plan Hermes as the runtime (the P4–P6 items re-scoped onto it; the OpenClaw cutover is the bridge until then). Fail on 1, 3 or 4 → stay on OpenClaw and revisit in two quarters.

## 8. Sources
- Hermes architecture: https://hermes-agent.nousresearch.com/docs/developer-guide/architecture
- Session storage: https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage/
- Platform adapters: https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters
- Gateway internals: https://hermes-agent.nousresearch.com/docs/developer-guide/gateway-internals
- Multiplexing: https://hermes-agent.nousresearch.com/docs/developer-guide/multiplexing-gateway
- API server: https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server
- Bedrock guide and `agent/bedrock_adapter.py`: https://hermes-agent.nousresearch.com/docs/guides/aws-bedrock , https://github.com/NousResearch/hermes-agent/blob/main/agent/bedrock_adapter.py
- Migrate from OpenClaw: https://hermes-agent.nousresearch.com/docs/guides/migrate-from-openclaw
- README / license / footprint: https://github.com/NousResearch/hermes-agent , https://fast.io/resources/hermes-agent-docker-deployment-production/
- Paperclip adapters: https://github.com/NousResearch/hermes-paperclip-adapter , upstream `packages/adapters/hermes-gateway` (re-export of `@paperclipai/hermes-paperclip-adapter/gateway`)
- AWS reference: https://github.com/aws-samples/sample-host-hermesagent-on-amazon-bedrock-agentcore
- AgentCore warm pool / startup: https://repost.aws/articles/ARCJIn3t7aRC2FxiRTV1SuCA/minimizing-startup-latency-with-amazon-bedrock-agentcore-runtime
- GitHub API (2026-09-16): stars, forks, releases, commit velocity for NousResearch/hermes-agent.
