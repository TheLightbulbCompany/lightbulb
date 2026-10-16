# How our Hermes agents actually run on AgentCore (execution runtime)

Date 2026-10-15. Read-only research: code and infra only, no AWS calls.

Code state: `origin/main` at `521ea1cc7` (2026-10-15 21:18). The main checkout is still at `1553085fd` (10-08), so every line number below is from `origin/main`. Paperclip lines are from the local fork `~/Desktop/paperclip-fork.nosync` at `c6f5a4d2e` (09-27). That fork is outside this repo and may differ from the deployed image.

This report goes deeper than `docs/research/2026-10-15-hermes-capabilities-for-execution.md`. It does not repeat its toolset and connector lists. It corrects two points in it (see section 8).

## 1. Topology

### One runtime, one session, one microVM per company

- One AgentCore Runtime per workspace (owner). `apps/backend/core/services/agentcore_runtime.py:1-5`, name from `runtime_name_for`, `:48-60`.
- One AgentCore session id per owner, stable for the life of the workspace. Every backend call lands on the same microVM. `apps/backend/core/services/runtime_transport.py:83-94` (`session_id_for`).
- So all agents of one company (Friday, Atlas, Forge, ...) run in ONE microVM, in ONE bridge process.
- No EFS. Each agent's `$HERMES_HOME` profile lives in S3 and is copied to local disk. `apps/backend/core/services/hermes_provisioning.py:29` and `:189-192`.
- Network: VPC mode, our subnets and security group. `hermes_provisioning.py:197-200`.

### What is shared and what is per agent

| Thing | Scope | Evidence |
|---|---|---|
| microVM, CPU, RAM | Company | one session id, `runtime_transport.py:83-94` |
| Local disk (~4-5 GB) | Company, all agents | `bridge/turn.py:2785-2790` ("shared with every teammate"), `bridge/recycle.py:29` (~4.1 GB free at idle) |
| IAM execution role | Company | per-owner Bedrock role re-trusted, `agentcore_runtime.py:7-10` |
| Boot secret (hook token, gate token, Bedrock bearer) | Company | `agentcore_runtime.py:153-191` |
| Profile dir `/opt/data/profiles/<agent>` | Agent | `bridge/sync.py:449-450` |
| Profile S3 prefix | Agent under `<owner>/hermes` | `hermes_provisioning.py:139-143`, `bridge/sync.py` |
| Connector secrets (`.env`) | Agent profile; resolved per turn by a secret scope | `bridge/turn.py:2694`, `bridge/__init__.py:79-90` |
| Hermes transcript (`state.db`) | Agent | `bridge/sync.py:691-745` (SQLite backup per profile) |
| Working folder | Agent + conversation: `<profile>/work/<conversation>` | `bridge/turn.py:2794-2799` |
| AgentCore Memory | Company (team scope) or company+member (private DM scope) | `apps/backend/core/services/workspace_memory.py:1-12`; `apps/infra/hermes/plugins/memory/agentcore/__init__.py:158` |
| Browser | Company: one AgentCore Browser profile, one live session | `apps/backend/core/services/browser_sessions.py:106-121`, `browser_control.py:1-25` |
| Website-login vault | Company | `apps/backend/core/services/website_logins.py:1-8` |
| Email address | Company: `agents@<slug>` | `apps/backend/core/services/agent_email.py:1-12` |

Isolation between agents is by process scope, not by sandbox. The shell can read the role's credentials over the metadata service (`agentcore_runtime.py:134-136`). Any agent's shell can read the shared disk.

### Diagram

```
Company (owner) ─────────────────────────────────────────────────────────────
  AgentCore Runtime  lb_<env>_<owner>_<hash>   (1 per company)
   └─ session lb-<owner>-<hash>                (1 per company, ≤8 h)
       └─ microVM (VPC, ~4-5 GB disk, shared)
           └─ bridge process (server.py :8080)
               ├─ turn slots: 4 per microVM (LIGHTBULB_MAX_CONCURRENT_TURNS)
               ├─ profile Friday  /opt/data/profiles/<friday>
               │    ├─ work/<room-A>/   lock (Friday, room-A)
               │    ├─ work/<issue-7>/  lock (Friday, issue-7)
               │    └─ state.db  memories/  skills/  .env
               ├─ profile Atlas   /opt/data/profiles/<atlas>   (same shape)
               └─ cdp_mux ──► AgentCore Browser session (1 per company, own 8 h TTL)
  S3  <bucket>/<owner>/hermes/<agent>/...   (profiles, saved at turn end + SIGTERM)
  AgentCore Memory /owners/<owner>/  and  /owners/<owner>:<user>/
  Browser saved state (cookies + localStorage) in S3
  Paperclip (company board) ──hermes_gateway adapter──► backend facade ──► same session
```

### Parallel turns

- Two agents of one company: YES, in parallel, in the same microVM. Up to 4 turns at once in the microVM, whatever the agent. `bridge/turn.py:2889-2890`. The owner row can override it (`max_concurrent_turns`, `hermes_provisioning.py:920`, `:1503`). Off-path compactions take a slot too, at most 3 at once (`turn.py:2893`).
- One agent, two different conversations (two rooms, a room and an issue): YES, in parallel. The lock is per (agent, conversation), not per agent. Each gets its own shell and folder. `turn.py:2880-2887`, `:2903-2907`.
- One agent, same conversation: NO. Turns serialize on the conversation lock. `turn.py:2956-2994`.
  - A new message while a room turn runs is steered into the running turn (Hermes `steer`, queued after the current tool batch). `turn.py:936-960`. A Paperclip turn never takes steers (`turn.py:949`).
  - A typed turn that waits 30 s or more behind a running job runs beside it in a side session. `turn.py:2898-2900`, `:3666-3684`.
- Hermes's own per-profile cap: `max_concurrent_sessions: 3`. `apps/backend/core/services/hermes_profiles.py:360`.
- Paperclip: the fork's default `maxConcurrentRuns` per agent is 20 (`packages/shared/src/constants.ts:77`). Wakes for an issue that already has a live run are parked as `deferred_issue_execution` and coalesced (`coalescedCount + 1`), then run after it (`server/src/services/heartbeat.ts:15460-15480`, `:26639`). So a second wake during a run gives one more run, not one per wake. This matches the memory note. The bridge's own room queue is SQS FIFO, grouped by room + agent (`apps/backend/core/repositories/messaging_repo.py:272-274`).
- When the recycle hold is on, new turns get 503 and are retried by the room queue; Paperclip reads it as transient. `turn.py:63-73`.

Memory note to update: "per-agent lock = +1 turn" (09-22) is out of date. The lock is now per conversation.

## 2. Limits

| Limit | Value | Where set |
|---|---|---|
| Hermes run budget (one turn) | 1800 s (30 min) | `hermes_profiles.py:363` (`agent.run_budget_seconds`), `stall_guards: true` `:364` |
| Hermes inactivity watchdog | off (0) | `apps/infra/hermes/managed/config.yaml:52-58` |
| Paperclip run wall clock | 3600 s | `apps/backend/core/services/paperclip_adapter_config.py:22-25`. The 30-min Hermes budget fires first. |
| Paperclip poll interval | 10 s | `paperclip_adapter_config.py:34` |
| Room run heartbeat timeout | 90 s; never started 300 s | `messaging_repo.py:76-81`; sweeper every 60 s `apps/backend/core/services/run_ledger.py:24` |
| Session idle timeout | 30 min | `agentcore_runtime.py:113-117`, sent in `hermes_provisioning.py:202-205` |
| Session max lifetime (AWS) | 8 h | `agentcore_runtime.py:118-119` |
| Our recycle | at 7 h when idle; forced at 7 h 45 min | `bridge/recycle.py:27-28` |
| Low-disk recycle | soft 1000 MB (wait for idle), hard 500 MB (stop even busy) | `bridge/recycle.py:30-31`, check every 30 s `:32` |
| Disk | ~4 GB told to agents; ~4.1 GB free at idle on dev | `bridge/turn.py:2785`; `bridge/recycle.py:29`. Size is AWS's, not set by us. |
| CPU / RAM | not found in repo (AgentCore default) | — |
| SIGTERM save window | 10 s (AWS gives 15 s) | `bridge/server.py:778-802` |
| Max output tokens (managed) | 32768, larger asks are clamped; default when unset | `apps/infra/lib/stacks/openrouter-gate-stack.ts:82-85`, `apps/infra/lambda/openrouter-gate/core.mjs:89-95`, `:109` |
| Max output tokens (BYO, ChatGPT) | not found in repo (Hermes/provider default) | — |
| Request body | 5 MiB (all); paid mode also bytes ≤ `max_prompt_tokens` (400000 per config comment) | `openrouter-gate-stack.ts:80`; `core.mjs:157-170`; `managed/config.yaml:60-64` |
| One model call | 900 s (gate Lambda timeout) | `openrouter-gate-stack.ts:65` |
| Gate concurrency, fleet-wide | 25 Lambda executions for all companies | `openrouter-gate-stack.ts:66` |
| Context window pin (managed) | 200000 | `hermes_profiles.py:247` |
| Compaction threshold | 60000 tokens, summary at `reasoning_effort: low` | `managed/config.yaml:39-51`, `:59-65` |
| File memory | MEMORY.md 2200 chars; USER.md limit `USER_CHAR_LIMIT` | `hermes_profiles.py:377-384` |
| Session db retention | 90 days, auto prune | `hermes_profiles.py:392-397` |

### Model per tier

| Path | Agents | CEO (Friday) | Aux (compaction etc.) | Where |
|---|---|---|---|---|
| `openrouter_managed` (trial, Base, managed paid) | `OPENROUTER_MANAGED_MODEL_ID` | same | same model | `hermes_profiles.py:234-252`. The value is a GitHub variable (`.github/workflows/deploy.yml:33-34`), not in the repo. Memory says DeepSeek (`deepseek-v4-flash`); not verifiable here. Allowed models are in `OPENROUTER_MODEL_POLICY_JSON_*`, also a variable. |
| `byo_key` Anthropic | `claude-sonnet-5` | `claude-opus-5` | `claude-haiku-4-5` | `hermes_profiles.py:74`, `:99`, `:208` |
| `byo_key` OpenAI | `gpt-5.4` | `gpt-5.6-sol` | cheap ref | `hermes_profiles.py:76`, `:208` |
| `chatgpt_oauth` | `gpt-5.6-sol` | same | same | `hermes_profiles.py:271-280` |

Free models (zero price in the policy) allow provider training (`data_collection: allow`); priced models deny it. `core.mjs:99-108`.

### What is lost at recycle and what survives

The microVM disk is gone at every session end: idle 30 min, the 7 h recycle, a disk recycle, an image roll, or a crash.

| Survives | How |
|---|---|
| Profile files the turn changed | Uploaded to S3 at turn end (`bridge/sync.py:691`) and on SIGTERM within 10 s (`bridge/server.py:784-802`). Restored on the next cold start (`sync.py:635`). |
| Hermes transcripts (`state.db`) | SQLite backup into S3 with the profile (`sync.py:742-745`). Turn leases are cleared on restore (`sync.py:531-552`). |
| `work/<conversation>/` source trees | Saved; restored on that conversation's own next turn (`sync.py:346-353`). |
| AgentCore Memory records | Never expire; events expire in 7 days (`apps/infra/lib/stacks/container-stack.ts:226-239`). |
| Paperclip issues, comments, documents | In Paperclip's database. |
| Browser sign-ins | Separate browser session; cookies + localStorage snapshot in S3 (`browser_control.py:736-746`). |
| Mail | Inbox rows, 30-day TTL (`apps/backend/core/repositories/agent_mail_inbox_repo.py:32`). |

| Lost | Evidence |
|---|---|
| Background shell processes (`process` tool, `cmd &`) | Nothing saves them. They do NOT keep the session busy: `/ping` reports busy only for turns and delegations (`bridge/server.py:772-775`). So the session can idle out at 30 min while a background job still runs. |
| Live async delegations | In-memory only (`bridge/turn.py:38-42`). The recycle waits for them, but the idle timeout and a forced stop do not. |
| A running turn's file writes | Pushed only at turn end; a roll mid-turn "destroys every filesystem write that turn has made" (`hermes_provisioning.py:875-877`). The SIGTERM save covers part of this, inside 10 s. |
| `.scratch/` (worktrees), `node_modules`, venvs, `dist`, `build`, caches, `/tmp`, files outside the profile | Never uploaded (`sync.py:148`, `:169-175`, `:222`). The skill tells agents this (`skills-src/lightbulb/references/platform-primitives.md:16`). |
| Deletes | Not propagated; a deleted file comes back on cold restore (`sync.py:12-13`). |
| Paperclip run in flight | The poll gets 404, then `failed` / `runtime_lost` (`apps/backend/routers/messaging.py:1679-1697`). |

## 3. Long jobs

A job longer than one 30-minute turn is a chain of separate runs. Nothing inside the runtime continues it.

### What carries over to the next run

- The Paperclip issue: status, comments, documents, sub-issues. The adapter uses `sessionKeyStrategy: "issue"` (`paperclip_adapter_config.py:58`), so each issue gets ONE Hermes session per agent. The next run on the same issue replays that issue's transcript from `state.db` (subject to 60k compaction).
- An issue created from a room is bound to that room's conversation. Its wake runs in the room's session with the header "Paperclip woke you for issue X ... record progress on the issue itself" (`bridge/turn.py:3935-3941`, `:3820-3833`). Nothing it writes is posted to the room.
- Files under the agent's profile and `work/<issue>/`.
- Memory: MEMORY.md/USER.md in the prompt; up to 5 AgentCore records per turn (`plugins/memory/agentcore/__init__.py:44-46`, `:140`).
- Wake context env: `PAPERCLIP_TASK_ID`, `PAPERCLIP_WAKE_COMMENT_ID`, `PAPERCLIP_WAKE_PAYLOAD_JSON` (`skills-src/paperclip/SKILL.md:17`).

### What re-starts it

- An assigned issue not in `backlog` is re-dispatched about every 30 s until `blocked` (`skills-src/paperclip/SKILL.md:27`). This is the de facto continuation loop.
- Paperclip retries: transient failure twice at 30 s (`heartbeat.ts:759-768`); max-turns continuation default 2, cap 10 (`heartbeat.ts:842-846`).
- Monitors (one-shot) and watchdogs (`skills-src/paperclip/SKILL.md:81`).
- Routines: `concurrencyPolicy: coalesce_if_active`, `catchUpPolicy: skip_missed` (`apps/backend/core/services/agent_routine_provisioning.py:14`, `:41`). A missed slot is skipped.
- Heartbeat timer with the agent's own `HEARTBEAT.md` (`skills-src/paperclip/SKILL.md:40`).

### What the agent is told at the next wake

- The Paperclip-rendered prompt (`request['instructions']` and `request['input']`), plus the clone rule and the profile's AGENTS.md (`turn.py:3818-3820`). The prompt text is built in Paperclip, not in this repo.
- Nothing tells it "your last run hit the 30-minute budget" or "the microVM restarted". Not found in repo.

### Delegation, background work, cron

| Feature | State | Limits |
|---|---|---|
| `delegate_task` (sync and async) | On | Async results come back as one follow-up room turn (`bridge/followups.py:1-11`). Follow-ups capped at 3 per hour, 32 KB text (`messaging.py:867-868`). Hermes delegation depth/children: not set in repo (Hermes defaults). |
| Subagent progress | Relayed to the room live channel | `bridge/subagents.py:1-25` |
| Background shell (`process`) | On | Dies with the microVM; does not hold the session busy (`server.py:772-775`). |
| Hermes `cronjob` | Off | `hermes_profiles.py:168`, `cron.allow_agent_scheduling: false` `:373` |

### What nothing tracks

- No platform goal, progress ledger or checkpoint for a job. Progress lives only in what the agent chose to write on the issue.
- No signal that a turn ended on the 30-minute budget versus finishing.
- Background processes and delegations killed by idle-out or a forced stop.
- A file the turn wrote under `.scratch/` or `/tmp`.

## 4. The browser

| Question | Answer | Evidence |
|---|---|---|
| Provisioned per | Company: one AgentCore Browser profile, created at provision or roll | `browser_sessions.py:106-121`, `hermes_provisioning.py:858-865` |
| Live session | One shared session per company. Each agent run gets its own tab. | `browser_control.py:1-25`; `skills-src/lightbulb/references/surfaces.md:17` |
| Session lifetime | Starts at AWS's 8 h max; our sweep saves and stops it after 30 min with no agent, input or watcher | `browser_control.py:63-67` |
| Linked to the microVM? | No. It has its own life. A microVM recycle does not end it. | `browser_control.py:12-19` |
| Agent runs at once | One automation (CDP) connection; one run holds a 300 s lease renewed per tool call; other runs wait up to 10 s, then 409 | `browser_control.py:52-61`; the bridge `cdp_mux` multiplexes runs onto it (`bridge/__init__.py:35-43`) |
| Live view | AWS streams to ONE viewer (URL TTL 300 s). Others get screencast frames through the bridge. | `browser_sessions.py:34-35`; `browser_control.py:21-24` |
| Who signs in | A person in the live view, or the agent via the vault | `website_logins.py` (bridge) `:1-23` |
| Saved state | Cookies + localStorage snapshot in S3. IndexedDB, sessionStorage, service workers are NOT saved. | `browser_control.py:736-746` |
| Sign-in survives recycle | Yes if the site keeps it in cookies/localStorage; restored at the next browser start | `browser_control.py:12-19` |
| Vault: what | Passwords in Secrets Manager `lightbulb/<env>/integrations/<owner>/website-login/<id>`; metadata in the integrations table | `apps/backend/core/services/website_logins.py:10-17` |
| Vault: how long | No expiry found. Kept until removed or account wipe. Requests expire in 24 h; Activity rows 30 days. | `website_logins.py:50-54` |
| Vault: model sees | Opaque handles only; one password per fill, in memory | bridge `website_logins.py:3-12` |
| Login ask | `browser_vault_save_login` files a Needs-you form; the bridge waits up to 390 s | bridge `website_logins.py:44`, `:184-219` |
| Codes / CAPTCHA | A person types it into the live view. The bridge posts one line and waits up to 390 s for the page to move. A Paperclip or voice turn has no room, so it does not wait at all (`no_room`). | bridge `website_logins.py:289-330` |

## 5. Email

- One address per company: `agents@<slug>`. Every agent sends from it under its own name. Inbound goes to the CEO unless it answers a thread another agent wrote. `agent_email.py:1-12`.
- Send rules (`agent_mail_send.py:1-24`): owner always; reply to stored inbound; any `to` only when `send_to_anyone` is on (default on).
- Send cap: 50 per company per UTC day, durable, shared by all agents and all send kinds. `agent_mail_owner_send.py:54`, `agent_mail_send.py:75-77`.
- Our outbound mail carries `Auto-Submitted: auto-generated`. `agent_mail_reply.py:46`.
- Inbound wake caps for strangers: 10 per sender and 50 per company per day; over the cap the mail is stored but does not wake. `agent_mail_inbound.py:20-21`, `routers/ses_inbound.py:377-390`.
- Loop guard: any mail with `Auto-Submitted` not `no`, or `Precedence: bulk|list|junk|auto_reply`, is DROPPED before it is stored. `agent_mail_inbound.py:59-81`, `routers/ses_inbound.py:289-292` (the store is at `:310`). Many sign-up and verification emails carry these headers, so such a code never reaches the inbox.
- Codes by email: the agent reads `lightbulb-mail.sh inbox` (`skills-src/lightbulb/scripts/lightbulb-mail.sh`). There is no push of a code into a waiting turn; a stored mail may wake the CEO as a new turn.
- Retention: 30 days. `agent_mail_inbox_repo.py:32`.

## 6. Cost and quotas

| Control | Value | Evidence |
|---|---|---|
| Trial window | 30 days from first ready | `apps/backend/core/services/trial_cap.py:38-50` |
| Trial daily key limit | $0.25/day (OpenRouter per-key, resets daily) | `apps/backend/core/config.py:239`, `openrouter_keys.py:66-76` |
| Base / paid-without-provider | $0.25/day | `config.py:241` |
| Managed paid backstop | $50/day key limit; real valve is a Metronome reservation per request | `config.py:243`; `core.mjs:166-180` |
| Cap hit (capped plans) | OpenRouter 403 "Key limit exceeded" becomes 429 `trial_daily_cap`; the day is stamped; every later call that day is refused at once | `core.mjs:13-27`, `:193-237`, `:155` |
| Empty OpenRouter account | Told apart from a spent key by reading the key's `limit_remaining` | `core.mjs:13-27` |
| Paid out of credit | 429 "Credit balance exhausted" | `core.mjs:180` |
| Expired trial | Gate refuses (403); waking doors refuse; Paperclip automation paused | `core.mjs:144-153`, `trial_cap.py:1-16`, `paperclip_repo.py:226` |
| Per-turn cost cap | None found beyond 32k output and the 30-min budget | — |
| Title generation | Off to save the daily cap | `managed/config.yaml:39-44` |

A multi-day job on a trial or Base plan stops each day the $0.25 is spent and resumes after midnight UTC, if a wake re-starts it.

## 7. Observability

### What the owner sees

- Room turns: a live run card with progress lines (`turn.py:1579`), the `run_active` frame once at turn start, and `ActiveRun` for a client that opens mid-turn (`messaging.py:340-350`).
- A failed or swept room run leaves one durable note in the room (`messaging.py:4348-4378`; sweeper `run_ledger.py:27-63`).
- Subagent progress in the room (`bridge/subagents.py`).
- Paperclip runs: only on the Teams board / issue run log. Nothing is posted to a room (`turn.py:3770-3776`).
- Agent-chosen posts: feed (`lightbulb-post.sh --feed`), issue comments, Needs-you approvals.

### What we see

- Bridge logs in the AgentCore runtime log group; turn-slot EMF metrics with memory, load and wait (`turn.py:2918-2953`).
- Gate request and cap-hit EMF (`core.mjs:186`, `:237`).
- PostHog `agent_run_completed` per run (`apps/backend/core/services/run_analytics.py:1-25`).
- Alarms: W67 more than 20 failed Paperclip runs in 15 min (`observability-stack.ts:1436-1460`); P29 free-tier first-turn failures (`:740-754`).

### What is silent

- Background process death and idle-out of a session with a background job.
- Delegations lost when the session stops.
- Recycles (time or disk): a log line only (`recycle.py:75-79`, `routers/messaging.py:1436`).
- A turn that ended on the 30-min run budget: no distinct signal found.
- Mail dropped by the auto-submitted guard: a status code in the response only; it is not in the metric list (`ses_inbound.py:110`).
- A code wait in a Paperclip run (`no_room`): the agent is told, nobody else.
- A single failed autonomous run (below the W67 storm line).
- Memory writes that are not yet recallable (about 2 min, per the prior report).

## 8. Corrections to the prior report

- The 403 "Key limit exceeded" IS now handled by the gate (`core.mjs:13-27`, `:193-237`). The prior report said it was not.
- The per-agent lock is gone; the lock is per agent + conversation (`turn.py:2880-2887`).

## Not found in repo

- MicroVM CPU, RAM and disk size settings.
- The managed model id and allowed-model policy (GitHub variables).
- Hermes delegation depth and child limits; Hermes default iteration cap.
- Output-token ceiling for BYO and ChatGPT paths.
- Paperclip's rendered wake prompt (in the Paperclip fork, not here).
- Any job-level progress ledger or "budget exhausted" notice.
