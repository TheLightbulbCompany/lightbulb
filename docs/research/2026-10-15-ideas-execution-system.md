# How an Idea gets executed: the system, the numbers, the gaps, and how to prove it works

Date: 2026-10-15. This is the synthesis of four code-read reports written today. Nothing in it was run; every claim comes from a file in the repo, the Paperclip fork, or an official document. The detail and the line numbers are in the four source reports:

- `2026-10-15-ideas-execution-paperclip-mechanics.md` — wakes, runs, retries, timers, delegation, the blocked-comment trace.
- `2026-10-15-ideas-execution-runtime.md` — AgentCore topology, limits, what survives a recycle, the browser, mail, money, observability.
- `2026-10-15-ideas-execution-signins.md` — every service, how it is connected, scopes, and what each of the 35 ideas needs.
- `2026-10-15-ideas-execution-outside-research.md` — Paperclip and Hermes upstream, AgentCore official limits, how Manus, Devin, Lindy, Codex and the credential vendors do it.

Earlier context: `2026-10-15-ideas-capability-map.md` (which ideas are deliverable), `2026-10-15-long-horizon-agent-engineering.md` and `2026-10-15-how-execution-agents-are-built.md` (how good agents are built).

Code state: Lightbulb `origin/main` 521ea1cc7; Paperclip fork c6f5a4d2e (the deployed image may differ; we are 3 releases behind upstream).

## 1. One idea, end to end, as the code works today

1. **Let's do it.** The backend claims the start (DynamoDB, idempotent), then:
   - size `project`: creates a Paperclip project, status `in_progress`, lead agent Friday. No tasks. **Projects never wake anyone.**
   - size `task`: creates one task, status **`backlog`**, assigned to the agent. **Backlog never wakes anyone.** (Chosen so the DM message is the only wake.)
   - size `quick`: nothing in Work.
   - Then it posts the person's own message in the agent's DM: the idea quoted, "Let's do it!", and for project/task "Tracked in Work: [link]".
2. **The DM wakes the agent.** Message → SQS FIFO (one lane per room+agent) → the workspace's runtime → the bridge → one Hermes turn. The turn has a 30-minute budget. It runs on the company's one microVM, which allows 4 turns at once across all agents.
3. **The agent follows `references/ideas.md`** (the whole instruction is 20 lines):
   - project: "break the work into tasks in that project, each assigned, so it continues after this reply";
   - task: move the linked task to `in_progress`, do it, close it with proof;
   - no link: answer in one go.
   The agent gets only the title, the why line and the three deliverables. It does not get "your part", what done looks like, or which connections the idea needs.
4. **What keeps the work moving after that turn** (and nothing else):
   - Paperclip's 30-second reconciler re-dispatches assigned `todo` / `in_progress` / `in_review` issues with no live run. It ignores `backlog`.
   - Retries: transient failures 2×30 s; then 1 attempt (3 for timeouts; 0 for setup failures); then the issue is set to **`blocked`** with a board notice. The DM gets nothing.
   - Timers: the CEO wakes every 8 h (paid), 12 h (trial) or 24 h (BYO key). Specialists wake every 24 h, and only if they hold `todo`/`in_progress` work. There is no "daily check-in" beyond these.
   - Events: a human comment, an @mention, all children done (wakes the parent), all blockers done.
5. **Delegation.** When Friday files a `todo` task for Atlas, Atlas wakes at once in its own issue session, in parallel with Friday. Atlas's result lands **only as an issue comment**. Friday is woken again only by a parent/child or blocker link, a mention, or its next timer. Nothing posts the result to the person's DM.
6. **Done.** The issue is done when the agent sets `done`. Nothing checks the work exists. The idea reappears in the list when the project is `completed`/`cancelled` or the task is `done`/`cancelled`.

### The two defaults that fight each other

- `ideas.md` says project tasks must be assigned so they wake. The Paperclip skill says `backlog` is "the normal case" and the helper script sends `backlog` when no status is given. **A task filed as backlog never runs**: the reconciler, the timers and the CEO's nudge all skip it. The helper also has no `--project` flag, so the agent cannot file a task inside the idea's project without a raw API call.
- A `task` idea is born in backlog. If the first DM turn fails (model cap, cold start, cut-off text) or the agent forgets to move it, nothing ever picks it up. The idea stays hidden as "started".

## 2. Topology and numbers

```
Company (owner)
  AgentCore Runtime (1 per company) → session (1, ≤ 8 h) → microVM (2 vCPU, 8 GB, ~4–5 GB disk, shared by every agent)
      bridge process: 4 turn slots for the whole company; lock per (agent, conversation)
      profile per agent (files, state.db, .env), synced to S3 at turn end
  AgentCore Browser (1 per company, own 8 h life; 1 automation connection, 1 live viewer)
  Mailbox agents@<slug> (1 per company, 50 sends/day)
  Paperclip board (company) ── hermes_gateway adapter ── backend facade ── the same microVM
```

| Thing | Value | Scope |
|---|---|---|
| Hermes turn budget | 30 min | per turn |
| Paperclip run wall clock | 60 min (poll every 10 s) | per run |
| Turns at once | 4 (`LIGHTBULB_MAX_CONCURRENT_TURNS`, unmeasured) | per company microVM |
| Same agent, two conversations | parallel | |
| Same conversation | one turn at a time; a new message steers the running turn; a typed turn runs beside it after 30 s | |
| Paperclip runs at once | 20 per agent (default; set it explicitly) | per agent |
| Session idle timeout | 30 min (background processes do **not** count as busy) | per company |
| Session recycle | ours at 7 h (forced 7 h 45), AWS hard cap 8 h, or free disk < 1 GB | per company |
| Survives a recycle | profile files, transcripts, AgentCore Memory, Paperclip issues, browser cookies + localStorage, mail (30 d) | |
| Lost at a recycle | background processes, live delegations, `/tmp`, `.scratch`, builds, a run in flight (`runtime_lost`) | |
| Browser idle stop | 30 min; codes/CAPTCHA wait 390 s; a Paperclip run has no room so it does not wait at all | per company |
| Model spend cap | $0.25/day on trial and Base (all agents stop for the UTC day); $50/day backstop on managed paid | per company |
| Cold start | 54–57 s; the facade gives up at 25 s, so the first wake into a parked workspace fails and relies on 2 retries 30 s apart | |
| Compaction | at 60k tokens, low-effort summary; context pin 200k | per conversation |
| Visible to the owner | room runs: live card + one failure note; Paperclip runs: board only; alarm only above 20 failures / 15 min | |

Cost: AgentCore is about $5 per workspace-month now; model spend is the real cost. A per-task sandbox would be $100–330/month at 50 tasks/day (owner ruling 10-15: not now).

## 3. Sign-ins and services

- Every connection is **per workspace** and is made by an **org admin** on Connections (`/chat?panel=connections`). Agents cannot start a connection; they can only print the link.
- Connect once, act many times: Google (Gmail/Calendar/Drive write), GitHub (repo write), Stripe (MCP), QuickBooks (accounting write incl. `send_invoice`), Vercel (token), AWS (role), Cloudflare, Attio, Apollo, Notion, Intercom, PostHog, Parallel search, X.
- Traps: Google needs the owner's **own Google Cloud app**, and an External app left in Testing loses access after 7 days. Google, QuickBooks and X tokens are single-writer and cannot be rebuilt from the backend after a wipe. Stripe webhooks need a manual secret paste.
- Read-only today: Microsoft 365 (no send, no event create), Linear, Sentry, Apollo (search only). Shopify writes are forbidden by rule without a per-conversation confirm. Meta Ads and Instagram are hidden in prod.
- Not in the product at all: phone/SMS, LinkedIn, Google Ads, GA4, Klaviyo, HubSpot, registrars, Product Hunt, job boards, Shopify CLI, Google Business Profile.
- Sites with no API go through the one shared browser: a person signs in once in the live view; cookies persist; codes and CAPTCHAs always need a person; verification emails with `Auto-Submitted` or `Precedence: bulk` are **dropped before storage**, so a code sent by email may never arrive.

Blocks the most ideas, in order: M365 write (~12 ideas for Outlook owners), DNS/registrar write (~6), Shopify write (~6), ads and social publishing (~5), phone/SMS (~5).

## 4. What upstream and the industry say (short)

- Paperclip's own protocol for long work: the issue is the durable record; leave a "next action" comment before exiting; split long or parallel work into child issues; the parent wakes when all children finish; keep `in_progress` only with a live wake path; waiting on a human = `in_review` with a pending interaction. It has review and approval stages per issue (executor sets done → `in_review` → a different reviewer, 3 rounds then a human). **No checklist feature.**
- Upstream failure reports match ours: wakes stuck as `deferred_issue_execution` after a run ends (#16519), zero-turn Hermes runs recorded as success (#16443), timer runs stacking (#6955), green status with no work (#1997, #5947).
- Shipping agents (Anthropic harness, Manus, Devin, Lindy) keep state in plain records plus a re-wake, not in a workflow engine. Done must be an end-state check the agent cannot self-assert. Lindy moved from open-ended agents to guided workflows. Human gates sit at side effects and credentials only.
- Evaluation: 20–50 cases from real jobs, several trials each, **pass^k** (all k pass), outcome graded not path, clean environment per trial, kill-at-every-step drill for duplicate side effects.
- AgentCore extras we do not use: browser **profiles** (explicit save of cookies for "sign in once"), **Identity** token vault (Google and GitHub built in), **Instances** compute (sessions up to 14 days).

## 5. The gaps, ranked by what they do to an idea

| # | Gap | Effect on an idea | Where it lives |
|---|---|---|---|
| 1 | Task ideas are born `backlog`; project tasks default to `backlog`; no `--project` flag | the most common way an idea silently never runs | `ideas.py`, `paperclip-api.sh`, `paperclip/SKILL.md`, `ideas.md` |
| 2 | Done is self-declared | the idea leaves the list with nothing delivered | no code; needs a "done means" per idea and a check |
| 3 | A deferred comment wake can strand behind a finished run; the reconciler then treats the deferred wake as live and never re-dispatches | the owner's blocked→todo issues that never started | Paperclip fork (`heartbeat.ts`, wake-queue policy); likely upstream #16519 |
| 4 | Retry exhaustion → `blocked` with a board notice; nothing reaches the DM | the person thinks it is running | fork recovery + `paperclip_events.py` (push is fire-and-forget) |
| 5 | Daily cap and cold start are not recognised as "wait and retry" for our adapter | a $0.25 day or a parked VM spends the retry ladder and blocks the issue | fork `recovery/service.ts` classifier; facade 25 s budget |
| 6 | The 30-minute budget ends a turn with no signal; nothing enforces a "next action" note | long jobs lose their place | bridge `turn.py`; `ideas.md` |
| 7 | Paperclip-run results land only as issue comments | the person never hears back in the DM | by design (`send_room_message` is NO_TURN in Paperclip turns) |
| 8 | Codes/CAPTCHA cannot be waited for in a Paperclip run; verification mail is dropped | browser-based ideas stall | bridge `website_logins.py`; `ses_inbound.py` loop guard |
| 9 | 4 turn slots per company, unmeasured; `maxConcurrentRuns` 20 per agent by default | fan-out (40 directories) queues behind chat; stacking | bridge; Paperclip agent config |
| 10 | Free-tier model cuts text at tool calls | fewer steps complete per turn | deferred by the owner |

## 6. What to change (proposal, not started)

Phase A — make the mechanics hold, smallest change first. Each item is one PR.

1. **Paperclip upgrade** to the current upstream release (already step 1 of the Forge parallelism plan). Then re-test the blocked-comment case on dev. Upstream #16519 describes our symptom.
2. **Statuses and the helper.** `paperclip-api.sh create/subtask` get `--project`. `ideas.md` says: a task idea → `checkout` first (binds + `in_progress`); a project idea → file every task as `todo` inside the project (id from the link); after each meaningful step leave a one-line "next action" comment. The Paperclip skill's "backlog is the normal case" gets an exception for idea work.
3. **Backstop for stranded idea tasks.** A task idea still `backlog` 10 minutes after start is moved to `todo` by our backend (that wakes the agent in its own issue session). Same for a project with no open task after 10 minutes: file one `todo` task "Plan this project" for the lead.
4. **Park on cap and cold start.** The fork's quota classifier learns our `hermes_gateway_run_failed` "hit its usage limit" message (park until reset, like the OpenClaw codes) and the cold-start 503 gets 3 minutes of patience instead of 60 seconds.
5. **Done means.** Each idea YAML gets `done_means` (one line a check can read). It goes into the task/project description. The idea's issues get Paperclip's review stage with Friday as reviewer (not the executor), 2 rounds then the person. For the five test ideas, a script check where one exists (invoice has a sent timestamp; PR merged; reply in Sent).
6. **Report back to the DM.** On `done` or `blocked` of an idea's issue, the backend posts one line to the DM that started it ("Done: <title> — proof in Work" / "Stuck: <title> — needs you in Work"). The start row already knows the room.
7. **Explicit agent limits.** Set `maxConcurrentRuns` (3) and keep `timeoutSec` 3600 on every agent at provision and reconcile.

Phase B — prove it. Then widen.

- Later, owner decisions: M365 write scopes, Shopify write + CLI, DNS/registrar, LinkedIn/ads connectors, phone/SMS; AgentCore Identity and browser profiles; model choice and free-tier pricing.

## 7. How we confirm it will deliver (test plan)

**Where.** Mechanics on **dev** (wake chain, backlog backstop, blocked-comment repro, kill-at-step). Real connectors on the **founder workspace on prod**, approvals on, only ideas whose side effects we control.

**Accounts needed** (the owner creates; I cannot): a test Google account with its own Cloud app (Gmail + Calendar), Stripe test mode, QuickBooks sandbox (dev already points at Intuit sandbox), one small GitHub repo with a deployed site. The founder workspace already has Google, GitHub, Stripe and QuickBooks connected (to confirm on Connections).

**Ideas to run first** (from the capability map): chase-overdue-invoices, answer-leads-fast, inbox-triage, monthly-receipts-package, seo-audit-fixes. Runners-up: send-invoices-on-time, dependency-prs, meeting-recaps.

**Per run, score these seven**, each yes/no:
1. A run started within 2 minutes of Let's do it.
2. The Work item left backlog and has a "next action" comment after every run.
3. It never sat in `blocked`/`todo` with no run for more than 5 minutes without a Needs-you item.
4. Every send, publish or payment had an approval first.
5. The done check passes (the deliverables exist, with proof links).
6. The Work item closed and the DM got the result line.
7. No duplicate side effect after a forced VM stop mid-run (one trial per idea).

**Trials.** 3 per idea on dev, 3 on the founder workspace. Ship gate for an idea: 3 of 3 on points 1–6 (pass^3) and 7 clean. Record tokens, wall time, runs per idea, and the failure class of every miss; every miss becomes a case.

**Blocked-comment repro on dev (first).** Create a task, let the agent block it with a question, answer by comment, watch `agent_wakeup_requests` and `heartbeat_runs`. Expect: deferred wake stranded (cause 1 in the mechanics report §9.4). Then upgrade Paperclip and repeat.

## 8. Open questions for the owner

1. Prod read of the founder workspace's wake rows for the blocked issues, or the issue ids, so the live case is confirmed before we fix it.
2. Approve Phase A order (1–7) before any code. Items 2, 3, 5, 6 are ours; 1 and 4 touch the fork (coordinate with the Forge/Paperclip upgrade track).
3. Which test accounts you will create, and whether the founder workspace may run the five ideas with approvals on.
