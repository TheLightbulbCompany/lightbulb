# Outside research: running long business jobs in production (2026-10-15)

Scope: gaps left by `2026-10-15-how-execution-agents-are-built.md` and `2026-10-15-long-horizon-agent-engineering.md`. Those reports are not repeated here.
Tags: [H] official/primary page read this session. [M] primary seen via search snippet, or several consistent secondary sources. [L] single secondary source. "Not found" means I looked and did not find it.
Tool note: some WebFetch pages are summarised by a small model. Where a page came back empty, I say so and did not fill the gap from memory.

## 1. Paperclip upstream

### Intended model
- Agents wake for assigned work, follow-up messages, or schedules. A database wakeup queue coalesces requests and runs budget checks first. [H] https://github.com/paperclipai/paperclip
- Assignment wakes the assignee. An @-mention alone does not assign work or wake anyone. [H] https://github.com/paperclipai/paperclip and https://raw.githubusercontent.com/paperclipai/paperclip/master/doc/SPEC.md
- Wake sources: `timer`, `assignment`, `on_demand`, `automation`. If a run is already active, new wakes merge instead of starting a duplicate run. [H] https://github.com/paperclipai/paperclip/blob/master/docs/agents-runtime.md
- Heartbeat config: `intervalSec` (0 turns the timer off), `wakeOnAssignment`, `wakeOnOnDemand`, `wakeOnAutomation`. Resumable adapters reuse the saved session ID on the next wake. Reset the session when context goes stale or the agent loops. [H] same agents-runtime.md
- Per-run timeout `timeoutSec`: 0 means the target default. That is no timeout on local/SSH targets and a 4-hour backstop on sandbox targets. [H] same page.

### Concurrency
- One owner per task. Claim is an atomic checkout to `in_progress`. A second claimant gets 409 and must never retry it. [H] https://github.com/paperclipai/paperclip/blob/master/docs/guides/agent-developer/heartbeat-protocol.md
- Per-agent concurrency: the SPEC doc is silent. A search summary of the heartbeat guide says "maxConcurrentRuns: 1 (V1 fixed)". A PR says the compiled default is 20 when the key is absent. An older issue says default is unlimited. Treat the default as unclear and set it explicitly. [M] https://github.com/paperclipai/paperclip/pull/13747 and https://github.com/paperclipai/paperclip/issues/6955
- Routines: `concurrencyPolicy` default `coalesce_if_active` (new trigger links to the live execution issue). `catchUpPolicy` default `skip_missed`. Triggers: `schedule` (cron + timezone), `webhook`, `api`. Each routine execution creates a tracked issue and wakes the owner. [H] https://docs.paperclip.ing/reference/api/routines

### CEO loop
- First heartbeat: review initiatives, propose a breakdown (org, sub-tasks, hires). Board must approve before execution. After approval the CEO creates tasks, proposes hires, delegates. [H] SPEC.md above.
- Hierarchy: Initiative, Project, Milestone, Issue, Sub-issue. Delegation uses `parentId` and `goalId`. [H] SPEC.md.

### How a long job is tracked across many runs (the answer for us)
Upstream says, in the heartbeat protocol [H]:
- Leave a clear next action in durable issue context before exiting.
- Use child issues for long or parallel work. The parent assignee is woken when all children are `done` or `cancelled` (`issue_children_completed`). Do not poll.
- Run liveness is separate from issue status. Only `plan_only` and `empty_response` outcomes trigger bounded continuation wakes (`continuationAttempt`). When continuations run out, Paperclip leaves an audit comment for a human or manager.
- Workspace setup alone is not progress. Progress means tool/action events, comments, document or work-product revisions, commits, or tests.
- Continuations never mark an issue `blocked` or `done`.
- Source: https://github.com/paperclipai/paperclip/blob/master/docs/guides/agent-developer/heartbeat-protocol.md

From the upstream agent skill [H] https://github.com/paperclipai/paperclip/blob/master/skills/paperclip/SKILL.md:
- A run does not keep watching after it exits. Only a persisted issue monitor (`executionPolicy.monitor.nextCheckAt`) wakes an issue on its own. Monitors are timer polling, not events.
- Keep `in_progress` only with an active run, a queued continuation, or a real scheduled monitor. "Successful artifact work left in in_progress with no live wake path is invalid."
- Waiting on a human: save a pending interaction (`request_confirmation`, `ask_user_questions`) and set `in_review`. Waiting on another issue: `blocked` with `blockedByIssueIds`, not free text.
- Writes must be verified. An empty PATCH response means the write failed.

### Budgets
- Company, agent, project levels. Soft alert (example 80%) and hard stop that auto-pauses the agent. Enforcement uses recorded spend, so in-flight work can overshoot. [H] https://github.com/paperclipai/paperclip and SPEC.md. Budget period and cascade are listed as undecided in SPEC.md.

### Definition of done
- No checklist feature upstream. The skill defines done as "requested work is complete, verification is recorded, and no follow-up remains." The nearest thing is a final-disposition checklist agents apply before ending a heartbeat. [H] SKILL.md above.
- Execution policy gives real gates. Every issue-bound run must post a comment (one re-wake if not). Optional review stage and approval stage per issue. When the executor sets `done`, the runtime routes it to `in_review` and the first reviewer. Reviewer excludes the executor. Change-request loop capped at 3 rounds by default (`maxReviewRounds` 1-50), then it escalates to a human. [H] https://docs.paperclip.ing/guides/power/execution-policy

### Failure modes reported by upstream users (open issues, may have moved since)
- Timer heartbeat longer than interval queues unlimited runs. One report: 235 queued runs, load about 14x. Still failed on 2026.626.0 with `maxConcurrentRuns: 1`. Open. [M] https://github.com/paperclipai/paperclip/issues/6955
- Idle `codex_local` run held the only slot for 574-681 minutes (`timeoutSec: 0`). Fix: default `timeoutSec` 3600 for new/patched agents. Existing agents not migrated. [H] https://github.com/paperclipai/paperclip/issues/1749
- Wake coalescing race: simultaneous wakes start concurrent runs. [M] https://github.com/paperclipai/paperclip/issues/10598
- Stale-lock sweeper releases the lock on a live run, so two sessions work one issue. [M] https://github.com/paperclipai/paperclip/issues/12276
- False success: agent says it is blocked, task still ends green. Request for `blocked`/`needs_human`/`needs_verification` states. [M] https://github.com/paperclipai/paperclip/issues/1997
- Completion comment attributed to the user re-triggers the assignee: status drift, duplicate comments, loops. [M] https://github.com/paperclipai/paperclip/issues/3817
- Auto-created recovery issue deadlocks against the original issue; users mark done by hand. [M] https://github.com/paperclipai/paperclip/issues/5947
- Recovery runs repeat work; runs succeed while the action is undone. [M] https://github.com/paperclipai/paperclip/discussions/13396
- Wakes stuck as `deferred_issue_execution` after a legacy run ends (#16519). Zero-turn Hermes runs recorded as succeeded (#16443). Titles only, not opened. [L] https://github.com/paperclipai/paperclip/issues
- No in-run loop detection. Budget stop only acts after spend. [M] https://github.com/paperclipai/paperclip/issues/390

## 2. Hermes-agent upstream

- Subagents: `delegate_task`. Default 10 concurrent children per batch (`delegation.max_concurrent_children`). Flat by default (`max_spawn_depth` 1); orchestrator children only if depth is raised. Children start with a fresh context, only `goal` + `context`, and return only a final summary. Child `max_iterations` default 250. Default no wall-clock timeout; optional inactivity cap; frozen children abandoned after 450 s idle or 1200 s inside a tool. Leaf children cannot use `clarify`, `memory`, `send_message`, `cronjob`. Optional `worktree_isolation` and `output_schema`. [H] https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation
- Cron: gateway ticks every 60 s, fresh agent session per job, file lock prevents double-run. Each run has no memory of earlier runs unless `continuity=true` or `context_from`. Script timeout 3600 s; LLM jobs have no stated timeout. `cron.max_parallel_jobs` caps concurrency. Missed fires catch up once. A run interrupted by a crash is marked `unknown` and is NOT re-run. `no_agent` mode runs a script with no LLM. `failure_streak` nudge at 3. Cron sessions cannot create cron jobs. [H] https://hermes-agent.nousresearch.com/docs/user-guide/features/cron
- Sessions: SQLite `state.db` (WAL), FTS5 `session_search`. Gateway chats do not auto-reset; only `/new` or `/reset`. Ended sessions pruned after 90 days. `max_resume_messages` 20,000. [H] https://hermes-agent.nousresearch.com/docs/user-guide/sessions
- Compaction: trigger at 50% of context by default; gateway hygiene net at 85%. Four phases: prune old tool results, set boundaries, summarise the middle with an auxiliary model, reassemble. `protect_first_n` 3, `protect_last_n` 20. Lean mode keeps an index of ids/SHAs/paths. Degraded cases: if the summary model's window is smaller than the main model's, middle turns can be dropped with no summary. Repeated stalls can fall back to a deterministic summary that drops messages. [H] https://hermes-agent.nousresearch.com/docs/developer-guide/context-compression-and-caching and https://hermes-agent.nousresearch.com/docs/user-guide/configuration
- Turn length: iteration budget default 500 turns [H, config page; exact key not visible]. `terminal.timeout` 180 s per command. `HERMES_API_TIMEOUT` 1800 s. `agent.session_stall_timeout` 300 s, notify-only, does not kill the turn. [H] configuration page above.
- Background processes: survive across sessions in persistent Docker mode; lost if the container is OOM-killed or exits; not recovered on Vercel Sandbox restart. [H] configuration page. `notify_on_complete` (added v0.8.0) reports back when a background command ends. [L] https://toolnavs.com/en/article/1539-why-doesnt-the-hermes-agent-long-task-notify-me-of-completion-notify-on-complete
- Production guidance for long tasks: the docs I read give none beyond the above. Not found: a "long job" guide.
- Known problems: issue #5563 token waste and environment hallucination after 3 weeks of heavy use [M] https://github.com/NousResearch/hermes-agent/issues/5563. Third-party: infinite tool loop on empty response after a tool call, mostly non-Claude models (issue #9400) [L] https://www.betterclaw.io/blog/hermes-agent-bugs-fixes. Third-party: context exhausted and cannot compress; advice is start a new session, discard half-done work, resume from committed progress and the issue [L] https://codenote.net/en/posts/hermes-agent-context-exhausted-handoff-new-session-resume-pr/.
- The README itself says nothing on turn caps, `delegate_task`, or background jobs. [H] https://github.com/NousResearch/hermes-agent

## 3. AWS Bedrock AgentCore (official docs only)

Runtime [H] https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html and https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html
- Each `runtimeSessionId` gets its own microVM. Idle timeout default 900 s, range 60-28800. `maxLifetime` default 28800 s (8 h), range 60-28800 for microVMs. The idle timer resets on each invoke; `maxLifetime` cannot be reset. After termination the session "can be resumed with a new microVM".
- New "Instances" compute type (EC2-backed capacity provider) allows up to 1,209,600 s (14 days). Same lifecycle page. Billed as EC2 plus a management fee. Unknown fit for us; worth a look for 8h+ jobs.
- Active session workloads per account: 5,000 in us-east-1 and us-west-2, 2,500 elsewhere (adjustable). New-session creation 25 TPS (adjustable). Data-plane API 1,000 TPS.
- Max hardware per session 2 vCPU / 8 GB. Docker image max 2 GB.
- Synchronous request timeout 15 min. Streaming/WebSocket max 60 min. Asynchronous job max 8 h. Payload 100 MB.
- Session storage limit: 1 GB per session (persistent session filesystem). I could not find a separate root-disk figure in the quotas page. Our own memory note says the microVM disk is about 5 GB; reconcile before relying on either.
- `InvokeAgentRuntimeCommand` timeout 1-3600 s (default 300). Shell sessions: up to 10 concurrent per runtime, 1 h max connection.
- Runtime concurrency "per runtime" is not a separate quota. The limit is per account (above). Not found: a per-runtime cap.
- Code updates: existing sessions keep the old code until they end.

Browser [H] same quotas page; https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-resource-session-management.html ; https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-profiles.html
- Session timeout default 900 s (15 min), up to 8 h. (The profiles page examples use 3600; that is just an example value.) Recording to S3 optional. Session data TTL 30 days.
- Concurrent active sessions per account 1,000 (adjustable). Up to 500 sessions per browser tool. Per session: 1 automation stream, 1 live-view stream, 1 vCPU / 4 GB, 10 GB disk. Async command max 8 h.
- Live view: a human can watch and interact directly through the live stream. Access is gated by IAM permission `ConnectBrowserLiveViewStream`. So "who can sign in" = any IAM principal you grant that action, and the human logs into the target site inside the stream. The docs I read do not describe any "pause the agent / hand back" call; do not assume one.
- Profiles: save cookies and localStorage to a profile resource with an explicit `SaveBrowserSessionProfile` call. New sessions start from the saved profile. Saving overwrites. A running session does not see later profile changes. Profile max 50 MB, 100 profiles per account (adjustable). Cookies still expire by their own rules. Docs warn profiles hold sensitive auth cookies, so restrict IAM. Profile storage is billed as S3 from 2026-04-15.
- Not found: any statement that profiles survive a site's "unusual login" or device checks, or guidance on 2FA/CAPTCHA.

Memory [H] quotas page; https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html
- Short-term events (expiry 7-365 days, 10 MB per event, 100 messages per CreateEvent) and long-term extracted records. Max 6 strategies per memory, 150 memories per region. CreateEvent 200 TPS per account, 5 TPS per actor per session. Long-term extraction 150,000 tokens/min per account (adjustable), 50,000/min per session for episodic.
- The docs name "multi-step business process, such as invoice approval, uses memory to track the status of each step" as a use case. That is an AWS suggestion; it is an extraction-oriented store, not a transactional ledger. I would keep job state in Paperclip/DB.

Identity [H] https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/key-features-and-benefits.html
- Token vault stores OAuth tokens, client credentials, API keys, KMS-encrypted. Supports 2LO (client credentials) and 3LO (authorization code, user consent). Built-in providers: Google, GitHub, Slack, Salesforce, Atlassian. Custom OAuth providers for anything else (so Stripe-style OAuth is possible; no Stripe built-in is listed).
- SDK decorators `@requires_access_token` / `@requires_api_key` fetch the token and, when consent or re-consent is needed, generate an authorization URL. Credential refresh is automated.
- Quotas: 50 OAuth2 providers, 50 API-key providers per region; 200 TPS on `GetResourceOauth2Token`; 11,000 workload identities (all adjustable). [H] quotas page.
- Not found: a per-user token count limit; refresh-token expiry policy; behaviour for Google "testing mode" apps.

Pricing units [H] https://aws.amazon.com/bedrock/agentcore/pricing/
- Runtime v1: $0.0895/vCPU-h and $0.00945/GB-h, per second. v2 consumption: $0.1276/vCPU-h and $0.0169/GB-h. v2 reclaims idle memory after 120 s.
- Browser: $0.0895/vCPU-h, $0.00945/GB-h, per second.
- Memory: short-term $1.00/GB ingested from 2026-10-06; long-term $0.75 per 1,000 records/month (built-in), $0.50 per 1,000 retrievals.
- Identity: $0.010 per 1,000 token/API-key requests; no extra charge through Runtime or Gateway.
- Gateway: $0.005 per 1,000 invocations.
- Worked example (my arithmetic): one 2 vCPU / 8 GB runtime session for 8 h on v1 = 2 x 8 x 0.0895 + 8 x 8 x 0.00945 = about $2.04 if billed for full allocation. Billing is on actual CPU used, so idle waits cost less.

## 4. Credentials and sign-in in shipping products

Pattern summary. Three tiers, in order of preference:
1. OAuth connector, connect once, provider refresh. Composio refreshes tokens before expiry; a revoked or lapsed refresh token moves the connection to EXPIRED and the user must reconnect; a webhook `composio.connected_account.expired` lets you send a reconnect link before a call fails. [H] https://docs.composio.dev/docs/authentication . Nango refreshes before expiry and at least every 24 h, fires webhooks when a refresh fails, and re-authorising keeps stored data. [H] https://nango.dev/docs/guides/auth/token-refreshing . AgentCore Identity does the same job inside AWS (section 3).
2. Saved browser session for sites with no API. Browserbase Contexts persist the whole user data dir (cookies, localStorage, IndexedDB); `persist: true` saves on close; do not run two sessions on one Context; sites may still expire the session, so detect logged-out state and re-authenticate. [H] https://docs.browserbase.com/features/contexts . Anon stores a user-linked session, monitors validity and re-prompts the user. Its docs say linking happens client-side so credentials do not pass through the developer's servers. [M] https://docs.anon.com/docs/link . AgentCore Browser profiles are the same idea (section 3).
3. Vault fill with human approval. 1Password Secure Agentic Autofill (early access, with Browserbase Director): credentials never enter the LLM context; each fill is approved by a human by default; TOTP supported. [M] https://1password.com/blog/closing-the-credential-risk-gap-for-browser-use-ai-agents . Browser Use's comparison: profiles/cookie sync, vault, generated TOTP, agent-owned inbox for email codes, and human-in-the-loop for 2FA that cannot be automated. [H] https://browser-use.com/posts/web-agent-authentication

Per product:
- Manus: Browser Operator extension uses your own browser, logins, and IP. For SMS, CAPTCHA, MFA it asks the user to take over, then hands back. Users can allow it to save logins. Vendor says this avoids CAPTCHAs; no independent check. [M] https://manus.im/docs/integrations/manus-browser-operator and https://help.manus.im/en/articles/11813705-what-can-i-do-if-i-encounter-browser-access-issues
- OpenAI ChatGPT agent: takeover mode where the user types credentials; not stored. Source: help center page returned 403 here; claim rests on earlier snippet in the first report. [M]
- Anthropic computer use: have a human confirm consent actions; keep credentials out of the prompt (injection risk). [H, from first report] https://platform.claude.com/docs/en/docs/agents-and-tools/tool-use/computer-use-tool
- Lindy: connects apps from the dashboard; MCP servers use OAuth. Not found: how Lindy stores logins for computer use, or how it handles 2FA. [L] https://www.lindy.ai/integrations/browser
- Relay.app: human-in-the-loop steps (approval, data input, task, path) pause the run until the assignee acts, notifying by Slack or email. Not found: its OAuth model. Important: Relay.app announced shutdown. Free users 2026-08-15, paying 2026-09-14, signups off 2026-07-16; the official docs page may list the dates reversed. Do not build on it. [M] https://www.getinboxzero.com/blog/post/relay-app-shutting-down and https://docs.relay.app/human-in-the-loop/human-in-the-loop-steps
- Paragon: not found (search returned only Nango).
- Anon claims no public API exists for about 95% of user-requested actions (their own figure). [L] https://cerebralvalley.beehiiv.com/p/anon-agents-passport-every-site-internet

Who needs a human each time (what the sources support):
- Standard practice: a human acts for first login, MFA setup, CAPTCHA, SMS/email codes, and re-auth after expiry. Not per run if the session or token is still valid.
- Services with proper OAuth (Google, GitHub, Slack, Stripe Connect, Atlassian, Salesforce): human once, at consent; again only on revoke, scope change, or refresh-token death.
- Sites with no API (directories, LinkedIn-style): human at first login and whenever the site challenges. Cookie lifetimes are set by the site, so expect periodic re-login. No source gives a number; not found.
- Anything with irreversible effect (spend, publish, legal): approval step, per Relay, Lindy, 1Password, Anthropic.

## 5. Parallelism and queueing (numbers where published)

- Manus: Wide Research runs 20 subtasks at once, with each subtask capped at 50 credits. [H] https://help.manus.im/en/articles/11960169-what-is-wide-research . Plan-level concurrent task counts (free: 1; paid: reportedly 20) come from third-party sites that disagree. [L] https://fast.io/resources/manus-ai-limits/
- Devin: official billing docs say Teams has no concurrent-session limit [M, via search snippet; the page itself returned 404 when I fetched it] https://docs.devin.ai/billing . Third-party says 1-2 on Starter and 5 on Teams, with a queue, unverified. [L] https://usagebar.com/blog/devin-pricing-and-rate-limits . Enterprise has per-user and org ACU limits; a session is blocked when either is hit. [M] https://docs.devin.ai/enterprise/features/usage-policies
- OpenAI Codex cloud: usage windows, not a published concurrency cap. Cloud tasks and local messages share a 5-hour window plus weekly limits. Older snapshot: Plus about 5-40 cloud tasks per 5 h, Pro about 50-400. Enterprise scales with credits. [M] https://developers.openai.com/codex/pricing . Task runtime limit: not found.
- Lindy: credits per run (about 1-3 on small models, about 10 on large ones); pauses agents when credits run out; checks in when a task uses more than expected. Concurrency and per-run time limits: not found. [H] https://docs.lindy.ai/account-billing/credits
- Relay: not found (and product is shutting down).
- Google Mariner: up to 10 tasks at once (first report).
- Paperclip: one owner per issue, per-agent cap via `maxConcurrentRuns` (default unclear, see section 1). Hermes: 10 children per batch, `cron.max_parallel_jobs`.
- Pattern across the vendors: a cost cap (credits/ACUs) is published more often than a concurrency cap. Queue-instead-of-reject is the common behaviour.

## 6. Durable execution fit

Evidence:
- Temporal: Signals for human approval, `Claim Check` pattern to keep payloads out of history. Strong for days-long audited flows, but needs a cluster and determinism rules. [M] https://docs.temporal.io/ai-cookbook and https://www.zenml.io/blog/temporal-alternatives
- Inngest: `step.waitForEvent`, `step.sleep`; lighter ops, event-triggered. [M] https://www.inngest.com/compare-to-temporal
- Restate: single binary, journals step results, fewer determinism constraints, source-available licence. [M] https://cadenya.com/other-content/durable-execution-for-ai-agents
- Vercel Workflow: durable steps, sleep, hooks, event-log replay; TypeScript first, Python in beta. The docs I fetched were only a getting-started index, so limits were not verified. [L] https://workflow-sdk.dev/docs
- Most comparison posts are vendor-affiliated. [M]
- Shipping vendors do not describe workflow engines. Manus, Anthropic's harness, and Paperclip all keep state in plain records plus a re-wake (reports 1 and 2; Paperclip protocol above).

Reasoned recommendation (mine, from the above):
1. Use the Paperclip issue as the durable record. It already has: atomic checkout, comments, child issues with a parent wake on completion, bounded continuation wakes, monitors, review/approval gates, budgets. Upstream wrote its protocol for exactly "long work across many runs."
2. Add a ralph-style loop only through Paperclip's own mechanisms: leave a "next action" comment, let the continuation wake or a monitor re-wake. Do not add a second scheduler.
3. For each unit of work, make it a child issue (one row per directory/invoice). Parent wakes when all finish. This is the ledger pattern from report 1, and Paperclip gives it to us.
4. Do not add Temporal/Inngest/Restate/Workflow now. Reasons: a second source of truth that must agree with the issue; AgentCore microVMs already die at 8 h so state must sit outside either way; no shipping agent vendor documents using one. Revisit if we need exact-once multi-step side effects with timers across days that issue comments cannot express.
5. Close the known upstream gaps ourselves (these are the real risk): set `maxConcurrentRuns` and `timeoutSec` explicitly on every agent; do not rely on defaults; add our own done-check so green does not mean done (issues #1997, #5947, discussion #13396); keep idempotency keys on external writes.
6. If a durable engine is ever added, pick the one that lets a human reply resume it (Signals in Temporal, `waitForEvent` in Inngest, hooks in Workflow), and keep the Paperclip issue as the human-facing record.

## 7. Evaluation before shipping

- Anthropic's eval guide [H] https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents :
  - Start with 20-50 tasks drawn from real failures. Run multiple trials per task.
  - pass^k when consistency matters: 75% per-trial success gives about 42% for three in a row (0.75^3).
  - Graders: code (state checks, tool-call checks), model rubric (one judge per dimension, with an "Unknown" option, calibrated on human grades), human review.
  - Grade the outcome, not the path; give partial credit on multi-step tasks.
  - Two experts should agree on pass/fail for every task. Build a reference solution. 0% pass usually means a broken task or grader. One example went from 42% to 95% on CORE-Bench just by fixing grading and ambiguous specs.
  - Start each trial in a clean environment. Capability evals start low; regression evals sit near 100%.
  - Example support rubric: state check that a refund is at most 100, max 10 turns.
- tau-bench (first report) gives the pass^k idea with numbers.
- Devin: Cognition's public evals are SWE-bench (13.86% in 2024, 25% random subset) and a proprietary FrontierCode; task and run counts not found. [L] https://cognition.ai/blog/introducing-devin . Answer.AI's outside test was 20 tasks (3 pass, 14 fail, 3 unclear) (first report).
- Manus: self-reported GAIA 86.5 / 70.1 / 57.7 for levels 1-3. Vendor numbers, directional only. [L] https://futureagi.com/blog/manus-ai-comparison-2025/
- Codex: internal eval methods not found. Lindy template testing: not found.
- Concrete suggestion for a fixed catalog (mine): 20 cases per job type from real past jobs, 5 trials each (100 runs), against sandboxed or test-mode backends. Ship gate: pass^5 on the "outcome check" at or above a set bar, plus zero duplicate side effects in a kill-at-every-step drill. Track tokens, wall time, steps, and failure class per run. Re-run on every Hermes image or runbook change.

## Gaps in this report
- Not read: OpenAI help center and system card (403), Devin billing page (404), Paperclip routines guide beyond the API page, Vercel Workflow limits, Inngest and Restate primary docs, Lindy and Relay credential docs, AgentCore Identity getting-started and consent-portal pages, AgentCore Browser live-view page (returned only a title).
- Paperclip issue statuses were read from search results and titles; check the current state before citing.
