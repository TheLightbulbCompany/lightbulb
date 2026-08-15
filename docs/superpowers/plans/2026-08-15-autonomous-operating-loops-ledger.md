# Autonomous operating loops execution ledger

This is the temporary evidence ledger for
`2026-08-15-autonomous-operating-loops.md`. It contains identifiers and
verification evidence only; never add credentials, tokens, customer message
contents, or other sensitive payloads. Delete it with the plan after the goal
is complete.

## Baseline

- Started: 2026-08-15, `America/Aruba`.
- Plan branch: `plan/autonomous-operating-loops`.
- Plan commit: `80eee446d390d4847cab18eecf8e0f944d28f5e2`.
- Authoritative remote baseline at start:
  `origin/main=e2d69ebee71283287b711811fdfac34370f90b53`.
- Production deploy for baseline: GitHub Actions run
  `31866178879`, successful, completed 2026-08-15 05:28:14Z.
- Marketplace automation packaging: commit
  `3637cb76178d3270cddac518f8c4c7399d5416df` (#1793), deploy run
  `31862656911`, successful, completed 2026-08-15 04:06:21Z.
- Routine reviewer passthrough: PR #1794, externally owned. Remote head at
  inventory time was `7058bb8823c448f31c08f698ce6a9c594c5d5b01`, open with successful
  Backend CI and Codex review. Do not claim deployed until its merge SHA and
  successful `deploy` run are recorded here.

## Browser sessions

| Session | Environment | 2026-08-15 evidence |
|---|---|---|
| `isol8-founder` | production | Authenticated as Isol8 Admin; `/settings/connections?connected=google` rendered the founder integration inventory. |
| `isol8-publisher` | production | Authenticated as the same Isol8 Admin founder company. The visible Marketplace `My listings` view reports 0 active listings and 0 total installs, while the eight curated packages are owned by a different seller identity with byline `@isol8`. Reauthenticate this session as that seller before any vNext listing mutation. |
| `isol8-dev` | development | Authenticated as disposable dev identity `Codex Loops` using the permitted `+clerk_test` convention. Authenticated API and final UI evidence show a clean company with exactly Friday and Atlas, both `Seed`, and a connected gateway. Preserve the session for clean-company marketplace tests. |

## Founder integration baseline

Observed through the production Connections UI on 2026-08-15.

- Connected: Stripe, Google Workspace, GitHub, PostHog, X, AWS, Meta Ads,
  Instagram.
- Not connected: Notion, Sentry, Linear, Attio, Intercom, Parallel, Vercel.
- Google accounts: `admin@isol8.co` and `support@isol8.co`.
- No agent was assigned either Google account at inventory time.
- AWS UI states: connected through an AWS role with read-only describe/list
  access. Its exact Cost Explorer permissions still require command evidence.

## Founder roster baseline

The production UI showed 11 agents. Names visible in the live Connections
assignment list:

- Friday
- Atlas
- Pulse
- Thread
- Stitch
- jarvis
- Tally
- Lens
- Vera
- Platform Operations & Reliability
- Product Analytics & Company Scorecard

Disposition is not yet executed. Preserve useful issue/work-product history
before deletion. Target roster remains Friday, Atlas, Forge, Pulse, Vera,
Gauge, Tally.

| Agent | ID | Seeded | Disposition |
|---|---|---:|---|
| Friday | `d94a4048-8ab7-4ad9-84b1-df777dc93a35` | yes | Keep; CEO and company/product control. |
| Atlas | `06f77598-eea9-4181-92f7-06ca5acb38e5` | yes | Keep; set dormant after verifying available status control. |
| Pulse | `1e6a8a8e-06f1-4257-8762-e913be9360e7` | no | Keep until clean-install validation, then replace with marketplace vNext. |
| Vera | `c82adb37-f67b-43ea-862b-0a4a315ec452` | no | Keep until clean-install validation, then replace with marketplace vNext. |
| Tally | `bb7be85d-c645-4cfd-8980-746ef7bb900d` | no | Keep until clean-install validation, then replace with marketplace vNext. |
| Thread | `4c6f38a5-a13b-4d7e-a842-226e7192120d` | no | Delete after lifecycle proof; no open non-routine work found. |
| Stitch | `69393575-4b47-4a1a-985a-7b8b58cb23d7` | no | Delete after archiving its obsolete prospect routine; no open non-routine work found. |
| jarvis | `b48ae2de-8a18-4e0f-aa1e-ae480f2aa5f0` | no | Reassign `ISOA-31`, `ISOA-44`, and `ISOA-82`, archive its routine, then delete. |
| Lens | `c648083b-4938-4685-adfb-f282ba6ba83b` | no | Delete after lifecycle proof; no open non-routine work found. |
| Platform Operations & Reliability | `db1516d1-f79b-41a0-8c14-c673783e3b10` | no | Reassign `ISOA-65` and `ISOA-86`, then delete. |
| Product Analytics & Company Scorecard | `f024fa76-2cbe-48ce-bbe1-95af93e329ee` | no | Reassign `ISOA-87`, then delete; Gauge replaces this function. |

The deployed API now exposes `Seed | Marketplace | Custom`. A 2026-08-15
authenticated production read reports Friday and Atlas as `Seed` and all nine
non-seed agents as `Custom`, including Pulse, Vera, and Tally. The latter three
must therefore be replaced by clean marketplace installs; a name match is not
marketplace provenance.

## Live-object migration inventory

| Object class | Evidence state | Required next evidence |
|---|---|---|
| Agents | 11 names/IDs and open non-routine work recorded | Verify one symmetric UI deletion before executing remaining dispositions |
| Routines | Six exact rows recorded; every routine is paused | Preserve prior runs, then archive/replace under the 13-routine contract |
| Goals | Two exact rows recorded | Convert `Grow isol8.co` into the measurable 90-day GTM goal; preserve angel-round goal |
| Projects | One exact row recorded | Create six finite GTM projects; retain old broad project until open work is migrated |
| Marketplace versions | Source/published audit in progress | Listing IDs, slugs, versions, seller, workflow contents, retirement disposition |
| Dev clean company | Not authenticated | Proven dev identity and clean-install company |

### Existing goals and projects

- `06039473-42eb-4d55-8f8e-0251a949236c`: `Grow isol8.co`, active company
  goal, no description. Convert in place to the explicit 90-day GTM target so
  existing goal/routine links are preserved and no duplicate growth goal is
  created.
- `f3eba54e-9ae1-44af-8ddb-5bf4acedc54c`: `Start angel round in November`,
  active company goal. Keep; it is a distinct outcome.
- `ee8f7deb-7d18-4118-8db9-baef1935f735`: `Launch and operate Isol8`, in
  progress and linked to the old growth goal. Keep until live issues are moved
  into the six finite projects or explicitly closed; then close the broad
  project rather than let it duplicate the new portfolio.

### GTM goal and finite projects created

The existing growth goal was converted in place through the production UI on
2026-08-15 so its earlier links remained valid:

- Goal `06039473-42eb-4d55-8f8e-0251a949236c`: `Reach 50 paying customers
  with 65% activation in 90 days`, company level, active.
- Description contains the 2026-08-15 through 2026-11-13 outcome, D30/D60/D90
  trial, activation, paid, customer, and armed-automation checkpoints.
- It names `signup_started -> chat_completed` as the controlling activation
  metric, the stop-marketing rule, PostHog/Stripe/Paperclip evidence sources,
  the `onboarding_ready` exclusion, founder authority edges, and the USD
  2,000/month spend ceiling.
- Post-save UI and authenticated `GET /teams/goals/{id}` both verified the
  persisted title and multiline description.

Six projects were created through the production Projects UI, then their status
and goal link were set through each project's visible property controls:

| Project | ID | Status |
|---|---|---|
| Demo and public proof | `2dd21675-6c40-45fb-bcfe-fc46e839a8de` | `in_progress` |
| First outbound cohort | `66009835-bbe8-477a-8d23-572cfb9f3593` | `in_progress` |
| Technical launch content | `523568ff-88e5-44b5-97e1-c62928a0e8ed` | `in_progress` |
| Integration directories | `fcfb3027-c6f3-4864-ad89-4a0293b1e90a` | `planned` |
| Show HN launch | `313a86e4-a5b0-41e2-b295-61b8f0e51956` | `planned` |
| Customer discovery | `378f6865-0ad5-4c89-b101-dfc04cf12721` | `in_progress` |

All six have `goalIds=[06039473-42eb-4d55-8f8e-0251a949236c]`, verified by a
post-save authenticated list read. Evidence-backed child issues, agent/human
owners, and reviewers still need to be created before the GTM portfolio is
complete.

### Existing routines

Every routine row is currently `paused`, although each attached trigger still
has `enabled=true`. All schedules use `UTC`, and none has auto-pause enabled or
an auto-pause threshold. The simultaneous status updates were recorded around
2026-08-15 05:21Z; current state takes precedence over the earlier handoff.

| Routine | ID | Assignee | Trigger | Disposition |
|---|---|---|---|---|
| Prospect signal sweep | `8627378a-bcbc-400e-8315-f9191fc73b74` | Stitch | `0 13 * * 1-5`, UTC | Preserve history, archive after Pulse replacement. |
| Nightly CEO progress | `a2f4a0bd-45ed-44b4-b4b4-825b63a32367` | Friday | `0 8 * * *`, UTC | Preserve history; replace with corrected company-control contract. |
| GitHub: Issue opened or changed | `8cb4435f-37b7-45c1-8772-9224feda62a6` | jarvis | GitHub webhook | Preserve history, archive after Forge replacement. |
| Discoverability check | `3e510ed5-410a-4ca8-a384-827b5ac9e1d7` | Pulse | `0 9 * * 1`, UTC | Preserve history; replace with new SEO/content contract. |
| Community signal watch | `43b41aa1-beb6-4143-90ef-916a5142a9a4` | Pulse | `0 7 * * *`, UTC | Preserve history; replace with new distribution contract. |
| Google: New email (checked on a schedule) | `45038be6-8bb0-47d0-9cab-36977e3ba52f` | Vera | `*/10 * * * *`, UTC | Archive. Never reactivate polling; replace with Gmail push. |

### Open-work reassignment before deletion

- jarvis: `ISOA-31` (`in_review`), `ISOA-44` (`todo`), and `ISOA-82`
  (`in_review`) must move to Friday before jarvis deletion, preserving their
  review and parent relationships.
- Platform Operations & Reliability: `ISOA-65` and `ISOA-86` are backlog and
  must move to Friday before deletion.
- Product Analytics & Company Scorecard: `ISOA-87` is backlog and must move to
  Friday before deletion.
- Pulse: `ISOA-88` remains assigned while Pulse is retained for replacement.
- Other listed deletion candidates have no open non-routine issue in the
  `excludeRoutineExecutions=true` inventory.

## Verification log

Add entries chronologically. Each completed implementation must record its PR,
tests, Codex review, merge SHA, deploy run, UI proof, and runtime proof.

- 2026-08-15: Verified the three named Playwright sessions. Founder and
  publisher sessions were reachable; dev authentication was not proven.
- 2026-08-15: Verified both Google mailboxes exist in production and that Vera
  was not yet assigned `support@isol8.co`.
- 2026-08-15: Verified #1793 reached production via successful deploy run
  `31862656911`.
- 2026-08-15: Production UI and authenticated BFF read both showed six legacy
  routine rows, all paused. The BFF read supplied exact routine, trigger,
  agent, goal, project, and open-issue identifiers after the UI objects were
  visibly confirmed.
- 2026-08-15: Converted the existing growth goal through the production UI and
  verified the exact persisted definition through the authenticated BFF.
- 2026-08-15: Created all six finite GTM projects through production dialogs,
  linked them to the measurable goal through the project goal picker, set four
  active and two planned states, and verified IDs/status/links through a
  post-save authenticated list read.
- 2026-08-15: Created seven finite, high-priority, unassigned `Todo` issues
  through the production task dialog so no legacy agent woke before roster
  replacement: ISOA-115 real 45-second demo, ISOA-116 public read-only board,
  ISOA-117 first 50-founder outbound cohort, ISOA-118 verified webhook event
  reference, ISOA-119 Sentry/Linear directory submissions, ISOA-120 Show HN,
  and ISOA-121 five activation/churn interviews. Each issue is attached to its
  named GTM project and the 50-customer/65%-activation goal. The production UI
  showed all seven persisted as `Todo` and `Unassigned`; issue-create requests
  returned 200. Descriptions distinguish agent preparation from the founder's
  irreducible first-touch, interview, disclosure, and publication authority.
- 2026-08-15: Friday delegation-boundary PR #1796 passed primary review,
  Backend CI run `31868710323`, and Claude review. `@codex review` was
  requested but the connector declined because its review quota was
  exhausted. PR #1796 was squash-merged as
  `75ecfa8716320f21a39932ab5cb2ec96a268f974`; deploy run `31871896168`
  completed successfully, including the production service and final Vercel
  jobs. The deployment alone did not mutate the already-provisioned founder
  workspace. The production admin `Regenerate config` flow then succeeded
  while restoring runtime `skills` and `wizard` subtrees. Read-only ECS proof
  showed the legacy `pick ONE task ... execute it` text absent and the new
  `CEO/company-control action`, `COMPANY-CONSTRAINT-TAKEN`, and
  `NO-EXECUTABLE-COMPANY-CONSTRAINT` anchors present in Friday's live
  `AGENTS.md`.
- 2026-08-15: Agent-facing Paperclip primitives PR #1797 was built at head
  `98863f3fcb2e4ea8458fafccc527fb92bfa987e0`. Local evidence is 6,470 unit
  tests passed with 7 skipped, 94 focused proxy tests passed, and 5 frontend
  primitive tests passed. Claude review passed; `@codex review` was requested
  but quota-blocked. The deployed Paperclip fork proves the unified attention
  feed is board-only, so the skill documents decisions/queues as agent inputs
  to attention rather than falsely claiming an agent-read API. PR #1797 was
  squash-merged as `c935eff8a114ec2cfad8d150eed91b9c54545582`;
  deployment run `31872851837` completed successfully through the production
  service, observability, and final Vercel jobs. Image build run `31872851843`
  also succeeded and published tag `2026.7.2-isol8.4-c935eff` at digest
  `sha256:8a39ad18da59c2b93861143833d896919ebe7829870664595475fa6fcb568115`.
  Runtime use still requires the explicit image pin and per-owner roll below.
- 2026-08-15: No external platform-ops token-removal PR was visible yet. A
  tested local fallback exists at `d172f4069` but is intentionally unpushed;
  it must not be published while the founder's external agent owns that work.
- 2026-08-15: Agent provenance and symmetric lifecycle PR #1799 was built at
  head `c46e40901120984db81361bf1e8842f4c39fb74f`. Primary review found and
  fixed two asymmetries: approval-driven hires omitted `listing_id` and were
  stamped Custom, and a deletion retry could not converge when OpenClaw had
  already removed the runtime agent. The focused post-fix service suite passed
  87 tests; Ruff, format-check, and diff-check passed. Frontend CI, full Backend
  CI, and Claude review passed on the new head.
  `@codex review` was re-requested in comment `5301140224`. PR #1799 was
  squash-merged as `0ccee31285eac46f0c46985724b11af3cd0c876b`; deployment
  run `31873768016` is in progress and must succeed before roster deletion.
- 2026-08-15: Explicit image-pin PR #1802 is open at head `479c467d6`. It
  changes both environment defaults to the already-built
  `2026.7.2-isol8.4-c935eff` image and records the complete skill-only delta.
  The JSON validates against `openclaw-version.schema.json`; 42 focused
  backend version/admin tests and 28 container-stack tests passed, and
  diff-check is clean. `@codex review` was requested in comment `5301301999`.
  The pin changes no existing owner; the founder container must be rolled
  explicitly after merge and deployment.
- 2026-08-15: Read-only ECS inspection found 19 top-level agent workspaces but
  only 11 live OpenClaw agent entries. The eight unreferenced directories are
  five 88-KB UUID-keyed Jarvis remnants plus `workspace-jarvis` (436 KB),
  `workspace-product-analytics-scorecard` (76 KB), and
  `workspace-platform-ops-reliability` (44 KB). None has a `.template`
  marketplace sidecar. PR #1799 correctly prevents this leak for future
  exact-ID deletions; it does not guess that an unreferenced historical
  workspace is disposable. Preserve these paths until one explicit destructive
  founder approval authorizes the bounded one-time cleanup.
- 2026-08-15: AWS-integration CloudWatch alarm transport PR #1801 is open at
  head `d0d38f61f`. It creates an ALARM-only EventBridge API Destination in the
  owner's connected region through the existing Settings AWS role, persists
  only secret-free lifecycle names, and cleans external resources before
  routine/trigger deletion, AWS disconnect, or account wipe. Verification:
  6,636 backend unit tests passed with 7 skipped; 276 chaos API tests passed
  with 286 skipped; 5 focused EventBridge tests and 39 focused RoutinesPanel
  tests passed; frontend type-check, focused infra template test, AWS
  CloudFormation validation, Ruff, and diff-check passed. SDK-shape review
  caught and fixed an invalid `DeleteConnection` argument before commit.
  `@codex review` was requested in comment `5301117545`. The PR remains
  unmerged and no live alarm delivery is claimed until deploy, connector-stack
  update, UI creation, and a controlled CloudWatch transition all pass.
- 2026-08-15: The production seller ownership check used the visible
  Marketplace drawer. The logged-in founder identity owns zero listings; the
  live Vera, Tally, Stitch, Pulse, Lens, Thread, Ora, and Echo packages render
  with byline `@isol8` under another seller identity. Existing immutable
  versions cannot be revised from the current session. A read-only production
  seller-index query resolved `@isol8` to
  `user_3DBS55XwZuB7LQIfWvwsvYWMFaM`. The authenticated admin UI shows that
  user has no Clerk identity, no billing record, and no container. The prior
  instruction to reauthenticate as that seller is therefore not executable;
  preserving listing lineage now requires an explicit ownership-transfer
  decision, or the founder must accept fresh canonical listings under the
  current publisher identity.
- 2026-08-15: Native routine reviewer support is deployed end to end. Paperclip
  fork PR #19 merged as `556cd64e1de6bf35e65bcb5140b2e38b6284b5fd`;
  Isol8 pins/promotes #1798 and #1800 reached production in deploy run
  `31870847605`. Production Paperclip is stable on task definition revision 31.
  Routine reviewer policy now persists, dispatches, validates live reviewers,
  records dead-reviewer failures, and participates in revision restore and
  circuit-breaker handling.
- 2026-08-15: Agent provenance/lifecycle PR #1799 deployed successfully in run
  `31873768016` as main commit
  `0ccee31285eac46f0c46985724b11af3cd0c876b`. Explicit image-pin PR #1802
  merged as `79baf9ce0f2a7447744a16393970eee18fbe8db3`; build run
  `31875008230` and deploy run `31875008293` succeeded. The founder service was
  rolled through the production admin control onto task definition
  `isol8-prod-openclaw-user:189`, task `d58d22e5f69b46a98ca3fd4039c7a303`,
  image `2026.7.2-isol8.4-c935eff`. ECS reports rollout `COMPLETED`, one running,
  zero pending and zero failed; an explicit Playwright reload shows the same
  task and `running` status in the production admin UI. ECS Exec against that
  exact task proved the live Paperclip helper exposes decision/stats/get,
  company search/extract, queue/triage, and safe issue/subtask commands.
- 2026-08-15: The first customer-path deletion test targeted Lens through the
  production inspector. Paperclip preservation successfully patched routines
  `ead36d5e-4279-4ebb-9fc6-c1094498ad7c` and
  `df942fc8-8fa7-414a-9e9e-59bfcec06184`, then OpenClaw rejected
  `agents.delete` with stable `code=file_lock_timeout`; request
  `a1ce453d-6308-4de9-9120-e60d38129548` returned 500 and Lens remained
  visible. PR #1803 (`fix/agent-delete-lock-retry`, corrected head
  `4e27ae276a27924ea987b732d39f7b65500ba67f`) adds two bounded retries only
  for OpenClaw's stable lock code, keeps all unrelated errors and ambiguous
  RPC timeouts fail-closed, and proves cleanup does not advance after
  exhaustion. Claude review correctly found that head `573651867` covered the
  REST route but not the WebSocket route used by the UI. The corrected head
  routes both paths through one helper while preserving the browser request
  ID. Corrected-head evidence is 68 focused tests, 154 complete
  WebSocket/lifecycle tests, and the full backend unit suite with 6,646 passed
  and 7 skipped; Ruff, formatting, and diff checks pass. Backend CI and Claude
  review are running on the corrected head; `@codex review` was re-requested.
- 2026-08-15: Authenticated production inventory returned 11 agents and six
  paused routines. Six non-terminal manual issues belong to deletion
  candidates and are protected by the lifecycle reassignment: `ISOA-31`,
  `ISOA-44`, `ISOA-82` (jarvis), `ISOA-65`, `ISOA-86` (Platform Operations),
  and `ISOA-87` (Product Analytics). The public marketplace inventory contains
  exactly eight published OpenClaw listings, all under orphan seller
  `user_3DBS55XwZuB7LQIfWvwsvYWMFaM`: Vera v9, Tally v8, Stitch v9, Pulse v9,
  Lens v9, Thread v3, Ora v3, and Echo v3. Forge and Gauge do not yet exist.
- 2026-08-15: Atlas was paused through the production agent inspector. The
  inspector changed its action to `Resume`; a post-mutation authenticated
  `GET /api/v1/teams/agents` returned Atlas with `status=paused` and
  `origin=Seed`. Atlas remains installed as the dormant recruiter and no
  routine may target it unless a deliberate hiring gap reactivates it.
- 2026-08-15: The exact Cost Explorer capability check succeeded inside the
  live founder task when run through the materialized `isol8-connector`
  profile. `ce:GetCostAndUsage` returned estimated unblended cost
  `185.4606969874 USD` for 2026-08-01 through 2026-08-07. The same command
  without the connector profile was denied as the container's base ECS task
  role, proving the integration credentials remain isolated from the runtime
  role. The existing AWS connection is sufficient for finance and reliability
  reads; no platform-ops token or AWS reconnect is required.
- 2026-08-15: A later customer-path smoke test selected the production Tally
  chat and requested exactly one read-only Cost Explorer call through
  `AWS_PROFILE=isol8-connector` in `us-east-1`. It succeeded with estimated
  2026-08-01 through 2026-08-16 unblended AWS spend of `475.0508907665 USD`.
  Fifteen elapsed billing days project to `981.77 USD` over 31 days, leaving
  `1,018.23 USD` of the `$2,000` ceiling. The earlier same-day snapshot was
  `465.4811232512 USD`; estimates accrue during the day, so every finance
  artifact must include its observation time. This covers AWS only and remains
  below the `$1,600` known-spend warning. Tally must retain the incomplete-source
  refusal until fixed non-AWS commitments are supplied. The legacy Tally also
  prefaced the bounded result with unrelated Bootstrap dependencies for Google,
  Bank of Bots, QuickBooks, and Xero; do not carry that package behavior into
  the clean finance contract.
- 2026-08-15: Read-only IAM inventory shows the connected
  `isol8-connector` role already has AWS-managed `PowerUserAccess`; outbound
  finance and diagnostic reads need no reconnect or scope upgrade. The
  dedicated EventBridge invoke role is absent, but alarm transport is deferred
  to the final provider-transport phase and is not a current dependency.
- 2026-08-15: The permitted dev-only sign-up was completed in `isol8-dev`.
  Authenticated `GET /api/v1/teams/agents` returned exactly Friday and Atlas,
  both `origin=Seed`. During a deployment replacement the status endpoint
  reported `container_substatus=restarting`; ECS then showed task
  `655324c7f9dc41e1a6c065a8f1351b9c` running on task definition
  `isol8-dev-openclaw-user:162`. The prior healthy task was stopped by the ECS
  service scheduler, not by an application crash. After the rollout, backend
  logs showed the stale frontend connection cleared, the status-poll self-heal
  dial established the gateway in `207 ms`, and a new browser WebSocket began
  forwarding `agents.list`. A fresh UI snapshot then showed `Connected` and
  exactly Friday and Atlas. This company is ready for marketplace smoke tests.
- 2026-08-15: Automation-assignee picker repair PR #1804 is open at head
  `aa92536cb9b7b88a1b56ef9c187442826523ef43`. It commits primary mouse
  selection before the search field can blur and the portal can rerender under
  the pointer, and adds Arrow/Enter/Escape combobox behavior. Local evidence is
  the full frontend suite with 379 files and 2,685 tests passed, 91 focused
  picker/routine/issue tests passed, TypeScript and ESLint clean, and
  diff-check clean. Frontend CI passed; Claude review is running. The required
  `@codex review` was requested and quota-blocked.
- 2026-08-15: Agent-delete lock retry PR #1803 passed Backend CI and Claude
  review, was squash-merged as
  `d4588741978214cbe22e44d26811f4dfbfda9e8b`, and reached production in
  successful deploy run `31880709935`. A second Lens deletion through the
  production inspector proved both retry delays executed, but all three
  attempts ended with OpenClaw `code=file_lock_timeout`; request
  `cb513a7a-bf57-44f2-8918-59f6f20a931c` returned 500 and Lens remained
  visible. Read-only ECS Exec then found
  `/home/node/.openclaw/openclaw.json.lock` as a zero-byte `node:node` file
  dated 2026-08-11, days before the current task. Pinned OpenClaw creates the
  sidecar exclusively before writing owner JSON; a process death in that gap
  leaves a malformed file its conservative runtime reclaimer cannot remove.
  This proves bounded retries are correct for transient contention but cannot
  repair a durable malformed lock.
- 2026-08-15: Automation-assignee picker PR #1804 reached final head
  `e5c1344ab009cc4698bee76d4a9159a48529d069` after adding stable filtered
  pointer selection, suppression of the trailing portal click, keyboard/ARIA
  combobox behavior, and current/unique-match activation. Twelve focused
  tests, the complete frontend suite (379 files, 2,688 tests), TypeScript,
  focused ESLint, Frontend CI, and Claude review passed. `@codex review` was
  requested on the final head and quota-blocked. It was squash-merged as
  `aaf93bc3f2c1c3c70212cca4edcd74461a25cee3`; deploy run `31881866070`
  completed successfully. Production UI proof used a temporary scheduled
  routine that was paused before testing and never ran. After reloading the
  browser onto the deployed bundle, filtering to Atlas exposed only
  `No assignee` and Atlas, marked Atlas as the active ARIA option, and `Enter`
  selected Atlas. A mouse selection also persisted the correct Atlas ID. The
  temporary routine was archived, and a no-save check against `Community
  signal watch` confirmed Cancel preserved its Pulse assignment.
- 2026-08-15: AWS-integration alarm transport PR #1801 was rebased onto the
  latest main and pushed at head
  `082e34e381510aa33b8940660a6163368a642136`. Post-rebase evidence: the full
  backend unit suite passed 6,663 tests with 7 skipped; the changed frontend
  routine suite and type-check passed; the focused infra template suite
  passed. Chaos API completed with 275 passed and 286 skipped; its sole
  failure was Schemathesis generating a carriage-return header for unrelated
  `POST /api/v1/ws/disconnect`, rejected by `requests` before the app received
  it. `@codex review` was requested again in comment `5302007645`; hosted CI
  and Claude review are running on this exact head.
- 2026-08-15: Durable OpenClaw config-lock recovery PR #1805 is open at head
  `f518ba6fbc339bd28039143c5d83274a174d2582`. It removes only the fixed
  `openclaw.json.lock` sidecar before doctor/gateway starts, and refuses to
  delete an unexpected non-regular or symlink path. This boundary is safe
  because every per-owner service already enforces `maximumPercent=100` and
  `minimumHealthyPercent=0`: the prior task is stopped before the replacement
  task reaches its startup command, so no OpenClaw process can own the
  sidecar. Both-environment `ContainerStack` tests passed 29/29, the exact
  POSIX guard passed `sh -n`, and diff-check passed. The repository-wide infra
  run reached unrelated Lambda bundling that requires the unavailable local
  Docker daemon and blocked PyPI, after the focused stack suite had passed.
  Claude review completed with no correctness findings; `@codex review` was
  requested in comment `5302020181` and quota-blocked. No hosted infra test is
  configured for this path, and a managed production approval gate rejected
  the merge until the founder explicitly approves proceeding with the focused
  29/29 stack evidence plus the documented Docker/PyPI limitation. After
  deployment the founder container must be reprovisioned through the existing
  admin UI, then Lens deletion retried.
- 2026-08-15: The customer-facing blank-agent flow was exercised without a
  successful mutation. Creating Forge is currently blocked by the workspace's
  10-specialist plan limit (`This workspace is at its 10-agent limit`), so
  roster deletion is also a hard prerequisite for the marketplace foundry,
  not just final cleanup. The dialog was cancelled and no agent was created.
  The production publisher drawer was then opened read-only and closed without
  saving: the current founder identity has no marketplace handle and must pick
  one before publishing, while the historical `@isol8` handle belongs to the
  orphaned seller identity. The drawer correctly lists Friday as not
  publishable and the other current agents as selectable.
- 2026-08-15: All six superseded production automations were individually
  archived through the customer UI while paused: `Community signal watch`,
  `Discoverability check`, `GitHub: Issue opened or changed`, `Google: New
  email (checked on a schedule)`, `Nightly CEO progress`, and `Prospect signal
  sweep`. A final production snapshot shows `No automations yet.` None was run
  or activated during cleanup; their historical run issues remain in
  Paperclip while the active routine surface is clean for the 13 contracts.
- 2026-08-15: Open work was removed from agents slated for deletion without
  creating hidden execution. A first direct reassignment of in-review
  `ISOA-31` proved the Paperclip assignment wake immediately: Friday run
  `d310a7bb-cbba-4438-91db-ebfe36f435ac` started. Its linked PR #1715 is
  closed unmerged and the task is superseded by the new reliability contract,
  so the task was marked Cancelled and the run became cancelled. `ISOA-44` and
  `ISOA-82` were then moved to Backlog before reassignment to Friday; their run
  counts stayed 2→2 and 1→1. Already-backlogged `ISOA-65`, `ISOA-86`, and
  `ISOA-87` were reassigned to Friday with run counts 0→0. No remaining useful
  nonterminal issue is owned by jarvis, Platform Operations, or Product
  Analytics, and the backlog-first ordering prevents an avoidable inference
  wake during later agent deletion.

- 2026-08-15: A read-only founder-container audit established the minimal
  package boundary for the replacement marketplace agents. GitHub, Gmail,
  Isol8/Paperclip, browser, and coding skills are image-global; Stripe,
  PostHog, and Meta are shared MCP connections; AWS is exposed through the
  buyer-authorized `isol8-connector` profile. No current agent has a per-agent
  model or skill allow-list. The legacy Pulse, Vera, and Tally workspaces do
  contain copied Lobster, n8n, Taskr, CRM, bookkeeping, Slack, and private
  specialist skills, but none is needed for the target operating contracts.
  The clean package specification is now frozen in
  `2026-08-15-marketplace-agent-contracts.md`: core identity files only, no
  private workflow/state/credential material, no heartbeat business scan,
  inherited company model, dormant schedules, pending webhooks, and
  company-local reviewers assigned only after install. Firecrawl/Parallel
  result quality remains an explicit pre-arm smoke gate for Pulse rather than
  an assumed capability.
- 2026-08-15: Friday's replacement control routines were created through the
  production customer UI and paused before any run. `Daily company control`
  is routine `17338ee2-2ad0-461f-bf5b-2b4b4efdea93`, scheduled
  `0 8 * * *` in `America/Aruba`; it persists `{{GTM_GOAL_ID}}` with default
  `06039473-42eb-4d55-8f8e-0251a949236c` and numeric
  `{{MONTHLY_SPEND_CAP_USD}}=2000`. `Weekly product/customer synthesis` is
  routine `741aaa81-8949-4120-87ea-d17a31293e2c`, scheduled
  `0 11 * * 1` in `America/Aruba`; it persists the same goal plus an ordered
  comma-separated `{{GTM_PROJECT_IDS}}` default for the six finite projects.
  Reloaded edit forms proved every default persisted; both detail pages showed
  `Paused` and `No runs yet`. The initial plain variable names were correctly
  removed by Paperclip's `syncRoutineVariablesWithTemplate`; definitions must
  be referenced as `{{NAME}}`, a constraint the UI currently does not explain.
- 2026-08-15: Bounded reads of the exact production routine-detail responses
  verified the hidden execution fields for both Friday routines. Each is
  `paused`, assigned to Friday `d94a4048-8ab7-4ad9-84b1-df777dc93a35`, has
  `consecutiveFailureCount=0`, `concurrencyPolicy=coalesce_if_active`,
  `catchUpPolicy=skip_missed`, and an enabled schedule trigger with the
  recorded cron and `America/Aruba` timezone. The daily and weekly variable
  definitions and defaults match the values above. `executionPolicy` and
  `parentIssueId` are still null because no reviewer stage or permanent parent
  is configured. `autoPauseEnabled` and `autoPauseThreshold` are also null,
  which is inheritance rather than disabled behavior: pinned Paperclip commit
  `57d4d68e` resolves null routine values through instance general settings
  (`server/src/services/routine-circuit-breaker.ts:48-51`), whose normalized
  defaults are enabled with threshold three
  (`server/src/services/instance-settings.ts:197-217`). The customer proxy
  returns `403 Route not permitted` for the instance-general-settings route,
  so this evidence proves the pinned effective default path but does not claim
  a customer-visible per-routine override.
- 2026-08-15: Customer-path setup also exposed two remaining routine-editor
  gaps. PR #1794 made `executionPolicy` pass through the BFF, but neither the
  create nor edit UI presents a routine reviewer, so Gauge cannot yet be set as
  Friday's native review stage through the customer surface; this is filed as
  #1808. Also, seven variable rows make the modal footer fall outside a
  1280x720 viewport and the
  Save button cannot be clicked or scrolled into view; the production weekly
  contract was simplified to two variables, which is the better operating
  shape independently of that layout defect. The missing `{{NAME}}` validation
  and modal overflow are filed together as #1807.
- 2026-08-15: External reliability PR #1806 was monitored rather than
  duplicated. Its current patch adds global backend settings for one Paperclip
  company, one Jarvis agent, and a static Paperclip key, then creates issues
  directly. No infrastructure change wires those values, and the path bypasses
  routine runs/review/circuit breaking, marketplace packaging, buyer-local
  identities, the Settings AWS connection, and the approved Forge owner in
  #1801. A blocking evidence comment was posted as PR comment `5302266226`;
  #1806 remains open and unmerged.
- 2026-08-15: The clean dev company was used to exercise the blank-agent
  customer path for Forge. Both bounded Create attempts failed before mutation
  with `Connect call failed ('10.0.3.64', 18789)` and the roster remained
  Friday/Atlas. AWS read-only evidence showed that exact address belonged to a
  newly launched dev task
  `63673393c2fa4771b5e0095149faee1a` for service
  `openclaw-user_3Hwnrt08fmjmk5c46lV1BzQQEfo-00fb1677f007`, started at the
  first attempt and still `RUNNING` with health `UNKNOWN`; the dev backend and
  Paperclip tasks were healthy. The customer sidebar still showed only the
  seed roster, so the retry was deferred until that exact task was
  demonstrably ready. The later cross-system audit below supersedes the
  initial inference that no Paperclip-side mutation occurred.
- 2026-08-15: The same dev task and its OpenClaw container later reported
  `HEALTHY`, and one bounded customer-path retry completed. It exposed a more
  serious split state: the sidebar showed only `Forge 3`, while the
  authenticated teams-agent response contained `Forge`, `Forge 2`, and
  `Forge 3`. Their creation order matches the two earlier connection-refused
  responses and the later successful pairing. CloudWatch confirms both failed
  requests reached Paperclip create before failing in `pair_with_openclaw`;
  the third completed OpenClaw pairing, device approval, and API-key setup.
  The current interceptor deliberately returns `ok:false` without reconciling
  the already-created Paperclip record, and its unit test pins that orphan
  behavior. Sanitized issue #1809 records the blocker. Do not create further
  target agents in production until one logical create converges to one
  Paperclip and one OpenClaw identity across retries.
- 2026-08-15: The production Connections UI showed both Google accounts and,
  after the agent roster finished loading, exposed the per-agent account
  selectors delivered by #1792. Vera was changed from `Default` to
  `support@isol8.co` through that customer dropdown. The immediate summary
  changed to `1 agent acts as this account`, and a full page reload preserved
  Vera's selected account. `admin@isol8.co` remains unassigned. This proves
  multi-account assignment persistence, not Gmail push readiness or that the
  support address is a Workspace mailbox rather than a Group. Reassign the
  account to the clean marketplace Vera's new ID during roster replacement;
  perform the mailbox and Pub/Sub smoke test only in the final trigger phase.
- 2026-08-15: Founder sequencing correction: do not rely on EventBridge during
  the current build. Forge and Tally must use only the already-connected
  Settings AWS integration for outbound reads. PR #1801 remains open and
  unmerged; its connector-stack update and controlled alarm-delivery proof move
  to the final provider-transport phase alongside Gmail Pub/Sub. The Forge
  reliability routine may be created paused, but its alarm trigger must not be
  armed and #1801 is not a current critical-path dependency.

## Founder actions

Record only precise actions that cannot be completed without reserved founder
authority.

- Decide whether to transfer the orphaned `@isol8` seller/listing ownership to
  the current founder identity or publish fresh canonical listings. The
  existing seller user has no Clerk identity, so reauthentication cannot
  resolve this blocker.
- At the final provider-transport phase, decide whether to deploy #1801. Only if
  deployed, apply the latest versioned Isol8 AWS connector-stack template once;
  the update adds the dedicated invoke role and does not change the selected
  AWS scope.
- Cash, cash as-of date, and fixed non-AWS monthly commitments remain required
  before Tally may calculate runway or complete operating burn.
- Eight unreferenced legacy agent workspaces require one bounded destructive
  cleanup approval after the target roster is live; the exact paths and sizes
  are recorded above.
