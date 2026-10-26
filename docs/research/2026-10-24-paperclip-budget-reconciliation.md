# Budgets & spend: Paperclip vs the Router branch — reconciliation research

Date: 2026-10-24
Method: Paperclip fork read (`~/Desktop/paperclip-fork.nosync`) + this repo + graphify
code graph. Prompted by the discovery that the 2026-10-23 Router branch built a
per-project budget that overlaps Paperclip's existing one.
Detail reports (ephemeral scratch): R1 paperclip-budget-engine, R2
lightbulb-surfacing-and-inference-cost, R3 spend-map-and-reconciliation.

## TL;DR

Paperclip **already has** a full, enforcing budget system scoped to
**company / agent / project** — but it is **inert for inference today** because
no one feeds it real inference dollars. The Router branch built a parallel
project budget to fill that hole. The right fix is not a second budget system:
it is the **cost-events bridge** the Lightbulb frontend already names as the
blocker — report real inference cost (which only the settlement Lambda holds)
into Paperclip's `cost_events`, and let Paperclip's existing budgets enforce.

## 1. Paperclip's budget engine (the fork — it is real and enforcing)

- **Scopes:** company, agent, **project** (`BudgetScopeType`, shared
  `constants.ts:873`). Metric is `billed_cents`.
- **Windows:** `calendar_month_utc` and `lifetime`. New project-scope policy
  defaults to `lifetime`; others default monthly (`budgets.ts:549`).
- **Spend** = sum of `cost_events.cost_cents`, filtered by agent/project for
  that scope.
- **Thresholds:** `warnPercent` default 80 → soft incident (no pause); at/over
  the amount → hard incident, pauses the scope (`pauseReason="budget"`), calls
  `cancelBudgetScopeWork` (cancels active runs + pending wakeups), and files a
  `budget_override_required` approval. `getInvocationBlock` (`budgets.ts:756`)
  also blocks new runs before they start. (`evaluateCostEvent`, `budgets.ts:687`.)
- **Agent `budgetMonthlyCents`** is a mirror of a scope=agent monthly policy.
- **`cost_events` CAN carry inference cost:** the heartbeat ledger writes one
  event per adapter run (`heartbeat.ts:~20038`) with provider/biller/model/
  tokens/agent/issue/project; the dollar is **adapter-reported** `cost_usd × 100`
  (the Hermes gateway adapter reads `cost_usd` from the run's terminal payload,
  `adapters/hermes/.../execute.ts:632`). **No `cost_usd` ⇒ written `costCents=0`,
  `costStatus="unpriced"` ⇒ budgets never trip.**
- **Costs page** (already built): overview (inference ledger, incidents, spend
  by agent/project), budgets (policy cards), providers (provider/model + quota
  windows), billers, finance (debit/credit ledger).
- **API:** `budgets/overview`, `budgets/policies`, `budget-incidents/:id/resolve`,
  `costs/*` (by-agent, by-project, by-provider, by-biller, finance, quota-windows).

## 2. Why it is inert for inference today (this repo)

- **The bridge sends tokens only.** The `hermes_gateway` run result is built in
  `infra/hermes/bridge/turn.py:865-880` with input/output/cached tokens + model,
  **no `cost_usd`, no `billingType`.** So every Paperclip inference cost event is
  `$0`/"unpriced" and budgets can't trip.
- **Nothing in our repo calls Paperclip's cost-events or budget routes.**
  `PaperclipAdminClient` has no cost/budget method; the only mentions are docs.
- **`budget_monthly_cents` enforces nothing:** our create form omits it (defaults
  0); even if set, agent spend stays $0.
- **No cost/budget UI in our frontend.** `feed.py:207` drops the dashboard costs
  block (Paperclip undercounts — only its own successful runs); the budgets block
  passes through but renders nothing. Frontend comments literally say budgets are
  "gated on a cost_events bridge" (`AgentsListPanel.tsx:340`).
- **Where the real dollar lives:** only the **settlement Lambda**
  (`openrouter-settlement/index.py`) has OpenRouter's real cost (nanodollars) ×
  markup; it ingests Metronome, debits the fast-gate, and (on the branch) accrues
  `budgets.<project>.spent_microcents`. Metronome is the authoritative billed
  ledger. `record_usage` is tokens only, spend 0.

## 3. The full spend map — 11 concepts, 7 enforce

Enforcing: Paperclip budgets; Metronome fast-gate reservation; OpenRouter per-key
daily $ limit; trial caps (`trial_local`/`openrouter_capped_day`); the branch's
project budget; Stripe Projects per-provider limits (designed); Link-wallet
purchase budget (designed). Non-blocking ledgers/visibility: Metronome, usage
counters, run-lifecycle (idle/max) limits. Plus the managed-vs-BYO switch that
decides which valves apply.

**The three real conflicts:**
1. **Two project-scoped monthly $ budgets** — Paperclip `budget_policies` vs the
   branch's billing-item `budgets` map. Different stores, ids, units; can silently
   disagree.
2. **Paperclip can't see inference $** (§2) — which is *why* the branch built its
   own accrual.
3. **The branch's attribution is unwired** — `project_id` is never passed by
   `build_hermes_runtime_env`'s caller, nothing sends `x-lightbulb-project-id`,
   and a per-runtime env var can't be per-project anyway. Everything → `__default__`.

## 4. Recommended reconciliation (for the updated plan)

**One budget source of truth = Paperclip.** Author budgets as Paperclip
`budget_policies` (its Costs page is the authoring + visibility UI; surface it in
our frontend). Then:

- **Build the cost-events bridge (the core additive piece):** report marked-up
  real inference `cost_usd` into Paperclip `cost_events` from the settlement path
  (the one place the dollar exists), per agent/project/run. This lights up
  Paperclip's existing budgets — the gap the team already named.
- **Keep the gate's pre-request 429 valve**, but have it read a **mirror** of
  Paperclip's ceiling written onto the already-fetched billing-item row (no
  hot-path call to Paperclip). This stops a request *in the gate*, before the
  model call — stronger than Paperclip's run-start block for a runaway loop.
- **Drop `PUT /billing/budgets` as an authoring surface** and the UsagePanel
  budget card (Paperclip authors + displays). The billing-item becomes a
  read-only mirror, not a second authored budget.
- **Fix attribution:** a **per-request `x-lightbulb-project-id` header** from the
  bridge (not the per-runtime env var); `project_id` = the Paperclip project id.
- **Keep** the resolved-model surfacing (Task 1).

**Ramp mapping:** Glass's per-project meter + cap = Paperclip's project scope +
`warnPercent` + hard-stop. The gap was never a meter — it was feeding real
inference cost in.

## 5. What this means for the 2026-10-23 branch

- Task 1 (resolved model): **keep.**
- Task 3 gate 429 + settlement accrual: **keep the valve**, but repoint accrual to
  **report into Paperclip cost_events** and have the gate read a mirrored ceiling.
- Task 2 billing-item budget store + `PUT`: **demote to a read-only mirror**; drop
  the authoring endpoint.
- Task 4 UsagePanel budget card: **drop**; surface Paperclip's Costs page instead.
- Net: the branch's machinery is ~half reusable; the premise ("build a budget")
  was wrong — the real work is the cost bridge + wiring Paperclip through.

## Open decisions for the owner
- Confirm **Paperclip authors, billing-item mirrors** (removes the two-budget
  disagreement by construction).
- Whether the gate's pre-request valve is worth keeping alongside Paperclip's
  run-start block (recommend yes — it's the only per-inference-call stop).
- Project vs agent as the primary budget unit users author (Paperclip supports
  both; Ramp-parity favors project).

---

## What the vendors we use prescribe (Stripe / Metronome / OpenRouter docs, 2026-10-24)

All three agree on the SAME shape, and it matches what we already run: **meter
and bill asynchronously in the vendor; enforce in your own fast layer; cost is
known in real time.**

- **Stripe** — bills, does NOT enforce. Entitlements are boolean flags you
  persist and check yourself ("your application needs to disable the associated
  features"); credit grants reconcile only at invoice time and a customer "can
  exceed their balance during the cycle"; real-time drawdown is Metronome's job.
  Stripe now points new usage integrations at Metronome.
  (docs.stripe.com/billing/entitlements, /api/billing/meter, credit-grants.)
- **Metronome** — its own prepaid-credits guide says keep a per-customer
  **entitlement flag in your system**, check it before each action, and drive it
  from Metronome signals (a $0-balance alert + payment webhooks). Balance reads
  (`getNetBalance`/`listBalances`) are "real-time" for DISPLAY, with no latency
  SLA — not for pre-flight gating. Webhook alerts are "a hint"; for a hard limit
  you block access yourself. Model: **serve, meter, alert, flip your flag.**
  (docs.metronome.com/launch-guides/prepaid-credits, /api-reference/usage.)
- **OpenRouter** — returns **actual `usage.cost` synchronously** in the final
  streaming chunk of every call (always on; `stream_options.include_usage` is
  deprecated/no-op). Per-key `limit`+`limit_reset` is a **hard** block (402/403)
  enforced by OpenRouter. Fallback for a cut stream: `GET /api/v1/generation` →
  `total_cost` + `cache_discount`. (openrouter.ai/docs usage-accounting, limits,
  provisioning-keys.)

### What this changes: no estimate needed
Earlier I argued budgets had to run on an estimate because settled cost arrives
~5 min later. **That was wrong** — the 5-min lag is only our settlement polling
`/generation`; OpenRouter already hands the real cost back at the end of each
call. So:

- **Record REAL per-call cost at run-end.** The bridge sums OpenRouter
  `usage.cost` across the run's calls (the stream already carries it; our bridge
  currently reads only tokens) and sets `cost_usd` in the run-status payload →
  Paperclip prices the run with **real** cost and full agent/project/run
  attribution. Cut-stream fallback: `GET /generation`.
- **The hard real-time floor stays and is exactly the vendors' prescription:**
  our Metronome fast-gate reservation + the OpenRouter per-key limit ARE the
  "own fast gate / entitlement flag" Metronome and Stripe tell you to keep. We
  already built it.
- **Metronome stays the async invoice/ledger** (settlement still ingests for
  billing); it is the reconcile, not the gate.
- **Paperclip budgets** are the soft per-project/agent control (warn/pause),
  now fed real cost.

Net: the design needs no estimate and no settlement write-back — just "bridge
reads the cost already in the stream." Supersedes the estimate-vs-reconcile
decision above.

---

## Open-questions resolution (2026-10-24) — corrects the "bridge sets cost_usd" premise

**Q2 — fork adapter shape: SETTLED, no fork change.** `parseCostUsd`
(`adapters/hermes/.../execute.ts:635`) reads `cost_usd ?? costUsd ??
usage.cost_usd ?? usage.costUsd` off the run result and emits `cost_usd`. Tokens
are snake and already match our payload. We just set `cost_usd` on the
run-status payload; Task 3 is pure verification.

**Q3 — units: SETTLED from our own code.** Settlement treats OpenRouter cost as
USD (`cost_nanodollars` × 1e9, on `total_cost` which is net of cache_discount)
and bills provider-cost × `OPENROUTER_MARKUP`. So the figure fed to Paperclip
must be **marked-up** cost (× markup) to equal what the customer is billed in
Metronome.

**Q1 — PREMISE WRONG: the bridge cannot get the real `usage.cost`.**
- Our tree only ever reads Hermes TOKEN attributes (`agent.session_input_tokens`
  etc., base-image owned). There is **no cost attribute** anywhere in our code.
  Hermes tracks its own `estimated_cost_usd` (a list-price estimate), not the
  provider's real `usage.cost`.
- The REAL `usage.cost` is visible only at the **openrouter-gate** (it forwards
  the OpenRouter stream; `forward.mjs` already parses the final chunk for
  `id`/`model`). But the gate is keyed by **owner** and carries **no run/agent
  identity** — it even strips `user`/`metadata` (`routeFields`).
- So real cost (at the gate) and run/agent attribution (at the bridge/Paperclip
  run) live in different places with **no shared key**. The estimate question is
  therefore about *attribution*, not latency.

Real options (owner decision):
- **A — Hermes estimate → Paperclip:** forward Hermes's `estimated_cost_usd`
  (× markup). Real-time, full attribution, trivial. But it's Hermes's list-price
  estimate — likely misprices `openrouter/auto` and is only approximate.
- **B — real cost via gate+settlement:** thread run/agent id to the gate (a
  custom header the agent's LLM calls carry — an UNCONFIRMED Hermes capability),
  gate stamps the requests row, settlement POSTs marked-up real cost to Paperclip
  cost_events with attribution, suppressing the heartbeat's $0 event. Accurate,
  materially more work, one unconfirmed dependency.
- **C — hybrid:** A enforces real-time, settlement reconciles to real (needs B's
  attribution anyway).

One empirical unknown left, needs the Hermes base image or a live managed dev
turn: does Hermes expose the provider's real `usage.cost`, or only its own
estimate? If it exposes the real one, A collapses into the clean seam.

---

## Hermes cost confirmation (read from the pinned base image, 2026-10-25)

Pulled `nousresearch/hermes-agent@sha256:a5b2eac…` (the pinned base) and read
`/opt/hermes` directly. Definitive:

- **Hermes's live turn cost is ESTIMATE-ONLY.** `agent/usage_pricing.py:552
  estimate_usage_cost` — called every API call by `agent/turn_usage.py:224` —
  prices from its own `official_docs_snapshot` price tables, hard-sets
  `status="estimated"`, and for our route appends the note *"OpenRouter cost is
  estimated from the models API until reconciled."* The `"actual"` status and
  `provider_cost_api`/`provider_generation_api` sources exist in the schema
  (`actual_cost_usd` column, `COALESCE(actual_cost_usd, estimated_cost_usd)`) but
  **nothing in the live SDK ever sets them** (full-tree grep: zero assignments).
  So `agent.session_estimated_cost_usd` (what a bridge read would get) is a
  token×list-price estimate, **no markup**, and for `openrouter/auto` priced at
  whatever entry resolves — not the provider's real figure.
- **Header capability:** Hermes supports a custom provider's `extra_headers`
  (`hermes_cli.config.apply_custom_provider_extra_headers_to_client_kwargs`) and
  `default_headers`, but these are **static, set at client-build time** (cached in
  `client_lifecycle`). Good for a per-AGENT/static tag; there is no per-REQUEST /
  per-run dynamic header hook.

### Consequence for the design
- "Bridge forwards Hermes's recorded cost" gives an **estimate**, not real cost.
  So simple≠accurate. (Corrects the 2026-10-24 plan's Task 1–2 premise.)
- **Most accurate feasible today:** the **gate** captures OpenRouter's real
  `usage.cost` (synchronous, in the final chunk) × markup, and attributes it
  **per AGENT** via either a per-agent gate bearer (reuses the per-owner
  key/bearer machinery + each profile's own `.env`) or a static agent-id
  `extra_headers` the gate reads. Feed that per-agent real cost into Paperclip →
  **agent budgets accurate**.
- **Per-PROJECT inference cost stays approximate** unless Hermes is changed to
  emit a per-run tag the gate can read (static headers can't vary per run). So
  project-scoped inference budgets would be agent-summed, not run-split, until
  that change.
