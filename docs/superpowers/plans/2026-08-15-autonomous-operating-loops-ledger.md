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
| `isol8-dev` | development | Session rendered the public landing page and was advanced only to the Clerk sign-up form. Automated credential entry was refused under the founder credential boundary, so authenticated dev state is not proven; the founder must complete one allowed `+clerk_test` sign-in before clean-company UI mutations. |

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
| jarvis | `b48ae2de-8a18-4e0f-aa1e-ae480f2aa5f0` | no | Reassign `ISOA-31` and `ISOA-44`, archive its routine, then delete. |
| Lens | `c648083b-4938-4685-adfb-f282ba6ba83b` | no | Delete after lifecycle proof; no open non-routine work found. |
| Platform Operations & Reliability | `db1516d1-f79b-41a0-8c14-c673783e3b10` | no | Reassign `ISOA-65` and `ISOA-86`, then delete. |
| Product Analytics & Company Scorecard | `f024fa76-2cbe-48ce-bbe1-95af93e329ee` | no | Reassign `ISOA-87`, then delete; Gauge replaces this function. |

The current API exposes only `seeded: bool`; it does not yet expose the
required `Seed | Marketplace | Custom` origin or canonical listing provenance.

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

- jarvis: `ISOA-31` (`in_review`) and `ISOA-44` (`todo`) must move to Friday
  before jarvis deletion, preserving their review and parent relationships.
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
  jobs.
- 2026-08-15: Agent-facing Paperclip primitives PR #1797 was built at head
  `98863f3fcb2e4ea8458fafccc527fb92bfa987e0`. Local evidence is 6,470 unit
  tests passed with 7 skipped, 94 focused proxy tests passed, and 5 frontend
  primitive tests passed. Claude review passed; `@codex review` was requested
  but quota-blocked. The deployed Paperclip fork proves the unified attention
  feed is board-only, so the skill documents decisions/queues as agent inputs
  to attention rather than falsely claiming an agent-read API. PR #1797 was
  squash-merged as `c935eff8a114ec2cfad8d150eed91b9c54545582`;
  deployment run `31872851837` is in progress and must succeed before runtime
  use.
- 2026-08-15: No external platform-ops token-removal PR was visible yet. A
  tested local fallback exists at `d172f4069` but is intentionally unpushed;
  it must not be published while the founder's external agent owns that work.
- 2026-08-15: Agent provenance and symmetric lifecycle PR #1799 is open at
  head `c46e40901120984db81361bf1e8842f4c39fb74f`. Primary review found and
  fixed two asymmetries: approval-driven hires omitted `listing_id` and were
  stamped Custom, and a deletion retry could not converge when OpenClaw had
  already removed the runtime agent. The focused post-fix service suite passed
  87 tests; Ruff, format-check, and diff-check passed. Frontend CI, full Backend
  CI, and Claude review passed on the new head.
  `@codex review` was re-requested in comment `5301140224`; the PR remains
  unmerged and undeployed.
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
  versions cannot be revised from the current session.

## Founder actions

Record only precise actions that cannot be completed without reserved founder
authority.

- Development browser authentication is currently not proven. Use the allowed
  `+clerk_test` convention only in dev and verify the `424242` code once before
  relying on it.
- Cash, cash as-of date, and fixed non-AWS monthly commitments remain required
  before Tally may calculate runway or complete operating burn.
- Eight unreferenced legacy agent workspaces require one bounded destructive
  cleanup approval after the target roster is live; the exact paths and sizes
  are recorded above.
