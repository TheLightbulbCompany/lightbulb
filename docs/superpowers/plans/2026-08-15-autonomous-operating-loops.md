# Isol8 Autonomous Operating Loops

> Working source of truth, 2026-08-15. This document is intentionally detailed
> enough to execute as a goal. Per `CLAUDE.md`, delete it when the goal closes;
> merged PRs and their descriptions become the permanent implementation record.

## Goal

Operate Isol8 through a six-active-agent, marketplace-backed company covering
eight business functions with 13 production Paperclip routines, explicit agent
provenance, native review/decision/attention/search capabilities, and live
external metrics. Every routine must be smoke-tested and armed. Founder
intervention is limited to credentials, legal or financial authority, and work
whose purpose is founder learning or reputation.

## Business situation and constraint

- Product is in production; distribution and paying usage are near zero.
- The activation north star is `signup_started -> chat_completed`.
- `onboarding_ready` is infrastructure success, not activation.
- Acquisition is attributed with persistent `first_touch_utm_*` properties.
- `onboarding_gate_blocked{code}` names the wall and the affected user.
- Distribution continues at learning volume when activation is weak; Friday
  shifts the company's priority to the activation blocker instead of scaling
  acquisition blindly.
- The loops that operate Isol8 are the product research program and the source
  for marketplace listings. There is no separate dogfood system.

## Settled target

### Roster

| Agent | Origin | Active responsibility |
|---|---|---|
| Friday | Seed | Company control, product prioritization, cross-function merge |
| Atlas | Seed | Installed but dormant; build or recruit an agent only on request |
| Forge | Marketplace, new | Product engineering and platform reliability |
| Pulse | Marketplace, revised | Community distribution, SEO/content, prospecting |
| Vera | Marketplace, revised | Support, sales replies, customer health, revenue recovery |
| Gauge | Marketplace, new | Activation, attribution, product and GTM analytics |
| Tally | Marketplace, revised | Finance, cost, margin, cash freshness |

End state: seven installed agents, six active. Friday and Atlas remain seed
agents. Every active specialist is a marketplace agent. No active custom agent
remains in the founder company.

### Eight business functions

1. Company and product control
2. Engineering and reliability
3. Community distribution
4. SEO and content
5. Sales
6. Customer operations
7. Activation and business analytics
8. Finance

### Counting rule

There are 13 routine contracts. The number of physical trigger registrations
can be greater than 13 because one routine can have multiple triggers. For
example, Vera's revenue-recovery routine listens to several Stripe event types.
Feature work assigned to Forge is a normal Paperclip issue assignment, not a
fourteenth routine.

## Runtime boundaries

### Paperclip owns durable business work

Paperclip owns:

- routines and their schedule/webhook/API triggers;
- issue assignment and cross-agent child issues;
- run history, comments, work products, reviews, and external IDs;
- attention, decisions, decision queues, effects, and `ruleKey` statistics;
- company search and extracted search results;
- the visible state the founder uses to manage the company.

Cross-function work is an assigned `todo` child issue carrying `parentId`.
Examples: Vera creates a child for Forge when support exposes a product defect;
Gauge assigns an activation blocker to Forge or Friday; Tally routes a finance
exception to Friday. OpenClaw subagent sessions are not the business handoff.

### OpenClaw owns agent execution

Each Paperclip agent UUID maps to an OpenClaw agent entry, workspace, session
route, model/tool/skill policy, memory, and runtime configuration. Use these
primitives as follows:

- Workspace files define durable role behavior and operating rules.
- Per-agent models and skills specialize work and control inference cost.
- Forge may request ACP for a coding harness when the connected provider can
  authenticate it.
- OpenClaw subagents may help Forge execute a bounded technical task, but the
  parent Paperclip issue remains the accountable unit of work.
- Periodic maintenance heartbeats do not scan the business. Paperclip routines
  and issue assignments initiate business work.
- Work products and decisions live in Paperclip, not only in workspace memory
  or Lobster state.

Per-agent tool settings are not a credential boundary. Agents share one tenant
container, OS home, CLI keyrings, AWS bundle, X credentials, and container MCP
configuration. Google account assignment is an explicit per-agent identity
instruction, but still uses a shared keyring underneath. Safety claims must not
pretend prompt text or an allowlist prevents another in-container agent from
using a shared CLI credential.

## AWS and platform operations decision

The Settings AWS connection materializes an `isol8-connector` AWS CLI profile
through `credential_process`, giving the container short-lived credentials for
the owner-selected ViewOnly, ReadOnly, or PowerUser tier. This is the normal
capability for Forge operating AWS and for Tally reading buyer-authorized cost
data. The founder connection has already proven it can query Cost Explorer.

The existing `platform_ops` token predates the current Platform Operations
agent. It was designed on 2026-06-30 for Friday and the admin operator console,
but its agent path was never delivered to a container. The founder has decided
not to create a second agent-authentication rail for operations.

Remove the agent-facing `platform_ops` plumbing:

1. Delete the platform-ops token mint/verify service and `/ops/token` endpoint.
2. Remove token acceptance from `/ops/*` and the curated `/admin/*` reads.
3. Remove `PLATFORM_OPS_OWNER_ID`, `PLATFORM_OPS_TOKEN_KEY`, the signing secret,
   task environment wiring, and obsolete GitHub/CDK inputs.
4. Keep human operator endpoints only where the admin UI still calls them, and
   protect those endpoints exclusively with Clerk platform-admin auth. Delete
   any unused duplicate ops route instead of retaining an orphaned API.
5. Delete the token-specific tests and replace them with admin-auth and
   unauthenticated-denial coverage for any retained routes.

Forge and Tally use only the Settings AWS integration. The integration tier and
role remain the owner's explicit choice; this plan does not add a hidden
founder credential or a marketplace-only exception.

The AWS connection is outbound access; it does not by itself push CloudWatch
alarm transitions into a routine. Alarm registration belongs to the AWS
integration setup: configure an EventBridge API Destination to POST signed alarm
events to the routine webhook when the connected role permits it. Otherwise the
marketplace routine remains dormant with an explicit one-time AWS setup step.
This uses the AWS integration and AWS event system, not a platform-ops token.

## Lobster workflow decision

Lobster is an optional deterministic sub-workflow inside an agent run. It is
not the scheduler, business state machine, approval system, notification queue,
review layer, or metric store. Paperclip already owns those responsibilities.

### Production marketplace audit, 2026-08-15

The eight published packages contain 38 Lobster workflows:

| Listing | Version | Workflows | Finding for this goal |
|---|---:|---:|---|
| Pulse | 9 | 3 | Parallel-dependent monitoring/content queues duplicate the new routines |
| Vera | 9 | 4 | Zendesk/Intercom/Parallel flows do not match Gmail-first customer operations |
| Tally | 8 | 4 | Plaid/expense/month-end system is much broader than the current finance loop |
| Stitch | 9 | 14 | Depends on an absent CRM/prospect/outbound stack; not part of target roster |
| Lens | 9 | 3 | Not part of target roster; workflows require a separate validation decision |
| Thread | 3 | 4 | Inbox/briefing behavior is folded into Vera and Paperclip attention |
| Ora | 3 | 4 | Outside this operating roster; retain only after its own clean-tenant audit |
| Echo | 3 | 2 | Outside this operating roster; retain only after its own clean-tenant audit |

The Pulse, Vera, Tally, Stitch, Thread, Ora, and Echo packages also contain
workspace-ID-bound `cwd` values or private notification/state queues. Those
constructs must not be copied into the new target packages without a portable
deployment test.

### Target package policy

- Delete every Lobster workflow from the Forge, Gauge, Pulse, Vera, and Tally
  source workspaces before publishing vNext. New agents therefore start with
  zero Lobster workflows.
- Published artifacts are immutable. Do not edit an old S3 artifact in place;
  publish a clean new version, verify it in a clean company, then retire the old
  marketplace version.
- The Paperclip routine description wakes the agent with the complete bounded
  job; the agent uses its connected tools directly and writes the result back.
- A Lobster workflow may be added only after at least three real runs expose a
  repeated deterministic transformation that benefits from a fixed pipeline.
- Likely later candidates are Gauge funnel arithmetic and Tally finance
  arithmetic after source JSON has been fetched. Search, email sending, issue
  routing, approval, and reporting are not candidates.
- Atlas keeps its seven Lobster build pipelines. Agent construction is a real
  staged deterministic workflow with integrity checks and hard gates, not one
  of the operating loops.
- Before marketplace work closes, every still-published non-target listing gets
  a clean-tenant workflow audit. Retire or hide any listing that cannot complete
  its advertised outcome with currently supported integrations.

## Agent origin and lifecycle

Add an API/UI field with exactly three values: `Seed`, `Marketplace`, `Custom`.
It is backend-authored and cannot be supplied by a seller artifact.

- Seed comes from the provisioned seed-agent IDs.
- Marketplace stores canonical listing ID, slug, version, and seller in the
  deployment sidecar and returns them with the origin.
- Agent-builder and owner-created agents are Custom.
- Legacy rows are backfilled only when evidence is unambiguous; otherwise label
  them Custom rather than infer from a name.

The same lifecycle work must make deletion symmetric: archive or reassign live
routines/issues, remove the Paperclip agent, remove its OpenClaw entry, and
remove its workspace sidecar/configuration. Test seed protection, marketplace
replacement, legacy sidecars, reprovision, and deletion.

## The 13 routine contracts

Every description contains: outcome, inputs, allowed actions, required work
product or named null outcome, review/audit policy, metric source, escalation
rule, recovery, and bounded stop condition. Use `coalesce_if_active`, skip
missed schedules, and auto-pause after three consecutive failures unless a
routine-specific reason proves otherwise.

Founder schedules use `America/Aruba`. Marketplace schedules arrive dormant and
require buyer timezone confirmation before arming.

| # | Owner | Routine | Trigger | Work product or valid null outcome | External proof / KPI |
|---:|---|---|---|---|---|
| 1 | Friday | Daily company control | Daily 08:00 | One bottleneck advanced, or no executable priority | Gauge checks cited board and source metrics |
| 2 | Friday | Weekly product/customer synthesis | Monday after scorecards | Prioritized operating plan | Gauge checks PostHog, Gmail, Stripe citations |
| 3 | Forge | Engineering intake | GitHub issue `opened` or `reopened` | Triage, PR URL, or `no-code-change` | GitHub state and CI; Friday reviews priority |
| 4 | Forge | Reliability incident | EventBridge alarm webhook | Diagnosis, fix/PR, or `no-remediation-required` | CloudWatch alarm history; Friday reviews |
| 5 | Pulse | Community distribution | Daily schedule | Published response URL or `no-qualified-signal` | Gauge checks URL and attributed sessions |
| 6 | Pulse | SEO and content | Weekly schedule | Gap list plus content PR/published URL | Gauge checks snippet/URL and organic activation |
| 7 | Pulse | Outbound prospecting | Weekdays | Ten cited prospects and outreach state | Gmail replies; Vera owns inbound replies |
| 8 | Vera | Support and reply routing | Gmail `gmail.message` push | Sent reply ID, resolution, or Forge child issue | First-response latency paired with reopen rate |
| 9 | Vera | Customer health | Weekly schedule | Risk list and completed interventions | Retention/churn from Stripe plus Gmail evidence |
| 10 | Vera | Revenue recovery | Stripe payment/cancel/refund events | Recovery action or governed decision | Final Stripe object state and recovered revenue |
| 11 | Gauge | Activation exception watch | Filtered PostHog webhook | Diagnosed blocker and assigned issue | Browser PostHog events plus CloudWatch direction |
| 12 | Gauge | Activation and GTM scorecard | Weekly schedule | Funnel, attribution, blockers, next constraint | PostHog browser events and first-touch properties |
| 13 | Tally | Finance snapshot | Weekly schedule | Spend/revenue/margin/cash-freshness snapshot | AWS, Stripe, usage counters; stale cash refusal |

### Review policy

Use native routine execution review when the output is bounded and the check is
worth another run. Do not double inference on every high-volume event.

- Gauge reviews Friday, Pulse, Tally, and Vera's weekly customer-health output.
- Friday reviews Forge and Gauge; GitHub CI, CloudWatch, and PostHog are the
  evidence, not Friday's opinion.
- Gmail and activation-event runs are audited by their weekly routines rather
  than reviewed one by one.
- Every true escalation has a stable `ruleKey`. Decision accept/reject/expire
  rates determine when a class earns more autonomy.

## Finite GTM projects

These are not routines. Use the Paperclip hierarchy deliberately:

- **Goal:** the measurable 90-day business outcome. The initial goal is to
  reach 50 paying customers and at least 65% `signup_started -> chat_completed`
  activation, retaining the D30 and D60 checkpoints from the GTM plan.
- **Projects:** finite initiatives that can be completed and closed.
- **Issues:** assigned pieces of project work with an artifact, reviewer, and
  completion state.
- **Routines:** recurring operating mechanisms attached to the same goal and,
  where useful, a project. They continue after a project milestone closes.

Create these projects under the GTM goal:

1. **Demo and public proof:** professionally edited 45-second
   GitHub-issue-to-artifact demo plus the public read-only company board.
2. **First outbound cohort:** 50 evidence-backed prospects, ten founder contacts
   per day, reply review, and a written motion retrospective.
3. **Technical launch content:** the first five verified engineering articles.
4. **Integration directories:** Sentry, Linear, Stripe, and other relevant
   directory submissions.
5. **Show HN launch:** launch asset preparation, launch-day operation, and
   response follow-up.
6. **Customer discovery:** founder interviews, calls, and synthesized findings.

The recurring Pulse outbound routine supplies prospects after the first cohort;
the project closes when the first 50 and its learning review are complete. The
weekly content routine continues after the five-piece launch project closes.
Demo production, directory submission, Show HN, and interviews never become
routines merely because they contain several tasks.

Pulse and Gauge support these projects, but the founder owns early calls,
launch positioning, and the learning from the first outbound cohort.

## Execution sequence

### Phase 0: Freeze contracts and migration ledger

- Approve this roster, routine table, and runtime boundaries.
- Record current agent IDs, open issues, routines, integration assignments, and
  durable artifacts before mutation.
- Create the Paperclip GTM goal and six finite projects; link later routines to
  the goal without turning the projects themselves into routines.
- Treat routine-native reviewer support as a dependency owned by its current PR.
- Confirm marketplace automation packaging is deployed. PR #1793 is deployed.

Exit: every live object has a keep, archive, reassign, or delete disposition.

### Phase 1: Early roster cleanup

Because the founder container is intentionally bare, do not preserve unused
agents merely for overlap.

1. Verify one UI deletion removes both Paperclip and OpenClaw state.
2. Fix deletion lifecycle first only if that verification fails.
3. Reassign any useful open issue to Friday.
4. Archive duplicate routines.
5. Delete Lens, Thread, Platform Operations, the custom Product Analytics
   agent, Stitch, and jarvis.
6. Keep only Friday, Atlas, Pulse, Vera, and Tally until replacements arrive.

Exit: five installed agents, no orphaned OpenClaw entries or active routines.

### Phase 2: Platform rails

Use separate worktrees and PRs:

1. Agent origin/provenance plus symmetric lifecycle.
2. Agent-facing Paperclip skill coverage for attention, decision queues,
   decision effects, `ruleKey` statistics, company search, and extraction.
3. Friday identity contradiction and heal migration.
4. Remove the agent-facing platform-ops token/auth/config/secret plumbing while
   preserving still-used human admin diagnostics behind Clerk admin auth.
5. AWS-integration alarm registration through EventBridge API Destination.
6. Routine-native reviewer support from the separate owner.

Exit: dev proves all primitives through the same UI and container paths a
customer uses; tests and Codex review are clean; deployments are watched.

### Phase 3: Marketplace agent foundry

Use the existing separate publisher/source company and customer-facing builder
and seller UI.

1. Create Forge and Gauge.
2. Narrow Pulse, Vera, and Tally to the responsibilities in this document.
3. Delete the current target source workspaces' Lobster pipelines and private
   state orchestration before export. Confirm the new tarballs contain no
   `.lobster` file.
4. Attach the appropriate dormant routine templates and integration ledger.
5. Publish immutable candidate versions.
6. Hire each candidate into a clean test company and validate origin, files,
   configuration, triggers, tools, and deletion.
7. Audit the remaining published listings and retire/hide failed packages.

Exit: each target listing completes one representative end-to-end outcome in a
clean company with only advertised integrations.

### Phase 4: Install the target founder roster

1. Replace Pulse, Vera, and Tally with the validated marketplace versions.
2. Hire Forge and Gauge.
3. Confirm Friday/Atlas show Seed and all five specialists show Marketplace
   with canonical listing versions.
4. Assign the connected Google support mailbox to Vera after the founder grant.

Exit: seven installed, six active, no active Custom agent.

### Phase 5: Configure all routines paused

- Create all 13 routine rows and every required trigger while paused.
- Add integration variables, thresholds, repository names, ICP, cash amount,
  and cash date without storing credentials in descriptions.
- Add GitHub's `opened|reopened` action fence as the first instruction.
- Configure PostHog filtering and stable escalation `ruleKey` values.
- Ensure every routine names a work product or legitimate null result.

Exit: no legacy duplicate is active and every contract is inspectable in UI.

### Phase 6: Smoke test

For every routine prove:

- scheduled/on-demand or signed webhook delivery;
- correct agent and correct connected account;
- idempotency, retry behavior, and self-trigger protection;
- external work product or named null outcome;
- native review or scheduled audit completion;
- cross-agent child handoff where applicable;
- metric read from the external source;
- decision/attention behavior with stable `ruleKey`;
- no unintended external action;
- circuit breaker behavior.

Exit: one recorded passing run per routine in the founder company.

### Phase 7: Arm in dependency waves

These are smoke-test gates, not multi-week phases:

1. Measurement and control: Gauge, Tally, Friday.
2. Customer and reliability: Vera and Forge.
3. Demand: Pulse community, SEO/content, and outbound.

Arm each routine as soon as it passes. Do not wait for weeks of results between
waves. Gmail alone may remain paused until `support@isol8.co` is confirmed as a
Workspace mailbox, connected, assigned to Vera, and configured for Pub/Sub.

### Phase 8: Operate and earn autonomy

- Friday selects one company constraint from Gauge/Tally/Vera evidence.
- Agents execute continuously and surface only exceptions through attention and
  decisions.
- Review `ruleKey` statistics weekly.
- Relax review/gates only for classes with sufficient accepted decisions and no
  conflicting external evidence.
- Add Lobster only when real run history proves a deterministic repeated step.
- Convert validated operating behavior into the next marketplace version.

## Founder-only blockers

1. Connect and assign `support@isol8.co`; confirm it is a mailbox, not a Group.
2. Create/grant Gmail Pub/Sub resources and complete the OIDC push subscription.
3. Complete credentials and sign-ins personally.
4. Approve legal, financial, and reputation-learning decisions when surfaced.

Browser/Chromium repair is not a prerequisite for these loops because direct
web fetch/search and connected CLIs cover the first versions.

## Definition of complete

- The founder roster is exactly Friday, Atlas, Forge, Pulse, Vera, Gauge, Tally.
- Origin and marketplace version are accurate and backend-authored.
- All 13 routine contracts exist with no legacy duplicates.
- Every non-blocked trigger is armed and has a passing production smoke run.
- Every run emits a resolving artifact or a valid named null outcome.
- Every routine has external proof, a KPI, recovery, and a bounded stop.
- Paperclip attention, decisions, decision statistics, and company search are
  usable by the fleet.
- Forge and Tally use the Settings AWS integration. The agent-facing
  platform-ops token, auth path, configuration, and secret plumbing are gone;
  retained human operator endpoints require Clerk platform-admin auth.
- Target marketplace packages do not carry the legacy Lobster orchestration.
- The weekly scorecard reports activation, attributed acquisition, paid state,
  onboarding blockers, cost per active container, and current priority.
- The 90-day GTM goal carries its activation and customer targets; all six
  finite GTM projects have owners, evidence-backed issues, and completion state.
- Founder attention is limited to genuine authority and learning edges.
