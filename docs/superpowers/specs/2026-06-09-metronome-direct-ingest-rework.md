# Metronome Direct-Ingest Rework — Design

> **Supersedes** the metering/ledger layers (§3.2.3–§3.5) of
> `docs/superpowers/specs/2026-06-07-bedrock-metering-enforcement-metronome-design.md`.
> The enforcement layer (per-owner AIP + condition-scoped IAM role + kill-switch
> primitives) and the Bedrock invocation-logging capture layer are UNCHANGED and
> already live (PRs #832/#836/#837/#838/#839).

**Decision (2026-06-09, user-driven):** the backend is NOT a billing brain. The
meter Lambda sends raw usage straight to Metronome (`/v1/ingest`); Metronome
rates it (rate card = AWS rate × markup, configured by us), maintains the
prepaid balance, and signals thresholds via webhooks. Stripe collects money.
The backend keeps exactly three billing-adjacent jobs: **gate** inference on a
webhook-fed local flag, **execute** the kill-switch, and **lifecycle plumbing**
(Metronome customer at provision; Stripe payment → Metronome commit).

**Why this beats the previous design** (Lambda → backend → LedgerBackend):
- Deletes the Lambda→backend hop and with it the `bedrock_meter` JWT, the
  `mint_meter()` manual ceremony (whose un-minted placeholder broke prod with a
  silent 401 — found live 2026-06-09), and `routers/internal_usage.py`.
- Deletes the interim DDB ledger and the backend's pricing-on-the-money-path
  (`bedrock_pricing` becomes display/reconciliation-only).
- One billing brain (Metronome+Stripe) instead of two to keep in sync.
- Exactly-once moves to Metronome's native `transaction_id` dedup
  (34-day window) keyed on Bedrock `requestId` — the DDB TransactWriteItems
  idempotency machinery is no longer load-bearing.

---

## 1. Architecture

```
                    ┌── account-level, us-east-1 (source-region logging, #837) ──┐
container ─→ Bedrock ─→ /isol8/prod/bedrock-invocation (ONE log group, both envs)
                            │
              ┌─────────────┴──────────────┐   CW allows exactly 2 subscription
              │ filter DEV                 │ filter PROD          filters/group —
              │ "application-inference-    │ "application-inference-  we use both
              │  profile" &&               │  profile" &&             slots.
              │ "isol8-dev-owner-bedrock"  │ "isol8-prod-owner-bedrock"
              ▼                            ▼
        MeterFnDev                   MeterFnProd          (same code, env-var
        sandbox token                live token            config — ZERO
        dev aip-owners table         prod aip-owners table conditionals)
        dev DLQ                      prod DLQ
              │                            │
              └────────────┬───────────────┘
                           ▼
              Metronome POST /v1/ingest
              transaction_id = requestId (34-day exactly-once)
              customer_id    = owner_id  (ingest_alias)
              properties     = {tier, input/output/cache_read/cache_write tokens,
                                region, run_status} — ALL STRINGIFIED

Metronome alert webhook (low_remaining_credit_balance_reached)
   ─→ backend /webhooks/metronome (HMAC Metronome-Webhook-Signature)
        ─→ set fast-gate flag + fire kill-switch (IAM revoke / desired=0)

Stripe top-up (Checkout) ─→ backend webhook ─→ Metronome payment-gated commit
                                               (credits released on Stripe-paid)
```

**Env split mechanism (verified live 2026-06-09):** every invocation record
carries `identity.arn` = the per-owner role
(`…assumed-role/isol8-{env}-owner-bedrock-{owner}/…`), so a CloudWatch term
filter distinguishes envs. Dev traffic → sandbox Metronome; prod → live.
Metronome env is **token-scoped** (same base URL; the API key decides sandbox
vs prod — research §5), so the two Lambdas differ ONLY in env vars:
`METRONOME_API_KEY_SECRET_ARN`, `AIP_OWNERS_TABLE`, `DLQ_URL`.

**Constraint:** this consumes both subscription-filter slots on the log group.
Any future third consumer must fan out behind one of the Lambdas, not add a
filter.

## 2. The meter Lambda (per env)

Dumb transport + attribution. Per record:
1. **Parse** top-level counts: `input.inputTokenCount`, `output.outputTokenCount`,
   `input.cacheReadInputTokenCount`, `input.cacheWriteInputTokenCount`
   (live-confirmed equal to the response body's `usage.*`; never needs
   `outputBodyJson`, so the >100KB-offload edge is gone — fix already written).
2. **Attribute**: one GetItem on `isol8-{env}-bedrock-aip-owners`
   (`aip_arn` → `{owner_id, tier}`). Miss → DLQ + `meter.unattributed` (a
   revenue leak must alarm, never vanish into Metronome's unmatched-events bin).
3. **Ingest**: `POST /v1/ingest` (urllib, Bearer = env's token), event per §1.
   Non-200 → DLQ + `meter.transport_error`; retry-safe via `transaction_id`.

**No VPC.** The Lambda only needed VPC for the internal ALB. Metronome, DDB,
SQS, Secrets Manager are all public HTTPS endpoints → drop `vpc`/SG entirely
(also kills the SG-description class of deploy failure and the ALB egress rule).

IAM: DDB GetItem on the env's aip-owners table, SQS SendMessage on its DLQ,
secretsmanager:GetSecretValue + kms:Decrypt for its token secret. No
`bedrock_meter` JWT anywhere.

## 3. The backend's three remaining jobs

1. **Gate** (hot path): `provision_gate` + pre-chat check read the **fast-gate
   flag** (DDB row per owner: `remaining_microcents`, `blocked`) — salvaged
   from #833's `metronome_fast_gate.py` MINUS the optimistic-decrement (there
   is no backend deplete anymore). Fed by: low-balance webhook (authoritative
   set), top-up success (re-arm), and a lazy poll of `getNetBalance` on
   gate-read when the row is stale (>N min) — because Metronome has **no
   verified recovery webhook** (research §3).
2. **Kill-switch execution**: the webhook handler calls what
   `enforce_after_deplete` does today (revoke + desired=0 via
   `kill_owner_bedrock`); `restore_owner_bedrock` wired into top-up success.
3. **Lifecycle**: at provision, create Metronome customer with
   `ingest_aliases=[owner_id]` + per-customer low-balance alert
   (`POST /v1/alerts/create`) + Stripe billing-provider link; on Stripe top-up
   webhook, `add_commits` with `payment_gate_config: STRIPE`. Salvaged from
   #833's `metronome_provisioning.py` / `metronome_funding.py` /
   `metronome_webhook.py` / `metronome_client.py`.

Balance display (Credits panel): backend reads `getNetBalance` (not hot-path).

## 4. Metronome console configuration (per env: sandbox + prod)

Not code — a runbook step, done once per env before that env's ingest matters:
- 4 **billable metrics** (SUM over `input_tokens` / `output_tokens` /
  `cache_read_tokens` / `cache_write_tokens`), filtered
  `event_type = bedrock_invocation`, grouped by `tier`.
- **Rate card**: per (metric, tier) price = AWS Bedrock `us.` CRIS rate ×
  `BILLING_MARKUP` (1.4). Rates live HERE — `bedrock_pricing._RATES` stops
  being the money path (kept only for display estimates + the daily
  CE-vs-Metronome reconciliation).
- **Webhook endpoint** (backend `/webhooks/metronome`) + signing secret →
  `isol8/{env}/metronome_webhook_secret` (secret already created by #833's
  auth-stack change).

## 5. Deletions (the payoff)

| Deleted | Why |
|---|---|
| `routers/internal_usage.py` (+ tests) | no Lambda→backend hop |
| `service_token.mint_meter`/`verify_meter`/`METER_TOKEN_KIND` | no JWT |
| `isol8/{env}/bedrock_meter_token` secret + Lambda KMS/secret wiring | no JWT |
| Lambda VPC config + SG + ALB egress + `ALB_DNS_NAME` env | no ALB call |
| `ledger_interface.py`/`get_ledger` + `ddb_ledger_backend.py` + `credit_ledger.py` | Metronome is the ledger |
| `credits` + `credit-transactions` DDB tables | after backfill |
| `connection_pool` legacy deduct path | per #834, unchanged |
| #833's `metronome_ledger_backend.py` | ingestion left the backend |
| `METER_IS_LIVE` as meter gate | repurposed: `BEDROCK_ENFORCEMENT_LIVE` (gate+kill on/off; ingest always on = shadow by default) |

## 6. Sequencing & gates

- **PR-A** backend lifecycle + gating (salvage #833; no LedgerBackend, no
  behavior change while enforcement flag is off; gate reads stay on the DDB
  ledger untouched until PR-C).
- **PR-B** meter stack rework: two Lambdas + two filters, direct ingest, no
  VPC; deletes internal_usage + JWT machinery; renames the log group to the
  env-neutral `/isol8/bedrock-invocation` (it carries BOTH envs' records).
  Folds in the already-written top-level cache-path fix.
- **PR-C** delete the DDB ledger (rework #834): flip enforcement flag, swap
  gate reads to the fast-gate flag, delete ledger code + tables.
  **NO BACKFILL — decision 2026-06-09:** existing DDB balances are abandoned
  (dev = test money; the only prod org's container is dead at desired=0 — the
  $4k runaway is why this work exists). Owners start fresh on Metronome
  commits via the normal top-up flow.
  ⛔ **Gated on:** sandbox end-to-end validated in dev (ingest → rated →
  balance moves → alert webhook → gate flips) + prod rate card configured.
- #833/#834 are **closed** when PR-A/PR-C open (salvage via cherry-pick/copy —
  branches stay on origin until then).

## 6b. Auth model — no self-minted JWTs anywhere in billing

After PR-B the billing pipeline contains **zero JWTs we mint**:
- Lambda → DDB / SQS / Secrets Manager: **IAM role** (native).
- Lambda / backend → Metronome: the **vendor API key** (per env; an external
  SaaS cannot accept AWS IAM — this secret is irreducible, but it is
  vendor-issued and set once; no minting ceremony to forget).
- Metronome → backend webhook: Metronome's **HMAC signature**
  (`Metronome-Webhook-Signature`), verified with the per-env webhook secret.
- The `bedrock_meter` JWT, `mint_meter()`, and its placeholder-secret footgun
  (the silent prod 401) are deleted outright.
- `service_token.py`'s `paperclip_service` kind is unrelated to billing and
  stays.

## 7. Open verifications (carry-over + new)

1. Ingest→balance→alert-webhook latency (sandbox; sizes the kill-switch lag).
2. Ingest batch limit; max timestamp backdate (DLQ-replay window).
3. No recovery webhook → re-arm via top-up + lazy poll (designed in, verify).
4. Properties stringification tolerance (docs say stringify; SDK example sends
   ints — we stringify).
5. Sandbox feature parity for alerts/commits (sales-gated items, research §5).
