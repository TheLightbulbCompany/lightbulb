# Handoff — GTM dashboards on the admin site

**Goal:** build dashboards on the admin site driven by the GTM plan.
**Prerequisite the owner named explicitly: research everything first — starting with
our own prod container and its current state.**

Disposable planning doc (CLAUDE.md). Delete when the work ships.

---

## 0. The research that must happen FIRST

The owner's instruction: *"the handoff should do the research of our prod container
and its current state."* Do not start building until this is written down.

Our own prod container IS the GTM plan's execution surface — the strategy in
`docs/growth/` is the theory, the founder's Paperclip board is where it actually
lives as goals/routines/issues. A dashboard built from the doc alone will measure
something nobody is running.

**Status:** DONE 2026-08-26 — answers recorded in §0.1 below (read-only AWS +
Aurora Data API + PostHog + repo/fork reads, cross-checked against the graphify graph).

Gather, and record the answers in this file:

1. **Container runtime.** Owner id (the founder is an **org** owner, `org_3H3v…`,
   not a personal user — its container is `openclaw-org_3H3viGAfq2SXc9dIdcatmGXg8Td-c42489f74b24`,
   task def family `isol8-prod-openclaw-user`). Status, task definition revision,
   image tag, whether it is on the 8.1 roll. Compare against `prod.tag` in
   `openclaw-version.json` — remember the pin is NOT inert.
2. **The roster.** Which agents exist beyond Friday/Atlas, and `reportsTo` wiring.
3. **Routines/automations — the load-bearing one.** How many exist, how many are
   **armed vs paused**, `isAutoPaused` vs manually paused, and whether any have
   triggers at all. Last known state (2026-08, verify — may be stale): **13/13
   routines manually paused, `autoPausedAt` null, 0 failures, and 2 of them have NO
   TRIGGERS so arming them is a no-op.**
4. **Goals + issues.** Last known: 3 active goals, 2 of which have ~zero attached
   work (the angel-round goal had 0 issues). Plus ~17 unanswered confirmation cards
   sitting in the board.
5. **Does it actually run?** Any successful autonomous runs in the last 30 days.

**Why this gates the work:** if our own company OS is off, then every North Star
number the dashboard renders for us reads zero — and that is a true reading, not a
bug. Whoever builds this needs to know which zeros are real before they debug them.


## 0.1 Research findings — recorded 2026-08-26 (all read-only; UTC)

### 1. Container runtime — healthy, on the pin
- ECS service `openclaw-org_3H3viGAfq2SXc9dIdcatmGXg8Td-c42489f74b24`: ACTIVE, 1/1 running,
  task-def `isol8-prod-openclaw-user:276`, task `c70b0eee…` RUNNING/HEALTHY since
  2026-08-25T18:24Z, image `isol8/openclaw-extended:2026.8.1-isol8.1-1c67107` = `prod.tag`
  (**match**; note `openclaw-version.json` `notes` prose still says prod stays on 7.2 — stale).
  1 vCPU/4 GB, capacity provider `FARGATE` (on-demand, not Spot). Three task generations on
  08-25 13:25–14:24 EDT (the 8.1 roll), steady since.
- DDB containers row: `status=running`, `substatus=gateway_healthy`,
  `last_gateway_handshake_at=2026-08-26T03:56Z`; `last_error` is a stale 08-03 circuit-breaker
  string never cleared. Billing row: `plan=standard`, `subscription_status=active`,
  `provider_choice=chatgpt_oauth`, `seat_count=2`, **`nightly_ceo_opted_out=true`**.
- Paperclip-companies rows (2, via `by-org-id` GSI): owner row `bootstrap_state=delivered`,
  `first_contact_state=sent`, `funnel_state=complete` (all 07-29); member `aden@isol8.co`.
- Last container log event 2026-08-26T03:01Z (`/isol8/prod/openclaw`, stream `…/c70b0eee…`).

### 2. Roster (Paperclip company `1071914e-6b9a-4351-a1d6-58bffe2e30f1`, prefix ISOA)
7 agents, all `status=idle`, none paused/errored, all `openclaw_gateway`, all budget/spent 0.
`reports_to`: Friday (ceo) at root; Atlas, Forge, Gauge, Pulse, Vera, Tally → Friday
(the 08-22 org-tree repair held). Last run: Friday 08-23 22:34, Atlas 08-07, Forge 08-25 02:55,
Gauge 08-25 02:32, Pulse 08-23 21:39, Vera 08-23 21:55, Tally 08-20 21:02.

### 3. Routines — the company OS is OFF
35 rows: **0 active, 13 paused, 22 archived.** All 13 paused are **manual** pauses
(`auto_paused_at`/`auto_pause_reason` NULL, `consecutive_failure_count=0` on every row);
10 of 13 were paused in one batch 2026-08-20 17:18:22–29. Two paused routines have **zero
triggers** (`Reliability incident` → Forge, `Support and reply routing` → Vera). Every schedule
trigger is `enabled=true` with `next_run_at` frozen in the past (08-21…08-25) — the scheduler
filters on `routines.status='active'`, so an armed trigger on a paused routine is inert.
The archived 22 include the original `Nightly CEO progress` (12 runs, archived 08-15 — hence
`nightly_ceo_opted_out=true`), `GitHub: Issue opened or changed` (11 runs), and 13 never-run
seeds. All 35 `origin_kind=manual`.

### 4. Goals + issues + decisions
- Goals (no target-date column exists): *Reach 50 paying customers…* active, 189 issues
  (done 133 / backlog 22 / cancelled 12 / in_review 12 / blocked 8 / todo 2), 13 routines;
  *Start angel round in November* active, **0 issues**; *Get 100 users who love the product*
  active, 1 todo. Still two decorative goals.
- Issues: 190 total = `issue_counter` (no gap). **69 hidden** (`hidden_at` set — see §3
  correction below for why the list endpoint under-counts), 79 unassigned.
- **12 pending `request_confirmation` cards** (was 17 on 08-22), oldest 2026-08-06 (ISOA-28
  "X login required"); all `addressee_agent_id=NULL` (board-only). Plus **1 pending
  `request_board_approval`** (Forge, 08-24 06:21, prod-deploy CORS change). Interactions
  all-time: 24 accepted / 23 expired / 9 rejected / 12 pending.

### 5. Does it run? — yes, until 08-20; nothing routine-triggered since
`heartbeat_runs` 30d: 209 succeeded / 72 failed / 10 cancelled / 6 timed_out. By the issue's
origin: routine_execution **70 ok / 17 failed / 1 timed_out**; by the dispatching
`routine_runs.source`: **schedule 27 ok / 7 failed, webhook 4 ok / 0 failed**, manual "run
now" 43 ok / 15 failed. Last schedule firing 2026-08-20 14:00 (Outbound prospecting →
ISOA-175, done); last webhook 08-20 16:21 (Engineering intake → ISOA-176, done). **No run of
any kind since 2026-08-25 02:59Z.** 30d failure causes: `gpt-5.5 429 usage limit` ×29 (last
08-20), compaction timeout ×23 (July), `operator relay unavailable` ×9 (last 08-23 22:34 =
Friday's final run), `issue_continuation_needed` wake → 34 failures.

### Fleet baseline (prod, 2026-08-26)
- DDB: **22 owners** (1 org + 21 personal). Billing: **4 `standard/active/chatgpt_oauth`**,
  **18 `trial_local` — every one past `trial_end`** (latest 08-24). Containers: 4 running
  (= the 4 paid, all reprovisioned 08-25 onto 8.1), 18 stopped `trial_idle` on pre-8.1
  task-defs (revs 248–267). Zero Bedrock-managed, zero BYO.
- Internal-looking rows: `prasiddha@gmail.com` (trial, stopped), `aden@isol8.co` (org member,
  no container), `prawahp@gmail.com`, `prabuddha@gmail.com` (trials). **No `admin@isol8.co`
  row in prod users**; the founder org's owner user has no email in DDB (Clerk only).
- Paperclip: 22 companies; **only 2 have any routine** (founder: 13 paused + 22 archived;
  Lull Brands: 1 active `Daily Lull Shopify analytics email report`, `0 5 * * 1-5`, last
  fired 2026-08-25 09:00 — the ONLY armed-and-firing automation in the fleet). Successful
  runs 30d: Isol8 209, MyTrainer 4, Lull Brands 3; 4/22 companies have ever had a run.
  `cost_events` = 0 rows fleet-wide; 0 of 297 founder runs carry `usage_json` — cost
  reporting is structurally empty, not merely zero.
- PostHog project 380894, `agent_run_completed`: 225 events since 2026-08-20T19:28Z, 8 owners
  (founder org 175, seven `user_` ids 26/13/6/2/1/1/1). `initiation_type`: human 219,
  **autonomous 1 (a `cron`)**, unknown 5. `run_source`: webchat 155, issue_assignment 52,
  routine_manual 10, unknown 5, paperclip_other 2, cron 1. **`trigger_provider` null on all
  225; `routine_schedule`/`routine_webhook` never appear.**

### Discrepancy that decides the aggregate-source question
Post-instrumentation (after 08-20 19:28Z) the founder DB shows 64 succeeded runs; routine-origin
= 13 manual-"run now" + **2 schedule-origin (08-23 21:33Z, 21:45Z, continuation runs on
schedule-created issues)**. PostHog has 10 `routine_manual` and **0 `routine_schedule`**.
So `agent_run_completed` under-attributes autonomous runs (enrichment loss / backend restarts —
the 5 `unknown`+`enrichment_failed` events are the founder's). **The Paperclip DB
(`heartbeat_runs.context_snapshot->>'issueId'` → `issues.origin_run_id` → `routine_runs.source`)
is the authoritative origin source; PostHog is the cross-check, not the ledger.**

### Corrections to §2 and §3 of this doc
- §2: the prod `phx_` read key **can run HogQL** — `POST /api/projects/380894/query/` → 200,
  `GET /insights/` → 200, `GET /projects/380894/` → 403 (no `project:read`). So "HogQL vs
  events endpoint" is settled: HogQL is available. Nothing in the repo uses it yet
  (`posthog_admin.py` is Events-API-only, per-user). Also: dev and prod backends write to the
  **same** PostHog project (`POSTHOG_PROJECT_WRITE_KEY` has no env branch in
  `service-stack.ts:1497`); no `dev.isol8.co` traffic visible in the last 7 days, but do not
  assume that holds.
- §3 (the `routineHealth`/`isAutoPaused` "unfair instrument"): those are **Isol8 frontend
  helpers** (`components/teams/shared/lib/routineHealth.ts`: `status==="paused" &&
  !!autoPausedAt`), not Paperclip fields. The real columns are `routines.auto_paused_at /
  auto_pause_reason / consecutive_failure_count / auto_pause_enabled / auto_pause_threshold` —
  all **fork-only** (`routine-circuit-breaker.ts`, default threshold 3, flips on the linked
  issue reaching blocked/cancelled). The backend never reads them.
- "Automations deleted <7 days" has no direct signal: there is **no `deleted_at`**; Isol8's
  DELETE is `PATCH status=archived`. Use `status='archived' AND updated_at − created_at ≤ 7d`.
- "Distinct trigger providers" has **no column** on `routine_triggers` (only `kind`
  schedule|webhook|api, `signing_mode`, free-text `label`). Provider is recoverable only by
  inverting the title/label (`automation_from_title`), or from the Isol8 **integrations table**
  (`{owner_id, provider, status:"connected"}`) for "connected", which is the better source.
- `routine_runs.status='completed'` only flips when the linked issue reaches `done`;
  `issue_created` = dispatched. `heartbeat_runs` has no `routine_id`.
- **No cancellation reason is stored anywhere** (Stripe `cancellation_details` never read);
  the nearest is PostHog `subscription_cancelled{was_trialing, cancel_at_period_end}`.
- `provision.gate.blocked` codes are **CloudWatch EMF only** (namespace `Isol8`, dim `code`),
  never persisted to DDB. No Isol8-side metric exists for routine create/arm/pause/delete or
  auto-pause; `automation_armed` is PostHog-only (3 events all-time, 1 owner).
- Issues-list under-count root cause (deployed fork): `visibleIssueCondition` =
  `hidden_at IS NULL AND harness_kind IS NULL` + plugin-operation exclusion; `attention=blocked`
  is a separate JS-filtered path. Founder: 69 of 190 issues are hidden.
- **No cross-company Paperclip read exists** (`PaperclipAdminClient` is company-scoped; the
  instance-admin cookie can read any company but callers resolve `company_id` from DDB first).
  Fleet aggregates = iterate `paperclip-companies` rows (or Data API SQL).
- §1's cross-reference to `2026-08-14-gtm-and-operating-loops.md` (rev 5): that plan was
  **never merged** — it exists only on branch `feat/marketplace-carries-triggers`
  (commit `2f686749f`). Its decided items still bind: "metrics come from the integrations,
  never a count the loop produces"; "cost per successful completion" as the one number.

### What the admin site has (verified, see §3 for the page list)
Every `/admin` page is a Server Component reading through `adminFetch` (no SWR/useApi, no
client polling except a 20s `router.refresh()` on Overview). No charting library is installed;
the only bar charts in the repo are the owner-facing `teams/dashboard/ActivityCharts.tsx`
(vanilla flexbox, hardcoded 14 days). The only fleet-wide read today is `GET /ops/fleet`
(`container_repo.scan_all()` + ECS describe + Clerk names, TTL-cached). Admin gate:
`require_platform_admin` = Clerk email ends `@isol8.co` or ∈ {`prasiddha@gmail.com`}
(`core/auth.py`) — the same predicate `run_analytics._is_internal_owner` uses for `is_internal`.
`GET /admin/users` declares `subscription_status`/`container_status` filters but ignores them.

### The GTM plan "in the founder container"
The Paperclip DB holds no document titled as a GTM/go-to-market/strategy plan. Closest stored
artifacts: two 2026-08-20 weekly operating plans (ISOA-136; ISOA-160 still `in_review` with its
accept card pending since 08-20 06:03), the two 08-20 Gauge scorecards (ISOA-137/158; the
latter measured "10 `signup_started` users, 0 same-user `chat_completed` → 0.0% activation"),
and ISOA-29's north-star definition (08-07). The repo `docs/growth/` pair is the written
strategy; nothing on EFS was examined.

---

## 1. The GTM plan — where it lives and what is already DECIDED

Two docs, both in-repo:

- `docs/growth/2026-08-17-blitzscaling-strategy.md` (24KB) — **the primary source.**
- `docs/growth/2026-07-10-content-seo-gtm-plan.md` — the operating-loops/SEO plan.
  §7 of the blitzscaling doc reconciles the two and flags where they collide.

§3 (*"PMF evidence — the metric set `DECIDED`"*) is effectively the dashboard spec.
**Do not redesign these metrics — they are decided.** Verbatim:

- **Activation gate:** subscribed → container ready → ≥1 automation armed → **first
  trigger/schedule-fired run completes successfully.** Deliberately EXCLUDES "sent a
  chat message" — *"chat is the demo, automations are the product."*
- **North Star:** successful autonomous runs per active owner per week, split by
  initiation type. The *rising share* of autonomous vs human-prompted runs over an
  owner's lifetime is the real signal.
- **Origin tagging is mandatory:**
  | Bucket | Treatment |
  |---|---|
  | Owner-authored + owner-**armed** marketplace routines | counts toward North Star |
  | Seeded "Nightly CEO progress" (`[NIGHTLY-CEO-LOOP]`, cron `0 8 * * *`) | **EXCLUDED — manufactured autonomy.** Measure pause/delete rate instead |
  | Marketplace-deployed but dormant | funnel metric (deploy→trust gap) |
  *"Never count nightly-CEO runs as retention evidence in an investor-facing number."*
- **Retention, three layers:** (1) W1/W4/W8 cohorts on "≥1 successful autonomous run
  this week"; (2) **automation survival** — % still armed and firing at day 30, via
  `routineHealth`/`isAutoPaused`, described as *"our unfair instrument"* because an
  auto-paused or deleted-within-7-days automation announces churn weeks before Stripe
  does; (3) revenue — logo churn, NRR, seat expansion.
- **Depth:** automations per active owner; **distinct trigger providers connected**
  (GitHub + Stripe + Gmail wired = switching cost).
- **Counter-metrics (falsifiers) — build these too:** circuit-breaker auto-pause rate,
  automations deleted <7 days, `provision.gate.blocked` codes, cancellation reasons.

**Two constraints that must be honoured in the UI itself:**

- *"At current scale (~15 external signups, ~4 working containers) cohort percentages
  are noise."* Render counts and named lists, not percentages, until the base is real.
  The honest bar the doc sets: *five external owners with armed automations firing
  successfully for four consecutive weeks.* Consider making that the headline tile.
- **Internal/external segmentation is mandatory before any aggregate read**
  (`admin@isol8.co`, `prasiddha@gmail.com`). The doc notes this *"burned us once on
  container crash stats."* `run_analytics` already stamps `is_internal` — use it.

---

## 2. Instrumentation — the gap is already CLOSED (do not rebuild it)

§3 of the plan lists an "instrumentation gap: PostHog is frontend-only and
structurally blind to server-side runs." **That has since been fixed.** Verified in
this session:

- `apps/backend/core/services/run_analytics.py` emits ONE server-side PostHog event
  per completed run: **`agent_run_completed`**, carrying `owner`, `initiation_type`
  (autonomous|human|unknown), `run_source`, `trigger_provider`, outcome,
  `is_internal`, `is_nightly_ceo`.
- Emitted from `GatewayConnection._emit_run_completed` — the same terminal signal
  billing already trusts, for every session type (webchat, channel, cron, Paperclip
  issue).
- `_AUTONOMOUS_SOURCES = {routine_schedule, routine_webhook, cron}`; everything else
  classified is human; only unresolved stays `unknown`.
- Dedup: uuid5 of `owner:runId`, so PostHog drops exact re-emits across restarts.
- **Known truthfulness caveat, matters for dashboard copy:** the path is best-effort
  by contract. On Paperclip enrichment failure it still emits, degraded
  (`initiation_type: "unknown"`, `enrichment_failed: true`) — the event COUNT stays
  truthful, only properties degrade. Runs terminating while the backend is down are
  lost. So `unknown` is a real bucket that must be shown, never silently folded into
  human or dropped.

**Read path already exists too:**
- `POSTHOG_PROJECT_ID = "380894"` is set in `service-stack.ts`;
  `POSTHOG_PROJECT_API_KEY` (personal `phx_` read key) comes from Secrets Manager.
- `core/services/posthog_admin.py::get_person_events` →
  `GET /api/projects/{id}/events/?distinct_id=…&limit=N`. Requires the `query:read`
  scope. It carries an explicit NOTE: **do not** query `/persons/?distinct_id=` for
  this. Surfaced today as `admin_service.get_posthog_timeline(user_id)` behind
  `GET /admin/users/{user_id}/posthog` — **per-user only, no aggregate.**

**Therefore the open design question is not "how do we instrument" but "where do
aggregates come from":** query PostHog server-side (HogQL vs the events endpoint —
note only `query:read` is confirmed granted), or aggregate internally from
DynamoDB + Paperclip. Decide this first; it determines everything else.

---

## 3. What the admin site has today

Pages (`apps/frontend/src/app/admin/`): `page.tsx` (overview), `users`,
`users/[id]/{overview,agents,billing,container,provisioning,activity,actions,logs}`,
`marketplace/{listings,takedowns}`, `deploys`, `audit`, `health`.

`routers/admin.py` has **no metrics / stats / dashboard / summary endpoints at all** —
its ~36 routes are per-user levers and marketplace/audit. **The aggregate backend is
greenfield.** Existing conventions to follow: `@audit_admin_action(...)` on mutations,
`admin_service` for logic, and the admin-host gate in `apps/frontend/src/middleware.ts`
(`/admin` 404s off `admin.isol8.co` / `admin.dev.isol8.co`).

---

## 4. Live data to build against (found this session, real)

A genuine stalled-activation cohort in prod — useful as dashboard fixtures because
every one of these SHOULD light up a counter-metric:

| owner | signed up | state |
|---|---|---|
| admin@airguncapital.com | 08-25 | `bootstrap=not_started`, first-contact email **never sent** |
| 7dm4d6zgdh@privaterelay.appleid.com | 08-05 | same — silent 3 weeks |
| ben@enjoylull.com | 08-21 | **PAYING**, `bootstrap=failed` (terminal), 10 lifetime runs, none since signup day |
| priyagupta@gmail.com | 07-28 | stuck `dispatched` |
| shawn@lidvizion.com | 07-30 | `funnel=not_started` — never finished onboarding |

Ben is the sharpest test case: active Stripe sub, ChatGPT OAuth `active`, container
running, and **zero** activity since 08-21. A correct activation dashboard must
surface him loudly. If it renders him as healthy, it is wrong.

Note `last_sign_in_at` in Clerk only moves on a FRESH sign-in — returning users on a
live session do not move it. Use `last_active_at`. I got this wrong once here.

---

## 5. Repo/session state at handoff

Shipped + verified this session: OpenClaw 8.1 base upgrade (dev+prod), #2027
(config derives from target image), #2042 (Friday resolver), #2043 (kickoff fires on
gateway-ready). Kickoff chain verified end-to-end on dev: ready → `chat.send`
(`id=bootstrap-di…`) → Friday run → `first_contact_state: sent`.

Open, none blocking this work:
1. The cohort above — no remediation actioned. Ben needs
   `POST /admin/users/{id}/provisioning/reset-bootstrap` (flagged **may double-deliver**).
2. 429 retry storm on Paperclip sign-in — needs backoff.
3. `deploy.yml` silent dispatch cancellation — a pending promotion is killed by any
   merge with `conclusion=cancelled` and ZERO prod jobs. **Verify promotions by
   counting prod jobs, never by reading the conclusion.**
4. `_runtime_only_delta` across a base boundary + single vendored schema (#2027 holes).
5. **`main` is one commit ahead of prod: `8d4a476e5` (#2044, "default layout shows
   Browser, not Dashboard")** — dev only. Dev and prod currently disagree about the
   default panel; know this before evaluating any dashboard UI.

**Retracted:** I flagged a "trial containers don't wake on click" bug. It was my own
test artifact — `useOnboardingStatus` is visibility-gated and my chat tab was
backgrounded. The wake path is correct by design (the poll carries a `wake` param and
is *deliberately* not disabled at `parked`). Do not go looking for that bug.
