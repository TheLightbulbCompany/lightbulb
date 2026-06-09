# Tenant-Proof Per-Owner Bedrock Metering, Enforcement & Credit Ledger — Design

**Status:** Design (brainstormed + workflow-verified). Awaiting review → implementation plan.
**Date:** 2026-06-07
**Author:** Claude (with prez)

---

## 1. Context — what we already shipped

This design is the explicit follow-up to two PRs that already merged to `main`.

### PR #826 — Bedrock cost remediation (squash `07eeb3e1`)
Made inference cheaper + correctly priced + observable **on the runs we actually capture**:
- **Model tiering:** default → Sonnet, Opus reserved for the CEO agent (Friday) per-agent, Haiku for aux ops (compaction/heartbeat/memory-flush); Haiku 4.5 added.
- **Caching:** `agents.defaults.params.cacheRetention="long"` (1h TTL).
- **Metering (interim):** bill on OpenClaw's cache-inclusive `estimatedCostUsd` (closed the ~30× cache-blind undercount on captured runs); record non-Bedrock volume at spend 0.
- **Rates:** corrected to AWS-verified `us.` regional values (Opus was billed 2.7× high), fixed an invalid Haiku id, added `sync_bedrock_rates.py` (sources rates from the AWS agreement-offer rate-card).
- **Guardrails:** W52 CloudWatch alarm on runaway Bedrock token volume; read-only `reconcile_bedrock_vs_ledger.py` (CloudWatch tokens + Cost Explorer dollars).
- **Ops:** `reapply_openclaw_config.py` (deep-merge-preserves runtime config).

**Stated boundary (from its own commit message):** does NOT capture autonomous-run spend, does NOT fix the Paperclip loop; container stays at `desiredCount=0`.

### PR #830 — deploy fix (squash `38da37c5`)
#826 added an account-global Cost Anomaly Detection monitor (W53) inside the *per-env* observability stack → CFN collision → rolled back `isol8-dev-observability`. #830 removed W53 (kept W52). Deployed green.

### Current prod state
- The founders' org container is at **`desiredCount=0`** (the only thing currently stopping live spend).
- #826's backend + the W52 alarm are deployed.

---

## 2. The actual problems

### 2a. AWS side — the metering only ever captured *web chat*
Root cause (workflow-verified, adversarially): **billing is coupled to the web-frontend WebSocket transport, not to the act of running an agent.** The single billing trigger (`connection_pool.py` lifecycle `phase=="end"` → deduct) requires BOTH:
1. **A live backend pool connection** — it's web-coupled (`_GRACE_PERIOD=30s` idle teardown). No web user → no connection → no listener.
2. **The container to *broadcast* the terminal event** — it only does so for `isControlUiVisible==true` runs (webchat, cron). Autonomous runs (Paperclip heartbeat/issue, channels) set `isControlUiVisible=false` and deliver only to per-session subscribers — never to an operator connection.

**Capture matrix (what's billed today):**

| Path | Billed? |
|---|---|
| Webchat / desktop chat | YES |
| Paperclip heartbeat / issue / cron / channels / aux-LLM | **NO** |

All 917 prod credit-deductions are webchat. The 24/7 runaway floor (a *failing* Friday heartbeat loop — terminal fires `phase=="error"`, which billing doesn't even trigger on) was **entirely unbilled**. And anything in the container is the tenant's environment → **tenant-removable, not billing-grade.**

**Conclusion:** the meter must live *outside* the container, at the AWS API boundary, and must capture every run regardless of how the container is reached.

### 2b. Ledger side — our hand-rolled DDB ledger + the wrong Stripe tool
- **Our DDB `credit_ledger` has real reliability holes** (all verified in code): `top_up` non-idempotent (`credit_ledger.py:111-114`); `deduct` lossy overdraft fallback (`:205-211`); `bedrock_invocation_id` accepted but no dedup guard/GSI; `adjustment` non-atomic read-modify-write; `issue_credit_for_owner` writes the Stripe *customer balance*, not the ledger (admin grants don't unblock). It is a money ledger we must keep correct ourselves — audit, idempotency, disputes, cost.
- **The real-time gate itself works** — `get_balance(consistent=True)` strong-consistent read on the chat hot path (`connection_pool.py:1905`), hard-block at `<= 0`. This is the one piece worth keeping in some form.
- **Stripe-native usage-based billing is the WRONG tool for a prepaid wallet** (verified against the doc tree): credit grants apply only to **metered prices on a subscription**, draw down only at **invoice finalization** (can't gate), and Stripe's terms **forbid using billing credits as stored value / a digital wallet** — which is exactly our product. Off the table.
- **Metronome** (Stripe-acquired Jan 2026) natively supports **prepaid credit wallets with usage burn-down + auto-recharge + payment-gated credits**, runs **alongside our existing Stripe subscription** (same Customer), and is a managed, battle-tested credit ledger. It is what OpenAI/Anthropic use. **But** it is push-based — it does NOT extract Bedrock usage and cannot gate AWS; those stay ours.

**Conclusion:** stop maintaining a home-grown money ledger. Make **Metronome the ledger system-of-record** and delete ours. Keep only a disposable local *fast-gate flag* (Metronome's own recommended pattern), because Metronome's balance read is async and can't back a synchronous per-chat gate.

---

## 3. Where we want to go — the design

### 3.1 Principles
- **Tenant-proof:** nothing inside the container is billing-grade. Meter + enforcement live at the AWS layer; the ledger lives in Metronome.
- **Prepaid wallet, no cycle:** buy credits → deplete as Bedrock is used → cut off at 0 → buy more. (Not subscription-usage-invoicing.)
- **Extensible from day one:** the meter emits a **normalized usage event**; the ledger sits behind a **thin interface** (`deplete(event)` / `get_balance(owner)`), so the backend is swappable.
- **Managed ledger:** Metronome is the SoR; the existing DDB `credit_ledger` is **deprecated → deleted**. We never again own money-ledger reliability/audit/cost.
- **Event-based, no long-running polling workers.**

### 3.2 Architecture (components, each one job)

1. **Enforcement boundary — per-owner AIP + IAM-scoped task role.**
   At provision, mint per owner: application inference profiles (Opus/Sonnet/Haiku via `copyFrom` the system profiles) + an IAM role scoped to *only* those AIP ARNs (gated on condition key `bedrock:InferenceProfileArn`, with the underlying foundation-model ARNs for every fan-out region, and an explicit deny on `CreateInferenceProfile`). The container's `openclaw.json` model ids become the AIP ARNs. Revoking the role's Bedrock policy = a per-owner kill-switch. *(8/8 IAM policy-simulation verified.)*

2. **Meter — AWS Bedrock invocation logging → CloudWatch → Lambda → normalized usage event.**
   Enable model-invocation logging (mode "Both": CloudWatch + S3). A CloudWatch Logs **subscription filter → Lambda**, per record:
   - parse token counts **including cache** — these live in `output.outputBodyJson.usage.*` and are **offloaded to S3 when the body >100KB** (large cached prefixes), so the Lambda must read the S3 object for big records (the #1 correctness landmine);
   - price via our rate table (the part of #826 that carries forward);
   - attribute to the owner via the **AIP ARN (`modelId`) / assumed-role ARN (`identity.arn`)** — AWS-stamped, not tenant-supplied;
   - emit a **normalized usage event** and call `deplete(event)` on the ledger interface;
   - bill on **`error`/`aborted`** runs too (the runaway profile), not just `end`.
   Failures → **SQS DLQ** (nothing lost). S3 = durable replay/audit tail.

3. **Ledger interface → Metronome adapter (SoR) + local fast-gate flag.**
   - `deplete(event)` → push a Metronome usage event (idempotency = the Bedrock `requestId`; Metronome dedups within 34 days = its backdate window, so exactly-once by construction).
   - **Metronome owns** balance, drawdown, idempotency, audit, expiry, reconciliation, and Stripe invoicing/dunning/tax/Radar.
   - **Local fast-gate flag** = a disposable projection (synced from Metronome low-balance webhooks + optimistic local decrement). The synchronous chat gate reads THIS. It carries **no money-correctness burden** — if stale, Metronome is truth and the kill-switch is the backstop.

4. **Enforcement loop.** meter → `deplete()` → balance crosses 0 → **IAM revoke + `ecs update-service --desired-count 0`** (both, because IAM propagation is ~5–15s and a retry storm can overshoot; `desired=0` is the guaranteed instant stop). Pre-chat gate (web) reads the local flag synchronously.

5. **Stripe = top-up payments only.** Buy credits → Stripe Checkout → payment → fund a Metronome prepaid commit/credit. (Stripe never meters usage and never gates.)

### 3.3 Data flow
```
container (any run: chat/heartbeat/cron/channel) ──InvokeModel via AIP──► Bedrock
                                                          │ (AWS logs it — tenant can't suppress)
                                                          ▼
                              invocation log ─► CloudWatch ─► subscription filter ─► LAMBDA METER
                                                  │ (cache tokens in S3 for >100KB)   │ price (our rates)
                                                  └─► S3 (durable replay/audit tail)   │ normalized event
                                                                                       ▼
                                              ledger interface.deplete(event) ─► METRONOME (SoR)
                                                                                       │ low-balance webhook
                                                                                       ▼
                                                              local fast-gate flag ◄───┘
                                                                   │ (synchronous chat gate reads this)
                              balance ≤ 0 ─► IAM revoke AIP policy + ECS desired-count 0  (kill-switch)

   top-up: Stripe Checkout ─► payment ─► fund Metronome prepaid credit ─► flag refreshed
```

### 3.4 Key decisions + rationale
- **Meter at AWS, not in OpenClaw/container** — only tenant-proof, every-run source; the container is the tenant's environment. (The cost is: AWS logs tokens not dollars → we price; cache tokens in S3 for big records → we parse.)
- **Metronome as ledger SoR, not Stripe-native, not harden-own-forever** — Stripe-native is subscription-shaped + a stored-value ToS violation; harden-own keeps us maintaining a money ledger; Metronome is purpose-built for prepaid wallets and removes that burden. The thin interface means it's a clean adapter, not a rewrite.
- **Local fast-gate flag stays** — Metronome's balance is async (no low-latency SLA; its own docs say keep a local entitlement flag). This is a cache, not a ledger — no money-correctness burden.
- **Kill-switch = IAM revoke + `desired=0`** — IAM alone is ~5–15s eventually-consistent (leaky vs a retry storm); `desired=0` is the guaranteed stop.

### 3.5 Migration / sequencing
- **Existing DDB `credit_ledger` → deprecate → delete** once the Metronome adapter is live. Backfill existing balances into Metronome prepaid credits (idempotency-keyed per owner; do not grandfather two money systems).
- **Sequencing — LOCKED: (a) DDB interim → Metronome, phased by environment.** Metronome only grants a **sandbox** until we engage their sales team, so prod-on-Metronome is gated on that contact; we don't block the runaway fix on it.

  **Phase 1 — prod, now (the cost-critical fix):** ship the meter (AWS invocation log → normalized event) + AIP/IAM kill-switch (+ `desired=0`) + the thin ledger interface, with the **existing DDB ledger** as the interim backend. This is what stops the live runaway and finally captures autonomous-run spend + enforces it.
  - **Interim caveat:** the new every-run meter will double-deplete the DDB ledger unless its `requestId` idempotency hole is closed first (the ~30-line + 1-GSI fix) — so that one hole **must** be fixed even though the ledger is destined for deletion. The other DDB holes (lossy `deduct`, non-atomic `adjustment`, admin-grant bug) can be left if the interim is short, since Metronome supersedes them.

  **Phase 2 — dev, next (validate Metronome):** implement the **Metronome adapter** behind the interface and wire it to the **Metronome sandbox** in the dev environment. Validate end-to-end: `deplete` → balance → low-balance webhook → local fast-gate flag → kill-switch; the Stripe-checkout → fund-Metronome-credit flow; balance backfill; the one-time cutover reconciliation (local vs Metronome) for pricing-math bugs.

  **Phase 3 — prod, when ready (cutover):** after engaging Metronome sales and provisioning a prod account, switch the prod ledger backend to the Metronome adapter, backfill prod balances into Metronome prepaid credits, then **deprecate → delete** the DDB ledger.

### 3.6 Build gates — live AWS checks (need SSO re-auth before coding)
1. On a real >100KB cache-using record: are cache tokens inline or offloaded to S3, and the exact offload-reference field?
2. Does `modelId` log as the AIP id, and `identity.arn` as the per-owner assumed-role ARN (attribution correctness) + the regional FM-ARN set for the IAM policy?
3. Kill-switch end-to-end timing (IAM revoke + `desired=0` → actual stop).

### 3.7 Out of scope (explicit)
- **Paperclip heartbeat loop fix** — *not* a cost lever in this model; spend is bounded by credits + the kill-switch. The failing-run retry storm is a separate container-reliability bug. (We deliberately do NOT throttle autonomy.)
- **Standing reconciliation sweep** — Metronome's idempotent ingestion is the accuracy mechanism; only a one-time cutover validation + a lightweight drift monitor.
- **Stripe-native credit grants / meters** — wrong shape + stored-value ToS.

### 3.8 Testing considerations
- Meter pricing: cache-correct incl. the S3-offload path; `error`/`aborted` billing; per-owner attribution.
- Idempotency: `requestId` dedup (meter→ledger) end-to-end; replay safety.
- Gate: local flag blocks at ≤0; kill-switch fires (IAM + `desired=0`) on crossing.
- Metronome adapter: deplete/balance/webhook sync; the swap from the DDB interim backend.

---

## 4. Decisions (resolved) + remaining checks
1. **Sequencing — RESOLVED: (a) DDB interim → Metronome, phased by environment** (Phase 1 prod DDB, Phase 2 dev Metronome-sandbox, Phase 3 prod Metronome after sales). See §3.5.
2. **Metronome access — RESOLVED for now: sandbox only** until we engage their sales team. Dev (Phase 2) uses the sandbox; prod (Phase 3) is gated on the sales contact + a prod account. Pricing/terms come from that conversation.
3. **Remaining:** confirm the 3 live-AWS checks (§3.6) are run (needs SSO re-auth) before the Phase-1 meter pricing/attribution/kill-switch code is finalized.
