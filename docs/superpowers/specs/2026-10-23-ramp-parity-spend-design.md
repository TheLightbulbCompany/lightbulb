# Ramp-parity spend: Router, provisioning, and agent money — design

Date: 2026-10-23
Status: DRAFT for owner approval (no build until approved)
Author: Claude (with prasiddha)

## Intent & why

We want parity with Ramp Glass on the two things the owner rated highest: the
**Ramp Router** story (route each agent request to a cheap-enough model, meter
it, and cap it per project) and the **financial angle** (let agents spend real
money inside budgets and policy). The Glass teardown (see
`docs/research/2026-10-23-ramp-glass-desktop-teardown.md`) showed Glass is an
agent workspace whose spend controls are: model routing + per-project cost
meters + scheduled automations, with real money handled by Stripe primitives
(Link wallet, Issuing). We already own most of the Router plumbing and, as of the
2026-10-23 live test, the Stripe provisioning rail is unblocked.

## Goals

- Route managed inference across several models, picking the cheapest adequate
  one per task, inside the existing openrouter-gate.
- Give every room/project a **budget** and a **live cost meter** the owner sets
  and sees.
- Unblock and ship the Stripe **provisioning rail** (services for the customer's
  company) on the current `2026-09-16.preview` surface.
- Let an agent **spend real money within a budget and a policy**, Phase 1 via
  **Stripe Link Agent Wallet** (customer-funded, approval-gated), with a shared
  policy/approval/meter layer.
- Keep Stripe **Issuing** as a designed-for Phase 2 that drops under the same
  policy layer, not a rebuild.

## Non-goals

- Becoming a card issuer in Phase 1 (no Issuing application, no float).
- Replacing Ramp as a company's system-of-record for cards/expenses.
- A new event bus, queue, or ledger. Reuse SQS wake + AppSync + DynamoDB +
  Metronome exactly as they are (messaging invariant).
- Any customer-data storage on local machines; all money state is backend-owned.
- **Router + inference budgets for BYO-key / ChatGPT-OAuth owners.** Those paths
  don't traverse the gate, so routing and dollar budgets can't apply. They get
  usage *counts* and the Track C purchase budget; inference is billed by their own
  key/plan. This is deliberate scope, not an oversight.

## Anti-goals (these make it a failure even if it "works")

- A per-project budget that is advisory only and cannot actually stop spend.
- A model router that silently downgrades quality with no owner visibility.
- Spending real money without an explicit, auditable approval the owner set.
- Shipping the provisioning rail against the old spec so it 404s/500s in prod.

## Constraints (verified facts + standing rulings)

- **openrouter-gate already does** per-owner key custody, a model-policy `models`
  LIST, price ceilings, per-request reservation × markup, settlement at real
  OpenRouter cost, daily caps, and modality (vision) routing. It is the only path
  on the inference flow; the reservation lives there. Metronome is a ledger, not
  a valve. (CLAUDE.md "Managed inference".)
- **Stripe provisioning rail is unblocked** (live test 2026-10-23): pinned to
  `2026-09-16.preview`, `GET features/identity` and `POST projects/prepare` now
  return 200 (`is_eligible:true`), where all 500'd on Sept 3. BUT the surface
  changed: `account_requests`/`payment_method_requests`/`accounts`/`account_links`
  now 404; the live resources are `projects`, `resources`, `provider_connections`,
  `catalog/*`, `eligibility`, `features`, `identity`, `projects/prepare`. Account
  creation moved to **Accounts v2 core + embedded onboarding** (the
  `onboarding_rework` feature flag; email-verification OTP).
- **Provisioning rail is backend-owned**: platform key never leaves the backend;
  credentials live in Secrets Manager under `projects/{provider}` (not the bare
  provider key); the state machine (`provisioning_workflow.py`) advances on read,
  has no webhooks, no idempotency contract (a timed-out write parks the row), and
  the catalog partition is configuration, not a safety net. Reuse this, don't
  rebuild it.
- **Link Agent Wallet requirements**: `@stripe/link-cli` (Node 24+), OAuth-style
  grant, US/Canada Link accounts, customer-funded, approval policies with
  per-purchase limits, hard caps ($500/request, $500/day, $20k/30d), approved
  card/token valid 48h. No Stripe approval, no float.
- **Issuing requirements (Phase 2)**: Issuing application + review, US/UK/EU +
  US physical presence, we fund/float, agent-card PaymentMethods need private
  preview access + preview API version.
- **Rulings that bind this work**: OTP codes are browser-only
  ([[otp-codes-browser-only]]); money/prod/security decisions go to the owner
  ([[route-decisions-to-coordinator]]); dev deploys on merge, prod on dispatch;
  reuse before building ([[ponytail-on-plans-and-designs]]).

## Architecture — one shared layer, three tracks

```
            ┌───────────────── Spend Policy + Budget + Meter (shared) ─────────────────┐
            │  per-room/project budget object (DynamoDB) · live meter · approval rail  │
            │  owner-written policy (allow/limit/escalate) · reuses Teams approvals    │
            └───────▲───────────────────────▲────────────────────────────▲────────────┘
                    │                        │                            │
            Track A: Router          Track B: Provisioning        Track C: Agent money
         (openrouter-gate)        (stripe_provisioning)        (Link wallet → Issuing)
```

The budget/meter/policy layer is built **once** and all three tracks report into
it. It reuses existing pieces: per-owner usage counters (`record_usage`),
Metronome, the Metronome fast-gate reservation, and the existing approval rail
(`provisioning_approvals` / Teams approvals), not new infrastructure.

## Track A — Router (meter + route + budget)

- **Reuse:** the openrouter-gate. It already takes a `models` list, meters,
  reserves, settles, and caps.
- **Build (small):**
  1. **Model selection** — a step that, per request, picks the cheapest model in
     `policy.models` that meets a capability bar for the task (tools, context,
     modality). Lives in/beside the gate so the one-path invariant holds.
  2. **Budget object** — a per-room/project budget (ceiling + window) in
     DynamoDB, checked in the gate's existing reservation step; over-budget is a
     structured 429 the UI renders as a limit card (same shape as `trial_daily_cap`).
  3. **Live meter** — surface spend/budget in `/chat` (reuse `record_usage`
     counters + the billing read surface); no new ledger.
- **No Stripe.** This ships independently and first.

## Track B — Provisioning rail (unblock + ship)

- **Reuse:** `stripe_provisioning.py`, `provisioning_workflow.py`, the
  `projects/{provider}` secret custody, the owner-approval gate.
- **Build:**
  1. **Re-point the client** to the **`2026-09-30.preview`** surface (the guide's
     version; `2026-09-16` is stale): `provider_connection_requests` replace
     `account_requests`, `livemode` is explicit on resource create and the payment
     profile/method, and `reveal_access_configuration` replaces `environment_info`.
  2. **Company-account creation** via Accounts v2 core + **hosted onboarding**
     (owner decision; Account Links, `account_update` for corrections), then a
     `projects_ready` gate: retrieve the account and require the projects capability
     active before any workflow starts. The email-OTP step stays browser-only
     ([[otp-codes-browser-only]]).
  3. **Validate** end to end on one real connected account in a gated flow, then
     flip `STRIPE_PROVISIONING_ENABLED` (dev first, then a prod dispatch).
  4. **Correct CLAUDE.md** — the "every state-changing call 500s, leave OFF" note
     is now false; replace with the 2026-10-23 evidence.

## Track C — Agent money (Link wallet Phase 1, Issuing Phase 2)

- **Phase 1 — Link Agent Wallet.**
  1. Integrate `@stripe/link-cli` (or its underlying API) behind the backend; the
     customer grants their Link wallet via the OAuth device flow (surfaced in
     Settings → Connections).
  2. An agent "spend request" (merchant, amount, ≥100-char context) is created
     only within the shared budget/policy; approval is surfaced as a Needs-you
     approval item (reuse the approval rail), or auto-approved by an owner-set
     policy within limits.
  3. The approved single-use card/token is used by the agent to pay; values never
     enter a log, row, or transcript (same custody rule as browser secrets).
- **Phase 2 — Issuing.** Only if the owner decides to become a card program.
  Drops **under the same policy/budget/approval layer**: swap the credential
  source from Link wallet to an Issuing card + real-time authorization webhook
  that applies the same policy. Gated on Issuing approval + preview access + the
  funding model.
- **Policy layer (shared, built in Phase 1):** an owner-written spend policy
  (allowed merchants/categories, per-purchase + window limits, escalate-above-X).
  A read-only "policy advisor" over the existing `company_stripe` reads can flag
  existing transactions against the policy even before issuing — cheap early win.

## Sequencing

1. **Track A** (no external dependency) and **Track B** (unblocked) in parallel.
2. **Track C Phase 1** (Link wallet) + the shared policy/budget layer, after A's
   budget object lands (C reuses it).
3. **Track C Phase 2** (Issuing) only on an explicit owner decision to pursue a
   card program.

## What's left to the builder / open calls

- Exact `2026-09-16.preview` request/response shapes for `projects`/`resources`
  (the gated API ref resolves these; confirm during Track B).
- Where model-selection logic physically sits (inside the gate Lambda vs a thin
  pre-step) — builder's call, constrained by the one-path invariant.
- Whether the Phase-1 Link wallet funds from the customer's personal Link account
  or a company Link account — product/owner input (B2B funding source).
- Budget granularity: per-room, per-project, or per-agent — owner input.

## Evidence
- Live Stripe test + requirement tables: `docs/research/2026-10-23-ramp-glass-desktop-teardown.md`.

## Builder-check resolutions (2026-10-23)

A fresh engineer-reviewer read this cold. Resolutions below supersede the
"open calls" list above.

**Settled as builder calls (my decisions):**
- **Card custody / delivery (Track C):** the approved single-use card/token is
  injected by the backend the same way saved logins are (typed over CDP into the
  Browser v2 / Mac rail); the agent never receives the raw value. If no
  server-side Link API exists and only the Node CLI does, Phase 1 begins as a
  throwaway spike to confirm the server path before any product build.
- **Room/project attribution (Track A):** thread a room/run id through the
  runtime request envelope into the gate; this requires a bridge + runtime-request
  change and a runtime roll (real cost, called out for the plan). Non-room traffic
  (nightly CEO, routines, heartbeats) charges a **workspace-default bucket**, not
  exempt.
- **Limit behaviour:** at the cap the gate **blocks new requests** (429 limit
  card, same shape as `trial_daily_cap`); it does **not** kill an in-flight run.
  Hard per-room stop is reliable only on the managed-paid reservation path;
  capped/trial owners stay best-effort (already bounded by OpenRouter's daily key
  limit). A small settlement-lag overshoot (≤5 min) is accepted.
- **Router selection:** v1 picks cheapest-adequate by a **heuristic** (context
  size, tools present, modality) — no classifier call on the inference path.
  Model tiers live in `MODEL_POLICY_JSON`. Visibility = a per-message model label
  + meter breakdown (satisfies "no silent downgrade"). Routing applies to the
  **managed path only**; BYO and ChatGPT OAuth don't traverse the gate.
- **Policy form:** **structured** (merchant/category allowlist + numeric limits),
  never free-text/LLM-judge on a money path. Owner + admins edit the policy;
  over-limit spend uses the existing approval rail. **Auto-approve within an
  owner-set structured policy counts as the explicit, auditable approval**
  (the owner pre-authorised the rule); anything over-limit needs a fresh approval.

**Answered from evidence:**
- **Connected-account model (Track B):** a **customer-owned Standard-style
  connected account** per company (customer keeps the Dashboard, we take no
  application fee) — from the Sept Stripe thread. Embedded onboarding needs a
  Connect.js surface on that account.
- **In-flight workflow rows:** none exist in prod — the rail has always been OFF
  (`STRIPE_PROVISIONING_ENABLED=false`), so there are no live rows referencing the
  404'd resources and no migration is needed.
- **Gated API ref access:** we have it (our account is the gated one; proven by
  the live test), so Track B is a build, not a discovery task.

**Still yours (owner decisions — see hand-off message):**
1. Phase-1 Link wallet funding: personal vs company/workspace wallet.
2. Do real-money purchases and inference share ONE budget pool, or separate pools
   under one shared policy shape?
3. Budget unit: per-room, per-project, or per-agent.

## Owner decisions (2026-10-23) + the Stripe-stack finding

- **Wallet (Phase 1):** the **workspace admin's** Link wallet funds agent
  purchases. Known caveat: the grant is tied to that person; re-grant if they
  leave. This is itself the argument for company-funded Issuing in Phase 2.
- **Budget unit:** **per-project** (rooms/agents roll up), matching Ramp's
  per-project meter.
- **Budget pool:** **separate pools, one shared meter** — see below.

**Why separate pools (and how the Stripe stack forces/aligns it).** Stripe now
owns all three layers we use: it **acquired OpenRouter** (Aug 2026, ~$7.5B, the
model router/inference supplier) and **Metronome** (Dec 2025–Jan 2026, ~$1B, the
usage meter), on top of its own payment rails (Link, Issuing, and the Privy
agent-wallet / Tempo micropayment units). Stripe's intended flow is explicit in
commentary: **every token routed through OpenRouter emits a metering event in
Metronome and settles as a payment in Stripe** — routing → metering → payment.

That is exactly the stack we already assembled (OpenRouter for inference,
Metronome for metering, Stripe for money), so we should build along its seams,
not across them:

- **The money comes from two different places, so one pool is impossible.**
  Inference is drawn from platform-managed OpenRouter credits (metered in
  Metronome, billed by us); purchases are drawn from the admin's Link wallet
  (customer-funded). They cannot be a single dollar pool.
- **Metronome is the one shared meter** across both — inference and purchases
  become distinct billable metrics/line items on the same ledger. So "build once"
  holds for the meter + policy + approval machinery; only the two pools differ.
- **Strategic note:** our whole Router+money architecture now sits inside one
  converging vendor. Upside: tight integration, design-partner leverage (Stripe
  is already in our shared Slack), and the Issuing/Link/Privy rails line up.
  Risk: vendor concentration on the money path — worth a conscious call later,
  not a blocker now.

**All three design decisions are now resolved. The design is ready for approval.**

## Accepted pattern + AWS alignment (checked 2026-10-23)

There is no single published "OpenRouter + Metronome + Stripe for agents"
reference architecture yet (acquisitions are months old). But Stripe's
established Billing/Meters pattern is clear and we already follow it:

- **Own ledger is the valve; Metronome/Stripe are the downstream meter.** Stripe
  and Metronome do billing, NOT enforcement/limits — the valve is ours.
- Our **openrouter-gate** (per-request reservation vs the Metronome fast-gate
  DDB row) is that valve; our **settlement Lambda** is the downstream meter writer
  (deduped by generation id). This maps 1:1 onto the recommended pattern.
- **Track A's per-project budget reuses this valve** — it is the sanctioned place
  to enforce, since Metronome won't.

**No current AWS conflict.** Checked the gate, fast-gate, settlement, markup, and
key custody. OpenRouter billing *us* and us billing *our customers* via *our*
Metronome contract are two separate ledgers; OpenRouter does not write our meter,
so there is no double-count. Our settlement Lambda is the sole writer of the
customer-usage meter.

**Watch (future, not a blocker):** if Stripe ships a native OpenRouter→Metronome
pass-through meter, our settlement Lambda becomes redundant and could double-count
if left running — switch to the native path then, don't run both. Design the
settlement writer so it can be turned off in favour of a native meter.

## Future direction (owner ruling 2026-10-23) — not in this build

Build Router + budgets for the **managed path only** now. Later, bring the other
inference paths under the gate so they get routing + budgets too:
- **OpenRouter sign-in with ChatGPT** — route ChatGPT-authenticated owners'
  inference through OpenRouter (and thus the gate) instead of direct OAuth.
- **Possibly retire the BYOK paths** entirely once the gate can cover them.
Until then, BYO/ChatGPT owners are managed by their own provider, by design.
