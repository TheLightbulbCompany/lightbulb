# Agent runtime on AgentCore — design

Status: design for owner review, 2026-09-15. Implementation plans are written **per phase**
from this document, when that phase starts (superpowers `writing-plans`). Rulings recorded
here are dated; anything not dated is a recommendation.

---

## 1. Goal

Move each workspace's agent off its single always-on ECS Fargate box and onto managed
AgentCore services — the OpenClaw gateway on **AgentCore Runtime**, the browser on
**AgentCore Browser**, and OAuth tool credentials in **AgentCore Gateway + Identity** — so
that:

1. **the box stops being the constraint** — a microVM is 2 vCPU / 8 GB against today's
   1 vCPU / 4 GB, which is the shape that starved Forge on 2026-09-14;
2. **nothing is always-on** — a workspace is billed while it is awake, and wakes in under a
   minute;
3. **we operate four fewer things** (no Chrome, no X/VNC, no Cloudflare WARP, no Spot
   roulette) and the agent's shell can no longer read every credential we hand it.

We keep OpenClaw. We keep the backend, DynamoDB, AppSync, Paperclip, EFS and the S3
replicas exactly where they are. This is a compute and credentials move, not a rewrite.

---

## 2. Why

### 2.1 The incident that started this

On 2026-09-14 Forge answered a question with "Forge finished · no reply". The root cause,
established from live cgroup and process evidence:

- Forge ran `vitest`, `tsc` and `cdk synth` **in parallel** inside its 1 vCPU / 4 GB
  container. The three builds peak at ~3 GB together; the Node gateway's event loop was
  starved of CPU.
- The ECS health check is `GET /healthz` **served by that same event loop**. A starved loop
  reads as a dead one, so ECS killed the task mid-turn — twice.
- On restart, session recovery replayed the killed turn and swallowed the follow-up
  message.
- Chrome sits resident in every box from boot (1.8 GB, 15 processes) whether or not a
  browser is ever used.

One process is the agent loop, the build machine, the browser host and the health probe.
That is a **shared failure domain**, and no amount of tuning removes it — only separation
does. The owner's ruling: root cause, not band-aids; no half measures.

### 2.2 What else the current shape costs us

| Symptom | Cause |
|---|---|
| Founder-class boxes need 2 vCPU / 8 GB just to boot | 24 SQLite restores run beside a resident Chrome; a 1 vCPU box crash-looped on 2026-08-14 |
| Cold start is ~222 s | ECS placement 63 s + litestream restore 41 s + `doctor --fix` 35–84 s + gateway ~65 s |
| Spot savings are optimistic | The Spot→on-demand fallback is one-way; a box that once failed placement pays on-demand forever |
| apollo and stripe MCP have been silently broken in prod for days (660 / 595 failures in 7 days) while their rows say "connected" | One rotating refresh token is seeded into two independent refreshers (the in-container MCP SDK and mcporter; PostHog a third). The loser dies. Nothing watches |
| Every credential is readable from the agent's shell | Exec runs on the gateway host with `mode: full`; children inherit `process.env`; every materialized file is uid 1000, same as the shell |
| Paid workspaces cost 24/7 | ECS services are always-on; only trials sleep |

### 2.3 Why AgentCore, and why now

We evaluated four routes (§3.2). AgentCore Runtime is the one that removes the shared
domain **without replacing OpenClaw**: our container, our entrypoint, a 2 vCPU / 8 GB
microVM per session, CPU billed only while used, no Spot, no placement step, and managed
sessions that stop when idle. AgentCore Browser and Gateway+Identity take the two heaviest
and most credential-sensitive pieces out of the container entirely. The owner asked for
AgentCore if it was cheaper *or* faster; it is faster to wake and roughly the same price as
Spot (§8). What it buys is reliability.

---

## 3. What we decided

### 3.1 Rulings (owner, 2026-09-15)

| Ruling | Consequence |
|---|---|
| **Keep OpenClaw.** No Hermes, no AgentCore harness, no Strands rewrite | Every phase is a host or transport change; the agent loop, plugins and skills are untouched |
| **Gateway on AgentCore Runtime**, one runtime per workspace | The always-on ECS service per workspace is deleted at cutover |
| ~~Exec in a worker session~~ → **exec stays on the gateway** (revised 2026-09-16) | The image arithmetic never required it: cutting exec leaves 2,264 MB, still over the 2,048 MB cap. Only cutting ACP fits. See §6.1 |
| **Browser on AgentCore Browser** | Chrome, X/VNC, WARP and the browser-view door are deleted |
| **MCP tools on AgentCore Gateway + Identity** — scoped to the nine remote-OAuth MCP providers (§5.4) | Tokens leave the container; one refresher | BUT NEEDS RECONCILING BECAUSE WE HAVE MANY INTEGRATIONS
| **Delete Telegram and Discord**, including the one live Telegram bot | No workspace ever needs an always-on process; sleep becomes possible | CAN REMOVE THESE PLUGINS FROM OUR IMAGE
| **Lobster is unused** | The sandbox's Lobster refusal needs no action |
| **No ECS fallback.** Everything is verified on dev first, then fixed forward | No dual-run mode, no per-owner "back to ECS" switch. The only ECS residue allowed is the worker transport, and only if AgentCore's shell API cannot hold a background job (§5.2) |

### 3.2 Rejected

| Route | Why not |
|---|---|
| **Guardrails only** (caps, alarms, "builds belong in CI") | They shrink the blast radius; the shared domain stays. We still ship them (§6, P1) because they are also AgentCore prerequisites |
| **Gateway stays on ECS, only exec/browser move** (the earlier target design) | The owner rejected it as a half measure; ECS keeps always-on billing, Spot fallback and placement latency |
| **Hermes Agent** | 24–34 engineer-weeks to parity; one process per agent identity; undocumented API-server contract; no custom-channel plugin API; Slack Socket Mode only |
| **AgentCore harness** | It **overrides the container's `ENTRYPOINT` and `CMD`**; a custom image supplies only filesystem and tooling, never the reasoning loop. Bare Runtime is the right primitive |
| **The `builder.aws.com` "Rebuilding OpenClaw on AWS" approach** | A Strands re-implementation of OpenClaw's concepts (one agent, seven tools, no cold-start numbers). The rewrite route, not a host |
| **AgentCore Memory for transcripts / agent memory** | Not a file or markdown store. Our memory index is <1 MB and rebuildable; replacing memory-core loses dreaming and the wiki bridge; transcripts, cron, tasks and the delivery queue sit in ~150 tables behind the fork's Kysely layer |
| **Dropping litestream** | Session storage is wiped on every version update and capped at a non-adjustable 1 GB (the founder is at 841 MB); EFS/S3 Files are NFS, where OpenClaw forces rollback journaling that litestream cannot replicate. See §5.5 |
| **OpenClaw cloud sessions / cloud workers** | They move the **whole agent loop** to a leased machine, and that machine runs with `plugins: { enabled: false }` and a closed 12-tool catalog (`src/worker/tool-authority.ts`) — so `lightbulb-channel`, the plugin that receives room messages and posts replies, does not exist there. Dispatch also requires a session that owns a git worktree or repository workspace; room sessions own neither. A cloud session cannot run a room turn at all. See §5.2 |
| **Hosted exec vendors** (E2B, Daytona, Modal, …) | None can mount our EFS; OpenShell copies the workspace per turn and serializes turns |

---

## 4. Target architecture

```
  browser / mobile ──HTTP──► backend (FastAPI on ECS — unchanged)
        ▲ AppSync (live)         │
        │                        ├─ InvokeAgentRuntime ─────────────► GATEWAY RUNTIME (one per workspace)
        │                        │   /invocations · /ws · /ping        ARM64 microVM, 2 vCPU / 8 GB burst
        │                        │                                     EFS AP at /mnt/oc → ~/.openclaw (symlinks)
        │                        │                                     state/ agents/ = local disk (SQLite)
        │                        │                                     litestream ──► S3 replicas (unchanged)
        │                        │
        │                        ├─ InvokeAgentRuntimeCommandShell ──► WORKER RUNTIME (one per workspace)
        │                        │   (or Fargate ssh — spike Q3)        toolchain image, same EFS AP
        │                        │
        │                        ├─ StartBrowserSession ─────────────► AGENTCORE BROWSER (+ DCV live view)
        │                        │
        │                        └─ mints per-owner OIDC JWT ────────► AGENTCORE GATEWAY (9 MCP targets)
        │                                                                └─ IDENTITY token vault
        └───────────────────────────────────────────────────────────────────────────────────────┘
```

**Where each piece runs**

| Unit | Today | Target | One-line why |
|---|---|---|---|
| Agent loop, model calls, plugins, cron, file tools, room ingress, RPC, relays, litestream | ECS Fargate service, always-on | **Gateway runtime**, awake on demand | Everything that reaches the agent still dials one long-lived process; it just no longer hosts builds or a browser |
| `exec` / `process` (git, gh, pnpm, vitest, tsc, cdk, python, aws, gog…) | Same box | **Worker runtime** | Builds get their own 2 vCPU / 8 GB and cannot starve the loop |
| Chrome + live view | Same box (1.8 GB resident) | **AgentCore Browser** | Removes 1.8 GB and 15 processes; logged-in profiles and takeover keystrokes stay in our account |
| Remote-OAuth MCP (9 vendors) | In-container, tokens on EFS | **Gateway + Identity** | Tokens never enter the container; one refresher |
| Everything else (backend, DynamoDB, AppSync, Paperclip, EFS, S3 replicas, bedrock-gate, Slack, email) | — | **Unchanged** | |

**Two runtimes per workspace, not one.** AgentCore `filesystemConfigurations` are set on the
runtime, mount into every session, must live under `/mnt/<x>`, and appear only at
invocation. Each owner's EFS access point therefore needs its own runtime — for the gateway
*and* for the worker. That is also how each owner keeps a distinct execution role, which is
AWS's own recommended multi-tenant control: the docs say AgentCore does **not** enforce that
a sessionId belongs to the calling user, and name per-tenant IAM principals as the fix.

**The scaling limit to watch:** 1,000 runtimes per account (adjustable), **shared by dev and
prod**. Two per workspace = 500 workspaces at the default. File the increase first (§6, P0).

### 4.1 Runtimes vs sessions — what exists when

A **runtime** is a definition: image + execution role + EFS mount config. It is a
control-plane resource that costs nothing while nothing runs. A **session** is a live
microVM created by the first `InvokeAgentRuntime` carrying a new `runtimeSessionId`; it is
the only thing that bills. Two runtime definitions exist per workspace permanently. Sessions
exist only while used:

| Session | Starts when | Stays alive while | Stops when | How many at once |
|---|---|---|---|---|
| **Gateway** | A human opens the app (the warmup invoke, §5.1), or any inbound turn arrives for a workspace with no live session — room message, Paperclip routine, Slack event, email, due cron job, desktop pairing, external hook | Runs, relays or shell sessions are active (the `/ping` busy signal) | ~20 min idle → backend prepare-stop + `StopRuntimeSession`; or the 8 h cap, rolled at a quiet point | **Exactly one per workspace** (single-writer invariant) |
| **Worker** | The **first `exec` call** of a turn — the sandbox backend's `ensureWorker()` | Shell commands keep arriving; a background build holds it | ~30 min with no shell activity; always when the gateway sleeps | **Zero** if no agent runs a shell command; at most one per workspace, shared by all its agents |
| **Browser** | The first CDP use, or the panel's Start button | CDP activity or panel input pings | 10 min idle (our sweeper; the AgentCore TTL is only a backstop) | Zero or one per workspace |

So a workspace that only chats pays for one session. A workspace that builds pays for two
while the build is warm. Nothing is pre-warmed on a timer — that would be always-on billing
by another name. The `runtimeSessionId` is also the **affinity key**: AWS routes a reused id
back to the same microVM and says a request without a consistent id "may be routed to a new
microVM," so the backend holds one id per workspace for the session's life.

---

## 5. How each piece works

### 5.1 Gateway runtime

- **Image:** a gateway-only ARM64 target — no exec tooling, no ACP harnesses, no browser
  stack. ≈1.25–1.41 GB compressed (prod today: 3,231 MB, 77 layers, amd64). `USER 1000`,
  because more than 53 layers plus a non-numeric `USER` fails container start with HTTP 424.
- **Contract server** (in the entrypoint): `/ping` returns `Healthy` or `HealthyBusy` from a
  liveness thread plus the plugin's busy signal; `/invocations` unwraps an envelope onto
  `127.0.0.1:18789` with a path allowlist (room ingress, RPC, abort/steer, hooks — the 202
  contract is kept); `/ws` pipes to 18789 with fragmentation (gateway messages reach 25 MB;
  the stream caps at 64 KB frames); returns 503 until the gateway is ready; restarts a
  crashed gateway in-VM without losing local SQLite.
- **Filesystem:** `~/.openclaw` is a **local** directory. Every EFS spine entry
  (`openclaw.json`, `workspace-<id>/`, `wiki/`, `credentials/`, `.config`, `.aws`, `skills/`,
  `media/`, …) is a symlink to `/mnt/oc`. `state/` and `agents/` are real local directories
  holding the SQLite DBs. See §5.5 for why the layout is exactly this.
- **Sessions:** the backend owns the lifecycle (§4.1). **The warmup invoke** is how a
  session comes to exist before anyone needs it: the frontend already polls
  `GET /container/status` every ~3 s while the app is open (`routers/container.py:138`, which
  dispatches `ensure_user_ready`; today that poll is what wakes a parked trial box). On
  AgentCore the backend, on that poll, sees no live session for the workspace and sends one
  `InvokeAgentRuntime` with a no-op `/health` envelope and a fresh `runtimeSessionId`. That
  call boots the microVM, mounts EFS (mounts appear only at invocation), runs the litestream
  restore and starts the gateway; the contract server answers 503 until the gateway calls
  back "ready", and the backend marks the session live. The boot therefore overlaps with the
  human reading the screen and typing instead of starting when they hit send. Every other
  wake door (room message off SQS, Paperclip routine, Slack event, inbound email, due cron
  job, desktop pairing, external hook) is the same invoke, triggered server-side. A warmup
  does not pin the session open: the idle clock still runs, so a human who opens the app and
  walks away gets the same 4 h stop. Idle → the backend runs prepare-stop (clean gateway
  exit, litestream final sync) then `StopRuntimeSession`; AgentCore's own idle timeout is
  only a backstop. `maxLifetime` is a hard 8 h, so busy sessions roll over at a quiet point
  before it.
- **Single writer:** never invoke a new `runtimeSessionId` for a workspace until the
  previous session has stopped. This replaces ECS `maximumPercent: 100`.
- **Deploys:** `UpdateAgentRuntime` per owner creates an immutable version; live sessions
  keep the old one until they end; rollback is re-pointing at the previous digest.
- **Credentials:** the owner's existing `…-owner-bedrock-{owner}` role, re-trusted from
  `ecs-tasks` to `bedrock-agentcore` with a SourceArn condition. The gate bearer is fetched
  from Secrets Manager at boot (as runtime env, a rotation would reach only new sessions).
  Any process in the VM can read the role's credentials via MMDS — the same posture as
  169.254.170.2 today — so the role stays narrow. **Measured correction (2026-09-16):**
  AWS's harness docs say `InvokeAgentRuntimeCommand` runs as root (uid 0); it actually runs
  as the container's own `USER` (**uid 1000** in our probe). Better for us — it matches the
  gateway's uid — but do not design against the documented claim.

### 5.2 Exec — stays on the gateway

**Revised 2026-09-16.** This section used to specify a second "worker" runtime with a custom
`lightbulb-exec` sandbox backend. The measured image cap removed the reason for it: cutting the
exec toolchain leaves 2,264 MB, still over the 2,048 MB ceiling, while cutting ACP alone gets to
1,728 MB (§6.1). Exec was never the block.

So `exec` and `process` run on the gateway, as today, on a microVM with **2 vCPU / 8 GB** —
double today's 1 vCPU / 4 GB. The parallel builds that starved Forge peaked around 3 GB
combined; whether that still starves on the bigger box is **unmeasured**, and measuring it is
the trigger for reviving the worker.

**What a worker would still buy, and why it is deferred rather than deleted:**

| Reason | Status |
|---|---|
| Image size | **Gone.** ACP is the mandatory cut, not exec |
| Build starvation | **Unmeasured** on 2 vCPU / 8 GB. Revisit if it still happens |
| Credential fence — `exec: full` children inherit the process env and read every materialised credential on EFS | **Still true**, and the sandbox is the only thing that fences it. But this is today's posture, so deferring is choosing not to fix it, not introducing a regression |

**If it is revived**, the design holds and the spike validated its transport: OpenClaw exposes a
real sandbox seam (`registerSandboxBackend`; built-in `docker`/`podman`/`ssh`), a backend returns
an argv the gateway spawns as a local child so `background`/`poll`/`kill` work unchanged, and
AgentCore's one-shot `InvokeAgentRuntimeCommand` **does** hold background processes between calls
(measured), streams output, and runs six concurrent commands per session — so a local `lb-exec`
client can be the process handle exactly as `ssh` is, with no need for the interactive shell API
or its 10-shell cap. Two constraints carry: `removeRuntime` must be a no-op and prune disabled
(the built-in `ssh` backend `rm -rf`s the runtime root, which against our EFS access point would
delete the owner's workspace), and enabling the sandbox **refuses ACP**
(`spawn-plan.ts:380-383`) — moot while ACP is out of the image, relevant if it returns.

### 5.3 Browser

- **AgentCore Browser**, one session and one profile per workspace, driven through a
  ~300-line loopback CDP proxy in `lightbulb-channel` that emulates Chrome's `/json/*` and
  pipes to the header-signed upstream. OpenClaw's browser plugin needs no patch.
- **The backend holds all AgentCore browser permissions; the container holds none.** With
  `StartBrowserSession` an `exec: full` agent could otherwise mount another owner's EFS
  through `filesystemConfigurations`.
- **Live view** is DCV, presigned 60 s, vendored `dcvjs`. Single viewer with "Take over".
  Fixed 1280×800. Opening the panel no longer launches a browser; there is a Start button.
- **Lifecycle:** the browser TTL is a flat absolute timeout with no idle reset, so we always
  send `sessionTimeoutSeconds: 28800` and run our own 10-minute idle stop; profile saved
  every 15 min, on stop, and at TTL − 10 min.
- **Why AgentCore over Kernel/Browserbase:** custody. A profile holds the owner's logged-in
  sessions and a takeover captures keystrokes; AgentCore keeps both in our account with no
  new subprocessor. The gateway-side work is identical for any vendor because `cdpUrl`
  cannot hold a secret, so Kernel remains the exit behind the same proxy.

### 5.4 Credentials: Gateway + Identity — yes, for nine providers, and not first

**What they are.** *Identity* is a token vault: one vendor OAuth token per (agent, user),
refreshed by AWS, never handing the refresh token to agent code. *Gateway* is one MCP
endpoint fronting many tool servers; on each call it pulls the caller's token from Identity
and attaches it, so the token never enters the container.

**Why yes.** It is the only rail on the table where a token leaves the container entirely.
It fixes the apollo/stripe refresh race by construction (one refresher). Forty per-owner
OAuth client registrations in Secrets Manager collapse to nine confidential clients, with
Identity's per-user vault doing the tenant split. It is nearly free ($0.005 per 1,000
Gateway invocations; Identity free through Gateway/Runtime).

**Why only nine.** Gateway fronts MCP servers, not CLIs. `gh`/`git`, `aws`, `gog`, `xurl`,
Vercel, Shopify, the Instagram and Paperclip curls and the harnesses all need the token in
their own file or env on the worker; that does not change. Scope: stripe, notion, sentry,
linear, attio, intercom, posthog, apollo, cloudflare.

**Why not first.** Least coupling to the incident, most vendor-by-vendor unknowns: Identity
has no public-client mode and no dynamic client registration, so each vendor needs a
confidential client registered against AgentCore's callback and verified one at a time; we
must build a backend-minted OIDC issuer because Clerk cannot mint a machine token; 3LO
targets need DEFAULT listing with our backend driving `CompleteResourceTokenAuth`. It runs
as the parallel phase (§6, P5). **Prod does not wait on it to unbreak apollo/stripe** —
confirm the dual-refresher cause in P1 and stop seeding the second copy.

**What it does not do.** A shared gateway does not isolate tenants by itself: one gateway
aggregates all targets into one tool namespace with no per-caller filtering. Credentials
resolve per user (owner A can never call with owner B's token), but every owner *sees* every
vendor's tools in `tools/list`. Acceptable for nine vendors. **Cedar/Policy only intercepts
Gateway calls** — no enforcement over the worker shell or the gateway's own outbound calls;
it is not a substitute for the bedrock-gate or the sandbox.

### 5.5 State: keep litestream

Today's design minus the nested ECS volumes: SQLite on the microVM's local disk, restored
from S3 at every session start, replicated by litestream every 10 s. Three facts decide it:

1. AgentCore session storage is **wiped on every `UpdateAgentRuntime` version update** and
   after 14 idle days. With one runtime per workspace, every image roll would erase cron
   jobs, MCP OAuth tokens, pairings and the only live copy of a rotated ChatGPT refresh
   token.
2. Its cap is **1 GB, not adjustable**. The founder workspace is at **841 MB** across 13
   DBs, growing ~28 MB/day; Forge's transcript table alone is 160 MB.
3. EFS and S3 Files are NFS. OpenClaw forces rollback journaling on NFS, litestream cannot
   replicate a rollback-journal DB, and `node:sqlite` commits run synchronously on the
   gateway's main thread: ~95 ms on EFS vs 1.38 ms on local disk. That is why the fleet left
   EFS on 2026-08-04.

Two traps that would otherwise fail silently:

- OpenClaw classifies the **lexical** path against `/proc/self/mountinfo`. **Measured
  2026-09-16, and the trap runs the opposite way to what we predicted.** On AgentCore the
  EFS mount is `nfs4`, and SQLite there happily reports `journal_mode = wal` and commits —
  SQLite does not refuse it, though its own docs call WAL over a network filesystem unsafe.
  So the dangerous case is not "a DB under the mount silently drops to rollback"; it is **a
  DB reached through a symlink from a local path *into* `/mnt/oc`**: the lexical path looks
  local, OpenClaw picks WAL, and the bytes are on NFS. The fence must therefore classify the
  **resolved** path (`readlink -f`), not the lexical one. Our §5.1 layout is still correct
  precisely because `state/` and `agents/` are real local directories and only the EFS spine
  is symlinked outward — but the fence, a post-start `PRAGMA journal_mode` assert on every
  DB, and the replica-lag alarm are what keep it correct.
- AgentCore's termination grace is **15 s total**. litestream's default
  `shutdown-sync-timeout` is 30 s and OpenClaw's drain can take minutes, so today's stop
  path would skip the final sync. Hence the control socket, `shutdown-sync-timeout: 10s`,
  and a TERM trap running `litestream sync -wait` before the gateway is signalled.

Cold-start lever: restore time scales with **bytes downloaded**, not live size — a founder
restore pulls ~2.3 GB (daily snapshot + 22 h of deltas) to rebuild 0.84 GB.
`snapshot.interval: 24h → 1h` and the cache-ttl fork fix (−26% on Forge) attack that
directly.

Litestream can only be retired by **AgentCore Instances + an EBS capacity-provider volume**
(survives stop/resume *and* version updates, 14-day sessions, EC2 on-demand + 12%). A
different compute type; revisit after cutover.

### 5.6 What gets deleted

Chrome + Mesa + fonts + the popups extension · Xvnc, matchbox, websockify, noVNC, `DISPLAY`
· Cloudflare WARP (apt repo, boot script, enrollment secret, proxy port) · the browser-view
router, its ALB pattern and the `bv-` path · `browserViewUrl.ts` · Telegram and Discord (28
non-test files: the BotSetupWizard, `routers/channels.py`, the channel-account carry-forward,
the Dockerfile plugin layer) · ACP harness processes on the gateway · the per-workspace ECS
services and task definitions · the 2 vCPU / 8 GB reliability floor · the nested ECS volumes,
`stopTimeout` override and `_tasks_fully_stopped_blocking`.

---

## 6. How we get there

Five phases. The spike that used to be P2 is **done** (§6.0), and two former phases are gone:
"must-fix on the box we have" (it patched the ECS container this migration deletes — the
opposite of fix-forward) and the exec worker (the image arithmetic never required it). An
implementation plan is written per phase, when that phase starts. Prod is reached only by
`gh workflow run deploy.yml --ref main` with owner approval (CLAUDE.md).

| # | Phase | Delivers | Est. | Blocks |
|---|---|---|---|---|
| **P1** | arm64 base | the fork publishing an arm64 image for our tag | 1–3 d | everything |
| **P2** | Gateway image | browser + ACP stripped, arm64, `USER 1000`, arm64 litestream — **under 2,048 MB, measured** | 3 d | P3 |
| **P3** | Boot it for real | contract server + filesystem layout; a real OpenClaw gateway serving a room turn from a microVM | 1.5 wk | P5 |
| **P4** | AgentCore Browser | restores the browsing P2 removed; deletes Chrome/X-VNC/WARP/browser-view, Telegram, Discord | 2 wk | — |
| **P5** | Cutover | backend transport seam, sleep/wake doors, per-workspace migration, ECS deletion | 2–3 wk | — |
| **P6** | Credentials → Gateway + Identity | the nine remote-OAuth MCP providers, one vault | 2–3 wk | — (parallel, any time) |

**≈8–10 weeks serial for one engineer**, down from 13–17 — the worker tier and the must-fix
phase both came out. P6 runs in parallel whenever convenient.

**Deferred, deliberately, with the trigger for revisiting each:**

| Deferred | Revisit when |
|---|---|
| **Exec worker + sandbox** | 2 vCPU / 8 GB is measured under parallel builds and still starves, **or** the credential fence matters enough on its own (`exec: full` reads everything in-process and on EFS — today's posture, not a regression) |
| **ACP-from-EFS** | Losing streamed Claude Code / OpenCode child sessions actually hurts. The harnesses are npm packages and EFS is already mounted, so this stays cheap to add |
| **The carry-over must-fixes** (liveness signal → `/ping`, replay guard, litestream `shutdown-sync-timeout`, heap cap, cache-ttl bloat) | They ride **inside** P3 and P5 rather than as their own phase. Details in the superseded plan at `docs/superpowers/plans/2026-09-16-agentcore-p0-p1-must-fix.md` |
| **Quota increases** | workspaces > 200 (runtimes) or > 60 (browser profiles). At 8 owners we use 8 of 1,000 |

### 6.0 The spike — DONE 2026-09-16

Ran in dev, all resources torn down, nothing committed. Findings are in §5 and the assumption
table below; the decisive one is the image cap in §6.1.

| Q | Answer |
|---|---|
| Image cap | **2,048 MB compressed, `Adjustable: False`** (`L-0A9E32B3`). 1.52 GB → READY; 2.88 GB → `ServiceQuotaExceededException`. Uncompressed is not capped (3.93 GB accepted) |
| Scratch disk | 2 vCPU / 8 GB RAM; root `overlay`, 8.8 GB with 5.0 GB free |
| Cold start | **34.4 s** cold on a 1.52 GB image, 1.2 s warm (ECS today: ~222 s) |
| EFS + VPC | Mounts at `/mnt/oc` as `nfs4` over a local proxy; writes fine; **Cloud Map private DNS resolves** to Paperclip and egress works |
| Shell API | Background processes **survive** between one-shot calls; output streams; six concurrent commands run in parallel. The interactive shell and its 10-shell cap are unnecessary |
| Not run | `/ws` fragmentation (answer it against the real contract server in P3) and the sandbox feature sweep (only needed if the worker is revived) |

### 6.1 The arithmetic that fixed the architecture

Prod extended image = **3,231 MB compressed**. Block sizes: base 991 · acpx 476 · opencode 278 ·
apt incl. X/VNC 236 · npm globals 233 · claude-code 195 · Chrome+Mesa+fonts 311 · WARP 143 ·
Go binaries 107 · AWS CLI 58 · gh 15.

| Remove | Result | Fits 2,048 MB? |
|---|---|---|
| Browser stack only (−554) | 2,677 MB | ✗ |
| Browser + exec toolchain (−554 −413) | 2,264 MB | ✗ |
| **Browser + ACP (−554 −949)** | **1,728 MB** | **✓** |

**ACP is the only mandatory cut; exec is optional.** That is what removed the worker tier from
the critical path. ~1,728 MB leaves ~16% headroom — an amd64 estimate, replaced by a real
number in P2.

### P1 — arm64 base (revised 2026-09-16 after diagnosis)

`ghcr.io/thelightbulbcompany/agent:2026.9.3-isol8.2` is **amd64 only**, and the API is explicit:
`Supported platforms: [arm64]`.

**Diagnosed, not guessed.** The multi-arch workflow on the fork's `main` (jobs `build-amd64` /
`build-arm64` / `create-manifest`) has **zero runs, ever**. Our tags are cut from `isol8/*`
branches, which carry a *different* file of the same name — "Private Isol8 Candidate" — with one
job, one step, `platforms: linux/amd64` hardcoded and an explicit header comment: *"amd64 only —
prod ECS Fargate is x86."* It was a deliberate choice, correct at the time. Every isol8 tag back
to v2026.6.1 is amd64-only. **No fatal amd64 hardcoding exists in the fork's Dockerfile** (pnpm
resolves arch via `process.arch`; the one arch-sensitive native addon, matrix-sdk-crypto, is not
in this pipeline's `OPENCLAW_EXTENSIONS`).

**Two routes, and the proof does not need the slow one:**

| | Route |
|---|---|
| **Now — local native build** | This Mac is arm64, so building the fork base here is **native, no emulation**. The base images are public digest-pinned `node:24-bookworm` (multi-arch), and the build needs no secrets or private registries. Worktree at the prod tag: `~/Desktop/agent-arm64-build.nosync` @ `v2026.9.3-isol8.2`. This unblocks P2/P3 immediately |
| **Later — fix the candidate workflow** | On the branch that cuts the next tag (`isol8/2026.9.4-rebase`): `platforms: linux/amd64` → `linux/amd64,linux/arm64` plus a `docker/setup-qemu-action` step, because that job runs on a **single amd64 Blacksmith runner with no paired native-arm64 job** — so CI pays QEMU emulation. `ubuntu-24.04-arm` has **never been scheduled in this repo**, so native runners are unverified (upstream's public repo proves the mechanism works in principle). Budget half a day plus a slow first build |

**Ruling (2026-09-16): build locally now, defer the CI fix.** Reproducible releases need the
workflow fixed eventually; proving the gateway boots does not. Deferring costs a manual build
step until someone needs a reproducible arm64 release.

**GHCR safety:** push **new tags only**; never republish `v2026.9.3-isol8.2`. Dev and prod run
the identical extended tag and `Dockerfile:126` pins the base by digest, so a new tag is inert
until something `FROM`s it.

### P2 — Gateway image (3 days)

A new `Dockerfile.gateway` (separate file, not a refactor of the 986-line original — additive,
zero risk to the prod image, deleted at cutover). Strips Chrome/Mesa/fonts, X/VNC, WARP, acpx,
opencode, claude-code. Keeps exec tooling, the baked plugins, skills, MCP servers, litestream
(**arm64 tarball** — the current pin is linux-x86_64). `USER 1000`, not `USER node`: more than
53 layers plus a non-numeric `USER` fails container start with HTTP 424.

**Acceptance: the pushed image is under 2,048 MB compressed, verified by `CreateAgentRuntime`
succeeding — that is the only authority.**

### P3 — Boot it for real — **DONE 2026-09-16, in dev**

Every layer proven in a real AgentCore microVM. Runtime `lb_gw_litestream_v5-1kN5AC9NMH`
(VPC + EFS access point `fsap-0ec88d77c4cb3bf2c` at `/mnt/oc`), image `gw-v5`.

**What runs:** the **unmodified production boot chain** —
`isol8-local-sqlite-boot.sh` → `litestream -exec` → `app.sh` → doctor + gateway — under a new
contract server. Measured end state: `state/openclaw.sqlite` restored from S3 (7.1 MB),
`journal_mode=wal`, `fs=overlay` (local, fence-approved), litestream replicating, gateway ready.

| Measured | |
|---|---|
| Gateway ready, in-microVM | **14–22 s** from container start (5.5 s for the same image locally) |
| Cold invoke, end to end | **54–57 s** (the 1.52 GB stub was 34 s) |
| Compressed image | 1.750 GB = **81.5%** of the 2,048 MB cap |
| Disk | overlay 8.8 GB, 5.4 GB used by the image, **3.0 GB free** |
| AWS credentials | work via MMDS; the CLI assumes the execution role. MMDSv2 token required |
| Cloud Map DNS, VPC egress | resolve and connect |

**New artifacts** (uncommitted, in `apps/infra/openclaw/agentcore/`):
- `contract-server.mjs` — `0.0.0.0:8080`, `GET /ping` (Healthy/HealthyBusy from a 1 s probe of
  the gateway's own `/healthz`), `POST /invocations` (envelope → `127.0.0.1:18789`), supervises
  the boot chain, plus a `diagnostic` escape hatch used throughout bring-up.
- `boot-layout.sh` — builds the §5.5 layout and enforces the fence.
- `Dockerfile.gateway` `CMD` → `boot-layout.sh`.

**The fence is proven both ways.** Positive: `state/` resolves to overlay, boot proceeds.
Negative: forcing `OPENCLAW_HOME=/mnt/oc/fakehome` produces
`FATAL: … resolves to … on 'nfs4' — SQLite must not live on a network filesystem`, exit 1.
And the inverted trap is confirmed real — a lexically-local path resolving onto `nfs4` where
**SQLite reported `journal_mode=wal` and committed successfully**. SQLite does not defend
itself; the resolved-path (`readlink -f`) fence is the only protection.

**Two hazards found by building it, both now fixed and both worth keeping:**

1. **A supervisor must never outlive its child.** The first contract server kept answering
   `/ping` after the gateway died, and because AgentCore keeps microVMs warm and routes to
   them, that became a zombie serving nothing for 8+ minutes. Fixed: bounded restarts, then
   `exit(1)` so AgentCore discards the microVM. Any production contract server must do this.
2. **litestream needs bucket-level `s3:GetBucketLocation`**, which cannot carry an `s3:prefix`
   condition. Production already grants it (`bedrock_aip.py:113-114`), so the existing owner
   policy ports cleanly to an AgentCore execution role — but a prefix-only policy silently
   fails the restore.

**Config shape that boots:** `gateway.mode=local`, `bind=lan`, `auth.mode=token` — matching
`config.py:1758`. Note `agents.list` auto-migrates to `agents.entries` with a warning.

**The transport seam is proven end to end (2026-09-16 15:06Z).**
`InvokeAgentRuntime` → contract server `/invocations` → gateway `:18789` → `lightbulb-channel`:

| Probe | Result |
|---|---|
| `GET /healthz` | `200 {"ok":true,"status":"live"}` |
| `POST /plugins/lightbulb-channel/rpc` (no token) | `401` — route exists, auth enforced |
| same + `Authorization: Bearer <gateway_token>` | **`200`** with a real health payload |

That last one is the exact call `container_rpc.py:81` makes, answered from inside a microVM —
so the seam P5 replaces for all 23 `resolve_running_container` call sites works today.

**Free win:** the plugin's health RPC already returns
`eventLoop: {degraded, utilization, cpuCoreRatio}` — the busy-vs-dead signal the liveness
design needs, with no new code. `/ping` can consume it directly.

**Enablement detail:** the channel plugin is switched on by `channels.lightbulb.enabled` plus
per-agent `accounts` (`config.py:2500`), **not** `plugins.entries`. With it the gateway loads
14 plugins including `lightbulb`; without it, 13 and no ingress route. One non-fatal warning to
clean up: `plugin must declare contracts.tools before registering agent tools (plugin=lightbulb)`.

**Still open, deliberately not done:**
- **A real room turn** — needs the backend (AppSync publish, hook-token callbacks, model creds),
  i.e. P5. Transport is no longer the unknown; the product wiring is.
- **⚠️ Concurrent sessions on one EFS access point break the gateway SILENTLY.** The first
  session on a fresh runtime is reliable; later ones spawn a gateway that emits **zero output**
  and never becomes ready (ending in `RuntimeClientError`/503). I hypothesised an
  `openclaw.json` lock and **disproved it** — two local containers sharing a config dir both
  start, and EFS shows no lock artifacts. The difference is the mount: bind mount locally vs
  **nfs4** in the microVM, where locking semantics differ (this repo already mandates
  `fcntl.lockf`, never `flock`, on EFS). **Mechanism not isolated.** It points the same way as
  the two-writers finding: at most one session per workspace, enforced durably. Worth a focused
  spike in P5 — a silent hang with no logs is the worst possible production failure.
- **Runtime-created sibling files.** OpenClaw writes `openclaw.json.last-good`,
  `config-journal-fingerprint.key`, `cache/`, `media/`, `plugin-skills/`, `tmp/` next to the
  config. Only pre-existing EFS entries are symlinked, so these land on ephemeral disk and die
  with the session. `openclaw.json.last-good` (config rollback) looks load-bearing. Decide in
  P5 which siblings must persist; `boot-layout.sh` should also log strays.
- `/ws` fragmentation for 25 MB relay messages.

### P4 — AgentCore Browser (2 weeks)

P2 removed browsing; this restores it. Loopback CDP proxy in `lightbulb-channel`, backend holds
every AgentCore browser permission (the container holds none — otherwise an `exec: full` agent
could mount another owner's EFS via `filesystemConfigurations`), DCV live view replacing noVNC,
our own 10-minute idle stop because the browser TTL is absolute with no idle reset. Deletes the
browser-view router, its ALB pattern and the `bv-` path, plus Telegram and Discord.

### P5 — Cutover (2–3 weeks)

Backend `agentcore_runtime.py`: a runtime and role per owner, the transport seam over all 23
`resolve_running_container` call sites, callback-driven session state, the warmup invoke,
`StopRuntimeSession`, the 8 h rollover. **Sleep and wake must work before the first workspace
moves** — AgentCore has no always-on (idle 900 s default, `maxLifetime` 8 h hard), so every wake
door (room message, Paperclip routine, Slack, email, cron, pairing, hooks) ships here.

Per-workspace, one-way, as in §5.5: drain ECS → record replica TXIDs → copy to
`_precutover-<date>/` → delete the ECS service → start the AgentCore session → verify TXID,
`integrity_check`, `journal_mode`, and new LTX per DB. Carry-overs landing here: the replay
guard and the cache-ttl bloat fix.

**P5a status (2026-09-16 pm, PR open, not merged):** the backend half is built — the transport
seam (`runtime_transport.py`, owner-derived session id), `container_rpc` and room ingress
through it, MMDS credentials in `appsync.ts`, `ensure_runtime` + a `require_non_production`
debug trigger, and the CDK plumbing. **Two blockers found by reading, not by failing:** the
AppSync credential provider had no MMDS source (a microVM gateway's reply frames would never
reach the UI), and the backend's `PassRole` was pinned to `ecs-tasks` (`CreateAgentRuntime`
would have been denied). Fences: debug-route 403 in prod; `AGENTCORE_GATEWAY_IMAGE` empty in
prod, refused by `ensure_runtime`. PR #2688.

Two more found by reading before the flip: (a) every wake door — the work consumer,
`container_rpc`, agent mail, the status poll, chat wake, the trial-idle sweep — funnels through
`resolve_running_container` and the ECS start/stop chokepoints, and each read a moved workspace
as a *stopped ECS box*; the first message would have started the old task next to the microVM
(two writers). Closed at the chokepoints: the resolver reports a truthy sentinel address for an
agentcore row, start/stop no-op, `ensure_runtime` refuses unless the ECS row is stopped and marks
it `running`. (b) boto3's defaults would re-send a timed-out ingress POST and cut a >60s RPC off
under a cold start; the transport now sets no retries, `read_timeout = timeout_s + 60`, runs on its
own thread pool, and surfaces botocore failures as httpx errors. AWS docs confirm a
`runtimeSessionId` is reusable after idle/`maxLifetime` termination. Still unproven: a real turn.

### P6 — Credentials → Gateway + Identity (2–3 weeks, parallel)

Unchanged from §5.4, and independent of everything above — it can run at any point.

## 7. What changes for the product

| Change | Today | After | Phase |
|---|---|---|---|
| **Agents stop freezing.** "Forge finished · no reply" goes away | One box runs the loop and the builds | Builds run on a separate worker | P3 |
| **First message after ~20 min quiet waits for a wake.** Paid workspaces are always-on today | No wait (paid) | Target < 60 s, unmeasured until Q6. Opening the app warms the session, so a human typing mostly won't see it; cron/Slack/email-triggered turns will | P6 |
| **First shell command after ~30 min idle: 60–90 s.** Only build-running agents notice | Instant | Then warm | P3 |
| **Browser panel:** DCV live view, Start button, one viewer with "Take over", fixed 1280×800; opening the panel no longer launches Chrome | noVNC, multi-viewer, auto-launch | | P4 |
| **Telegram and Discord gone** from Settings → Channels | Wizard exists | Removed | P4 |
| **Claude Code / Codex delegation** becomes headless — you see the result, not a streamed child session | Streamed | Headless | P3 |
| **Stripe/Notion/Sentry/Linear/Attio/Intercom/PostHog/Apollo/Cloudflare stay connected** | Silent refresh death | One refresher | P5 |
| A single turn cannot outlive the session's 8 h lifetime (we roll at a quiet point) | No cap | 8 h | P6 |

**Unchanged:** rooms and messaging, Teams/Paperclip, mobile, desktop app and pairing, Slack,
agent email, marketplace, billing, the bedrock-gate and trial caps, every non-MCP
integration.

**For us:** four fewer things to operate; deploys become `UpdateAgentRuntime` per owner with
sessions rolled at idle; ECS Exec becomes `InvokeAgentRuntimeCommandShell`; cost moves from
"allocated 24/7" to "consumed while awake".

---

## 8. Cost

**Rates, us-east-1.** AgentCore Runtime/Browser $0.0895 per vCPU-h + $0.00945 per GB-h, per
second. **CPU is billed only while actually used** — "CPU charges don't apply during model or
tool I/O wait if no background process uses CPU" — while **memory bills continuously at the
session's peak for the session's life**. Fargate $0.04048 per vCPU-h + $0.004445 per GB-h on
allocation 24/7; Spot ≈30% of that. Gateway $0.005 per 1,000 invocations; Identity free through
Gateway/Runtime.

**The consequence that shapes the design: an awake-but-idle session costs only memory —
1.5 GB × $0.00945 ≈ $0.014/hour.** An extra hour of idle tolerance costs about a cent and a
half, which is why the idle timeout is hours, not minutes (§5.1).

**Per owner per month** (no worker tier — exec runs on the gateway)

| Line | Today | Target |
|---|---|---|
| Gateway, 4 h idle timeout → ~12 h/day awake, ~2 h/day of real work | — | **≈$5.20–8.40** |
| Gateway, awake 6 h/day | starter $1.84 Spot / $6.13 OD · founder $2.95 / $9.82 | **$5.04** |
| Gateway, never sleeps | starter $12.76 Spot / $42.53 OD · founder $25.52 / $85.06 | **≈$10–14** |
| Browser, 1 h/day | inside the box (1.8 GB of it, resident whether used or not) | **$2.71–5.02** |
| Gateway + Identity (P6) | — | **< $1** |
| **Typical owner** | **$12.76 Spot / $42.53 OD** | **≈$8–10** |
| **Founder-class** (2 vCPU / 8 GB today) | **$25.52 Spot / $85.06 OD** | **≈$8–10** |

**Fleet at the typical profile:** 8 owners $115 Spot today → **≈$70–80**; 100 owners $1,404
Spot → **≈$850–1,000**.

**Read it straight.** The headline is not a discount on like-for-like — it is that **AgentCore
gives a 2 vCPU / 8 GB machine for less than we currently pay for 1 vCPU / 4 GB, conditional on
workspaces sleeping.** Against Spot at 1 vCPU that is roughly a 25–35% saving; against
on-demand it is ~4×; for founder-class boxes it roughly halves. A workspace that never sleeps
lands near today's price with double the machine. No new fixed costs (the PrivateLink endpoint,
$14.60/mo per env, is optional — NAT works). **Engineering cost ≈8–10 weeks of one engineer** —
that is the real price.

**What cost does NOT buy:** cold start. Only 34 s of the ~2 min is AgentCore; the rest is
OpenClaw's own boot, and the fixes for it (hourly snapshots, the doctor marker, lazy MCP
connect) work on ECS too.

**Soft spots:** the 15% CPU duty was measured on one owner, one afternoon, on a saturating box
with Chrome resident — treat it as a floor, re-measured in P3. The ~12 h/day awake figure is an
assumption until sleep ships. Memory billing at session *peak* means one 3 GB build pins the
rate for the rest of that session, which is an argument for the 8 h rollover landing at a quiet
point.

## 9. Risks

Rewritten 2026-09-16: five of the original nine were settled by the spike.

| # | Risk | Mitigation |
|---|---|---|
| 1 | **The arm64 gateway image does not fit under 2,048 MB.** The 1,728 MB figure is an amd64 estimate with ~16% headroom; arm64 layer sizes differ | P2's acceptance test is `CreateAgentRuntime` succeeding — the only authority. If it fails, the next cuts are the exec toolchain (−413 MB) and then the fork's own `node_modules` |
| 2 | **Dropping ACP costs a capability.** Agents lose streamed Claude Code / OpenCode child sessions | `runtime="subagent"` still works, and headless `claude -p` / `codex exec` run through exec. ACP-from-EFS is the cheap restoration if it hurts |
| 3 | **A symlink or mount subtlety opens a DB in rollback and replication stops silently** — and the trap is the inverse of what was predicted: SQLite reports WAL on the EFS mount, so the danger is a local path symlinked *into* `/mnt/oc` | Classify the **resolved** path (`readlink -f`), not the lexical prefix; post-start `journal_mode` assert on every DB; replica-lag alarm |
| 4 | **Exec still starves the gateway on 2 vCPU / 8 GB** | Measure under parallel builds in P3. The worker design is intact and its transport is validated (§5.2) |
| 5 | **A wake door is missed after cutover** (cron, Slack auto-disable, SES giving up after ~3 min) | Every door tested in P5's dev gate before any workspace moves; `next_wake_at`; the veto list |
| 6 | **Two writers on one replica prefix — REPRODUCED 2026-09-16, and it fails SILENTLY.** Issuing different `runtimeSessionId`s for one owner gives you concurrent microVMs, each running litestream against the same S3 prefix. Observed divergent histories in one prefix (TXID 97 written *after* TXID 157); object count 43→131; every later restore then hung at `restore state/openclaw.sqlite` with **no error surfaced**, ending in `RuntimeClientError` | The backend must **serialize sessions per workspace with an at-most-one lock**, not an optimistic check. AgentCore will hand out concurrent microVMs per session id without complaint. This is the highest-consequence finding of the P3 work: the failure produces no error in the replica, so it would be discovered at a restore — i.e. during an incident |
| 7 | **The fork's arm64 release never gets fixed** and local builds become the de facto release path | P1 fixes the workflow. Local `buildx` unblocks the proof only |
| 8 | **Gateway/Identity vendor registration fails for some of the nine** | P6 is parallel and per-vendor; a vendor that cannot register stays on the in-container rail |

**Settled by the spike, no longer risks:** the cap being on uncompressed size; the shell API
being unable to hold background jobs; Cloud Map/private-ALB reachability from a VPC microVM;
whether EFS mounts at all.

## 10. Decisions

**Decided (2026-09-15):** keep OpenClaw · gateway on Runtime · AgentCore Browser ·
Gateway+Identity for the nine · delete Telegram (incl. the live bot) and Discord · Lobster
unused · no ECS fallback · dev first, fix forward.

**Decided (2026-09-16), after the image cap was measured as a hard 2,048 MB:**

| Ruling | Why |
|---|---|
| **ACP comes out of the gateway image** (acpx, opencode, claude-code = 949 MB) | It is the only single block large enough to get under the cap. Browser-only (2,677 MB) and browser+exec (2,264 MB) both still exceed it. Owner accepted dropping it after seeing the measurement |
| **exec STAYS on the gateway** | Cutting it does not get the image under the cap anyway, so it buys nothing at this stage |
| **No worker tier for now** | It existed to host exec. With exec staying, it comes off the critical path. Revisit for the credential fence (`exec: full` can read everything in-process and on EFS) and for reliability, after measuring whether 2 vCPU / 8 GB actually starves |
| **One runtime per workspace** | Forced regardless — `filesystemConfigurations` are per-runtime |
| ACP-from-EFS (install the harnesses onto the mounted workspace instead of baking them) | Considered, **not pursued now**. It would keep the capability at the cost of a first-use install; revisit if losing streamed Claude Code / OpenCode child sessions hurts |

**Sleep, decided 2026-09-16:** idle timeout is **4 hours**, not the 20 minutes carried over
from the ECS design. Idle memory is ~$0.014/h, so a generous timeout costs a couple of dollars
a month and removes essentially every cold start inside a working day. Owner ruled that **Slack
and email latency does not matter**, so there is **no per-workspace `always_on` escape hatch** —
every workspace sleeps. Overnight and weekend first-messages pay ~2 min; cron pays it unwatched;
the app's warmup invoke covers the human path.

**Must fix in P5:** the current trial idle clock counts **only human sends**, so a box can park
right after replying. Idle must mean no runs, no relays, no shell sessions and no recent
activity — not "no human typed" — or every message after a reply pays a cold start.

**Resulting shape: one runtime per workspace · browser and ACP out of the image (≈1,728 MB,
amd64 estimate, ~16% headroom) · exec on the gateway · no worker · sleep at 4 h idle ·
AgentCore Browser restores browsing.**

**Open**

| # | Decision | Recommendation |
|---|---|---|
| D2 | Worker transport if Q3 fails | Fargate on-demand worker over ssh — pre-accepted; the one ECS residue |
| D3 | ACP delegation under the sandbox | Drop it; headless `claude -p` / `codex exec` via exec |
| D5 | Trial workspaces | Same path, cut over last |
| D6 | Single-viewer browser live view | Accept, with "Take over" |
| **D7 — blocks P2** | Create the dev spike resources (two runtimes, a custom browser, a gateway) | Approve |

---

## 11. Sources

Research reports in the session scratchpad `97c94ff1-…/scratchpad/`:
`agentcore/{runtime-capabilities,browser-capabilities,current-system-inventory,gateway-hosting-constraints,cost-model,cloud-workers-crabbox,DESIGN-EVALUATION,REVIEW}.md` ·
`agentcore-design/{TARGET-DESIGN,worker-agentcore-vs-fargate,sandbox-seam-mechanics,gateway-min-size-and-sleep,browser-attach-and-live-view,browser-vendors,exec-sandbox-vendors}.md` ·
`hermes/{hermes-architecture,agentcore-hosting-options,openclaw-coupling-inventory,slim-openclaw-on-agentcore}.md` ·
`managed/{agentcore-managed-services,tool-integrations-today,agentcore-docs-sweep,state-and-litestream}.md`.
AWS: the AgentCore developer guide (100+ pages swept 2026-09-15) and the pricing page.
Incident evidence: memory `founder-container-freeze-kills-forge-turns-2026-09-14`.
