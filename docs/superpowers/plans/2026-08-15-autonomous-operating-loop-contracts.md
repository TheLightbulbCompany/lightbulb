# Isol8 Production Routine Contracts

Temporary execution source for the 13 production routines in
`2026-08-15-autonomous-operating-loops.md`. Copy these contracts into the
customer-facing Automations UI, then verify the persisted values. Delete this
file with the plan when the goal closes.

## Rules shared by every routine

- Timezone is `America/Aruba`. Scheduled triggers skip missed runs and use
  `coalesce_if_active`.
- Every structured routine variable must appear in the title or description as
  `{{VARIABLE_NAME}}`. Paperclip synchronizes definitions to template
  references and removes unreferenced definitions. Prefer one ordered ID-list
  variable over many equivalent scalar IDs when it keeps the customer editor
  auditable.
- Every routine starts paused, auto-pauses after three consecutive failures,
  and is armed only after a passing on-demand or controlled-event smoke run.
- Search Paperclip before creating work. Reuse an open issue or external ID
  when the event was already handled. Never create duplicate outreach,
  support replies, incidents, or blocker issues.
- Complete one bounded pass. A resolving external URL/ID is the work product;
  a contract-specific named null outcome is equally valid. "Done" without one
  of those is a failed run.
- Take reversible, within-policy actions autonomously. Create a Paperclip
  decision with the named stable `ruleKey` only for the listed authority edge.
  Do not ask for approval merely because an action is visible or unfamiliar.
- When another function owns the next action, create a `todo` child issue for
  that agent with this run issue as `parentId`. Do not use OpenClaw subagents as
  a cross-function business handoff.
- Never authorize spend, change legal/financial terms, publish invented facts,
  expose customer data, issue a refund, or impersonate the founder. Connected
  tools share container credentials; prompt policy is not a security boundary.
- Cite the source query and observation time. Put the compact conclusion and
  links on the run issue; attach the substantive output as a work product.
- On a source outage, record the exact unavailable source and the recovery
  action. Do not substitute guessed data. Let genuine technical failures fail
  the run so the circuit breaker can count them.

## 1. Daily company control

- **Owner / reviewer:** Friday / Gauge.
- **Trigger:** `0 8 * * *`.
- **Variables:** `{{GTM_GOAL_ID}}`, `{{MONTHLY_SPEND_CAP_USD}}=2000`.
- **Outcome:** Advance the single current company constraint.
- **Inputs:** active goal and projects, authorized decision queues/triage, open
  work, latest Gauge/Tally/Vera artifacts, current failures, and unresolved
  child work. The unified attention feed is board-only. Use company search; do
  not re-discover old context from scratch.
- **Autonomous authority:** choose one executable priority; set issue priority
  and status; assign one bounded `todo` child to the competent specialist;
  close stale duplicate work; comment the why-now evidence.
- **Authority edge:** legal commitment, new spend, pricing/terms change,
  founder-positioning choice, or two genuinely conflicting top constraints.
  Create a decision with `ruleKey=company-control.authority-edge`.
- **Work product:** URL of the priority issue advanced plus its cited source
  artifact. Named null outcome: `no-executable-priority`, with the blocker and
  owner required.
- **KPI / proof:** latest `signup_started -> chat_completed`, paying customers,
  known spend/headroom, and customer exceptions cited from source artifacts.
- **Stop:** one constraint advanced or one authority decision created. Never
  execute specialist implementation work in this run.

## 2. Weekly product/customer synthesis

- **Owner / reviewer:** Friday / Gauge.
- **Trigger:** `0 11 * * 1`.
- **Variables:** `{{GTM_GOAL_ID}}`, plus `{{GTM_PROJECT_IDS}}` containing the
  six finite project IDs in demo, outbound, content, directories, Show HN,
  discovery order.
- **Outcome:** Publish one prioritized operating plan for the next seven days.
- **Inputs:** the current week's Gauge scorecard, Tally snapshot, Vera health
  report, Forge reliability/engineering outcomes, Pulse artifacts, customer
  threads, and each specialist's cited own-origin accepted/rejected/expired
  decision statistics by `ruleKey`. Friday cannot read another agent's private
  origin-scoped decision statistics directly.
- **Autonomous authority:** reconcile evidence, select the company constraint,
  create/update a small set of owned issues, and retire work contradicted by
  current evidence.
- **Authority edge:** change to ICP, pricing, launch claim, company spend, or a
  founder interview/call objective. Use `ruleKey=weekly-plan.founder-learning`.
- **Work product:** prioritized weekly plan artifact with owner, expected
  evidence, and stop condition for every commitment. No null outcome is valid;
  missing sources produce an explicit incomplete-data plan.
- **KPI / proof:** trials, activation, paid conversions/customers, attributed
  acquisition, support/revenue exceptions, and spend headroom.
- **Stop:** one plan published and its finite issue changes applied. Do not
  create a new goal or duplicate project.

## 3. Engineering intake

- **Owner / reviewer:** Forge / Friday.
- **Trigger:** GitHub `issues`; process only actions `opened` or `reopened`.
- **Variables:** `{{GITHUB_REPOSITORY}}=Isol8AI/isol8`,
  `{{DEFAULT_BRANCH}}=main`.
- **Outcome:** Turn a real issue into verified triage and, when warranted, a
  reviewable code change.
- **Inputs:** webhook payload, canonical GitHub issue, repository code/tests,
  related Paperclip work, and CI state. Exit immediately for every other
  GitHub action before applying a label or comment.
- **Autonomous authority:** reproduce; classify; label/comment; create or reuse
  the Paperclip issue; implement on a worktree/branch; run proportional tests;
  open a PR; request reviews. Never merge, bypass CI, rewrite unrelated work,
  or mutate production directly.
- **Authority edge:** security disclosure, destructive migration, pricing or
  billing semantics, customer-data exposure, or incompatible product choice.
  Use `ruleKey=engineering.product-authority`.
- **Work product:** GitHub PR URL, triage comment URL, or named null outcome
  `no-code-change` with a falsifiable reason (duplicate, invalid, unsupported,
  or already fixed).
- **KPI / proof:** canonical issue/PR state and CI; weekly merge rate for
  `forge/*` branches.
- **Stop:** one triage disposition or one PR. The action filter is also the
  self-trigger fence.

## 4. Reliability incident

- **Owner / reviewer:** Forge / Friday.
- **Trigger:** EventBridge API Destination carrying CloudWatch transitions to
  `ALARM` only, signed by the routine webhook secret.
- **Variables:** `{{AWS_PROFILE}}=isol8-connector`, `{{AWS_REGION}}`, and
  `{{GITHUB_REPOSITORY}}`.
- **Outcome:** Explain and resolve, or explicitly contain, a production alarm.
- **Inputs:** alarm payload, `describe-alarm-history`, relevant CloudWatch
  metrics/logs, current deployment state, repository, and related incidents.
- **Autonomous authority:** read AWS; correlate deployments; create/reuse an
  incident issue; implement a repository fix and PR; add evidence comments.
  The connected AWS tier may technically permit mutation, but this routine's
  policy does not authorize production mutation.
- **Authority edge:** customer communication, rollback requiring elevated AWS
  access, data repair, spend increase, or a change that weakens a safety fence.
  Use `ruleKey=reliability.production-authority`.
- **Work product:** incident diagnosis plus PR/fix URL, or named null outcome
  `no-remediation-required` with alarm-history proof.
- **KPI / proof:** CloudWatch alarm history and time from first ALARM transition
  to OK/contained disposition.
- **Stop:** one incident disposition; do not keep polling after the cited state
  is stable. Duplicate deliveries append to the existing incident.

## 5. Community distribution

- **Owner / reviewer:** Pulse / Gauge.
- **Trigger:** `0 9 * * *`.
- **Variables:** `{{ICP}}`, `{{PRODUCT_FACTS_URL}}`,
  `{{DISALLOWED_CLAIMS}}`, `{{DAILY_RESULT_LIMIT}}=1`, and
  `{{CHANNELS}}=HN,Reddit,X`.
- **Outcome:** Solve one current ICP problem in public and make Isol8
  discoverable through usefulness, not a pitch.
- **Inputs:** current HN/Reddit/X discussions, company search for prior answers,
  verified product/repository facts, and recent attributed acquisition.
- **Autonomous authority:** publish one factual, useful response from the
  company account when it answers the question directly, contains no private
  data, no invented claim, no attack, no commitment, and no paid placement.
  Do not force an Isol8 mention when it is not relevant.
- **Authority edge:** founder personal voice, controversy, competitor claim not
  backed by a public source, launch announcement, or policy/partnership promise.
  Use `ruleKey=community.reputation-edge`.
- **Work product:** published response URL. Named null outcome:
  `no-qualified-signal`, with searched sources and exclusion reasons.
- **KPI / proof:** resolving URL plus Gauge's browser-emitted first-touch
  attributed sessions/activations for the channel.
- **Stop:** one response or null outcome; never mass-reply.

## 6. SEO and content

- **Owner / reviewer:** Pulse / Gauge.
- **Trigger:** `0 9 * * 2`.
- **Variables:** `{{ICP_QUERY_SET}}`, `{{SITE_AND_REPOSITORY}}`,
  `{{PRODUCT_FACTS_URL}}`, `{{CONTENT_PROJECT_ID}}`, and
  `{{DISALLOWED_CLAIMS}}`.
- **Outcome:** Close the highest-evidence discovery gap with a useful,
  technically accurate asset.
- **Inputs:** search results/snippets, company search, current site content,
  Search/PostHog evidence available through connected tools, customer language,
  and the prior weekly plan.
- **Autonomous authority:** create the gap list; update an existing factual
  reference or open a content PR; merge only where repository branch policy and
  normal reviews permit it. Do not fabricate volume, testimonials, benchmarks,
  integration counts, or competitor claims.
- **Authority edge:** new positioning, comparative accusation, legal/security
  claim, founder story, launch post, or paid distribution. Use
  `ruleKey=content.positioning-edge`.
- **Work product:** cited gap list plus content PR or published URL. Named null
  outcome: `no-supported-content-gap`.
- **KPI / proof:** fetched snippet/URL plus browser-emitted first-touch organic
  sessions and activations; never infer conversion from pageviews alone.
- **Stop:** one gap and one asset/disposition.

## 7. Outbound prospecting

- **Owner / audit owner:** Pulse / Vera via reply evidence.
- **Trigger:** `0 10 * * 1-5`.
- **Variables:** `{{ICP}}`, `{{EXCLUSION_LIST}}`,
  `{{DAILY_PROSPECT_LIMIT}}=10`, `{{OUTBOUND_PROJECT_ID}}`, and
  `{{FOUNDER_SENDING_MAILBOX}}`.
- **Outcome:** Supply ten evidence-backed prospects and a specific reason-now
  for founder learning; never manufacture a generic lead list.
- **Inputs:** recent Show HN/launch/funding/hiring/product signals from public
  sources, prior prospects in company search, and Gmail sent/reply state.
- **Autonomous authority:** research, deduplicate, score, and draft a concise
  personalized first touch. During the first-50 project, do not send the first
  cold contact: founder learning and reputation are the purpose of that edge.
  After the project, sending may be enabled only by an accepted decision rule
  with measured reply/complaint history.
- **Authority edge:** first-touch sending class or any message containing a
  commercial/legal promise. Use `ruleKey=outbound.first-touch-send`.
- **Work product:** ten-prospect artifact with source URL, ICP match, signal,
  exclusions checked, draft, and outreach state. Named null outcome:
  `insufficient-qualified-prospects` with the actual count.
- **KPI / proof:** Gmail sent IDs and replies; never count drafts or prospects
  as pipeline.
- **Stop:** ten qualified records or exhausted cited sources.

## 8. Support and reply routing

- **Owner / audit owner:** Vera / weekly customer-health routine.
- **Trigger:** Gmail `gmail.message` push for the assigned
  `support@isol8.co` mailbox; never polling.
- **Variables:** `{{SUPPORT_MAILBOX}}`, `{{PRODUCT_FACTS_URL}}`,
  `{{RESPONSE_POLICY}}`, `{{GITHUB_REPOSITORY}}`, and
  `{{SLA_TARGET_MINUTES}}`.
- **Outcome:** Resolve the customer request in one thread or route a concrete
  product defect to Forge without losing the customer reply.
- **Inputs:** Gmail thread via `gog`, verified account identity, company search,
  product docs, subscription context where relevant, and related issues.
- **Autonomous authority:** classify; send factual support replies; ask one
  necessary clarifying question; update the thread; create a `todo` Forge child
  with `parentId` and reproduction evidence; tell the customer what happens
  next. Never silently close a thread.
- **Authority edge:** refund/credit/discount, deletion, security/privacy event,
  legal threat, contractual promise, harassment, or unknown high-impact bug.
  Use `ruleKey=support.authority-edge`.
- **Work product:** Gmail sent message/thread ID and resolution, or Forge child
  issue URL. Named null outcome: `non-support-message` with classification.
- **KPI / proof:** first-response latency paired with reopen rate. A fast reply
  without durable resolution is not success.
- **Stop:** one sent response and resolved/routed state. Gmail history ID and
  thread ID are idempotency keys.

## 9. Customer health

- **Owner / reviewer:** Vera / Gauge.
- **Trigger:** `0 8 * * 1`.
- **Variables:** `{{SUPPORT_MAILBOX}}`, `{{RISK_THRESHOLDS}}`, and
  `{{INTERVENTION_POLICY}}`.
- **Outcome:** Identify real customer risk and complete the smallest helpful
  intervention before churn.
- **Inputs:** Stripe customer/subscription state, Gmail support/reply history,
  unresolved Paperclip issues, and prior interventions. Do not use PostHog
  Stripe-derived events to reconcile Stripe.
- **Autonomous authority:** produce the risk cohort; send support-oriented
  follow-up grounded in the customer's actual state; create owned child work;
  close risks disproved by current evidence. No blanket campaigns.
- **Authority edge:** discount, contract/pricing change, refund, collection
  threat, founder call, or representation about roadmap timing. Use
  `ruleKey=customer-health.commercial-edge`.
- **Work product:** risk/intervention artifact with Stripe object IDs, Gmail
  message IDs, owner, and next check. Named null outcome: `no-actionable-risk`.
- **KPI / proof:** final Stripe subscription state plus Gmail response and
  support reopen evidence; weekly retained/churned counts.
- **Stop:** every included risk gets one disposition; maximum one unsolicited
  intervention per customer per seven days.

## 10. Revenue recovery

- **Owner / audit owner:** Vera / weekly customer-health routine.
- **Trigger:** Stripe `invoice.payment_failed`, `customer.subscription.deleted`,
  and `charge.refunded` events as one routine with multiple triggers.
- **Variables:** `{{SUPPORT_MAILBOX}}`, `{{RECOVERY_POLICY}}`, and
  `{{AUTHORIZATION_LOOKBACK_DAYS}}`.
- **Outcome:** Help a customer recover legitimate access/revenue state and
  detect unauthorized financial actions without taking financial authority.
- **Inputs:** canonical Stripe object/event, customer Gmail thread, company
  decisions/issues, and prior deliveries. Stripe is the source of truth.
- **Autonomous authority:** send factual payment-update help; link the billing
  portal; create/reuse a recovery issue; for `charge.refunded`, locate the
  authorizing decision/escalation and flag absence. Never refund, discount,
  retry a charge manually, cancel, or alter subscription terms.
- **Authority edge:** missing refund authorization, commercial concession, or
  disputed/fraudulent state. Use `ruleKey=revenue-recovery.financial-edge`.
- **Work product:** Gmail message ID, recovery issue/decision URL, or named null
  outcome `no-recovery-action` with final Stripe state.
- **KPI / proof:** final Stripe object state and recovered revenue; browser
  PostHog events are not an independent Stripe verifier.
- **Stop:** one governed action; Stripe event ID is the idempotency key.

## 11. Activation exception watch

- **Owner / reviewer:** Gauge / Friday.
- **Trigger:** PostHog webhook filtered in PostHog to browser-emitted
  `onboarding_gate_blocked`; the routine rejects unrelated event names.
- **Variables:** `{{GITHUB_REPOSITORY}}`,
  `{{CLOUDWATCH_NAMESPACE}}=Isol8`, and `{{BLOCKER_THRESHOLDS}}`.
- **Outcome:** Turn a named onboarding wall into a reproduced, owned product
  issue before acquisition is scaled.
- **Inputs:** PostHog browser event and person/session context, surrounding
  `onboarding_step_entered` events, CloudWatch
  `provision.gate.blocked{code}` direction, repository, and related work.
- **Autonomous authority:** deduplicate by event/person/code window; diagnose;
  create a `todo` Forge child with `parentId`; prioritize repeated blockers;
  record instrumentation disagreement. Never expose user conversation data.
- **Authority edge:** product-policy choice, intentional eligibility gate,
  privacy ambiguity, or acquisition stop/resume decision. Use
  `ruleKey=activation.product-authority`.
- **Work product:** blocker issue URL with event/code evidence. Named null
  outcome: `duplicate-activation-blocker` with canonical issue URL.
- **KPI / proof:** browser PostHog event plus CloudWatch direction. CloudWatch
  busy/PostHog silent is an instrumentation incident; assert direction, never
  equality.
- **Stop:** one canonical blocker disposition.

## 12. Activation and GTM scorecard

- **Owner / reviewer:** Gauge / Friday.
- **Trigger:** `0 9 * * 1`.
- **Variables:** `{{GTM_TARGETS}}` containing the D30/D60/D90 targets,
  `{{GTM_GOAL_ID}}`, ordered `{{GTM_PROJECT_IDS}}`,
  `{{COHORT_TIMEZONE}}=America/Aruba`, and `{{CHANNEL_MAPPING}}`.
- **Outcome:** Publish the weekly constraint-setting scorecard and assign the
  single most important evidence-backed follow-up.
- **Inputs:** browser-emitted PostHog events, persistent
  `first_touch_utm_*`, Stripe final state, Paperclip armed routines/work, and
  CloudWatch blocker direction. Build onboarding funnels from
  `onboarding_step_entered`; never `step_completed`.
- **Autonomous authority:** compute weekly cohorts; compare checkpoints; assign
  one Forge/Pulse/Vera/Friday child; flag source disagreement. Continue demand
  at learning volume when activation is weak; do not scale volume blindly.
- **Authority edge:** stop/resume scaled launch, ICP/positioning change, or a
  claim that requires customer interviews. Use `ruleKey=scorecard.strategy-edge`.
- **Work product:** scorecard artifact covering trials, activation
  (`signup_started -> chat_completed`), paid/customers, first-touch acquisition,
  blockers, armed automations, source coverage, and current constraint. No null
  outcome; use explicit `incomplete-data` sections.
- **KPI / proof:** PostHog browser events and final Stripe objects. Exclude
  `onboarding_ready`, paid-inapplicable `trial_first_agent_response`, and
  Stripe events copied into PostHog.
- **Stop:** one scorecard and one follow-up assignment.

## 13. Finance snapshot

- **Owner / reviewer:** Tally / Gauge.
- **Trigger:** `0 7 * * 1`.
- **Variables:** `{{AWS_PROFILE}}=isol8-connector`,
  `{{MONTHLY_SPEND_CAP_USD}}=2000`, `{{FINANCE_MANUAL_INPUTS}}` containing
  cash on hand, cash as-of date, and fixed monthly committed spend, plus
  `{{SOURCE_COVERAGE}}`.
- **Outcome:** State what Isol8 has spent, earns, and can safely commit without
  pretending incomplete accounting is complete.
- **Inputs:** AWS Cost Explorer, Stripe final state, inference usage counters,
  active-container count, supplied cash/fixed commitments, and prior snapshot.
- **Autonomous authority:** read/reconcile sources; calculate known MTD spend,
  projected month-end spend, remaining headroom, revenue, inference dollars per
  active container, and gross-margin inputs; assign a cost anomaly to Forge.
  Never purchase, resize, refund, transfer, or change a financial account.
- **Authority edge:** projected spend over cap, known spend at or above 80% of
  cap, unexplained material variance, stale cash requiring runway, or any new
  commitment. Use `ruleKey=finance.founder-authority`.
- **Work product:** finance snapshot with arithmetic, source timestamps,
  coverage, known/unknown split, and cap headroom. Named refusal states:
  `cash-stale` when `CASH_AS_OF` is over 45 days old, `fixed-costs-missing`, and
  `source-coverage-incomplete`; never state runway or total burn under them.
- **KPI / proof:** AWS spend per active container, inference dollars per active
  container, Stripe revenue, known spend versus USD 2,000 cap.
- **Stop:** one reconciled snapshot or explicit refusal; do not estimate missing
  sources as zero.

## Arming order

1. Tally snapshot, Gauge scorecard/exception, Friday daily/weekly.
2. Vera customer health/revenue recovery/support on-demand, then Forge GitHub
   and CloudWatch.
3. Pulse community/content/prospecting.
4. Gmail Pub/Sub last: create the Google topic/grant, create the automation,
   add the OIDC push subscription, send one controlled support message, verify
   the Gmail history/message/thread path, then arm. Never fall back to polling.
