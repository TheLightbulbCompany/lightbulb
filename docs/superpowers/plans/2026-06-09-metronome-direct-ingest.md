# Metronome Direct-Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> to implement task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Meter Lambda sends Bedrock usage straight to Metronome (per-env Lambda →
per-env token); backend keeps only gate/kill/lifecycle; delete the JWT hop and the
interim DDB ledger.

**Architecture:** see `docs/superpowers/specs/2026-06-09-metronome-direct-ingest-rework.md`.

**Salvage sources (branches kept on origin until PR-A/PR-C open):**
- `origin/phase-2-metronome-adapter` (#833): `metronome_client.py`,
  `metronome_fast_gate.py`, `metronome_funding.py`, `metronome_provisioning.py`,
  `routers/metronome_webhook.py`, auth-stack secrets (`metronomeApiKey`,
  `metronomeWebhookSecret`), database-stack fast-gate table, service-stack env
  wiring, all matching tests.
- `origin/phase-3-metronome-cutover` (#834): the deletion lists
  (`credit_ledger.py`, `ddb_ledger_backend.py`, connection_pool deduct, credits
  tables), CreditsPanel balance-source change. (Its backfill script is NOT
  salvaged — decision 2026-06-09: no balance migration; DDB balances abandoned,
  dev = test money, prod org dead at desired=0.)
- Worktree (uncommitted): `apps/infra/lambda/bedrock-usage-meter/index.py`
  top-level cache-path fix (KEEP — goes into PR-B with its test rewrite).

**NOT salvaged:** `metronome_ledger_backend.py`, the `LedgerBackend`-behind-interface
shape, `internal_usage.py` changes — ingestion has left the backend.

---

## PR-A — backend: Metronome lifecycle + gating (branch `metronome-lifecycle-gating`, off main)

No behavior change while `BEDROCK_ENFORCEMENT_LIVE=False` (default). DDB ledger
keeps running as today's biller until PR-C.

### Task A1: cherry-pick the #833 base modules
- [ ] `git checkout -b metronome-lifecycle-gating origin/main`
- [ ] Copy from `origin/phase-2-metronome-adapter`:
      `apps/backend/core/billing/metronome_client.py`,
      `metronome_fast_gate.py`, `metronome_funding.py`,
      `metronome_provisioning.py`, `apps/backend/routers/metronome_webhook.py`
      + their `tests/unit/**` counterparts + `core/config.py` settings block +
      `main.py` router mount + infra: auth-stack secrets, database-stack
      fast-gate table, service-stack env wiring (+ infra tests).
      `git show origin/phase-2-metronome-adapter:<path> > <path>` per file.
- [ ] Run: `uv run pytest tests/unit/billing tests/unit/routers/test_metronome_webhook.py -q` → PASS
- [ ] Commit: `feat(billing): Metronome client + lifecycle modules (salvaged from #833)`

### Task A2: strip the dead ledger shape from the salvage
- [ ] Do NOT copy `metronome_ledger_backend.py` / `ledger_interface.py` changes.
- [ ] `metronome_fast_gate.py`: delete the optimistic-`decrement` path (no backend
      deplete exists anymore); flag is set by webhook + re-armed by top-up; add
      `is_stale(row, max_age_s)` helper for the lazy poll. Update its tests.
- [ ] Commit: `refactor(billing): fast-gate is webhook-fed only (no backend deplete)`

### Task A3: provision-time Metronome customer + alert
- [ ] `metronome_provisioning.ensure_metronome_customer(owner_id)`:
      create customer (`ingest_aliases=[owner_id]`) + per-customer low-balance
      alert (`POST /v1/alerts/create`, `alert_type=low_remaining_credit_balance_reached`,
      threshold from `settings.METRONOME_LOW_BALANCE_THRESHOLD_CENTS`) + Stripe
      billing-provider link. Idempotent (alias-conflict ⇒ adopt). Persist
      `metronome_customer_id` on the billing row.
- [ ] Call site: `core/services/user_provision.py` owner path (after billing row
      exists), best-effort + retried by next status poll — provisioning must not
      hard-fail on a Metronome 5xx.
- [ ] Tests: created-once idempotency, alias conflict adoption, 5xx tolerated.
- [ ] Commit: `feat(billing): Metronome customer + low-balance alert at provision`

### Task A4: webhook → flag + kill-switch
- [ ] `routers/metronome_webhook.py` (salvaged HMAC verify stays): on
      `alerts.low_remaining_credit_balance_reached` with `remaining_balance <= 0`:
      `fast_gate.set_balance(owner, remaining)`; if `settings.BEDROCK_ENFORCEMENT_LIVE`
      → `kill_owner_bedrock(owner)` (reuse `enforce_after_deplete`'s
      already-stopped short-circuit; move that logic into
      `core/services/bedrock_enforcement.enforce_on_low_balance`).
- [ ] Top-up success (`metronome_funding` post-payment): `restore_owner_bedrock`
      + `fast_gate.set_balance(owner, new_balance)` (closes the #832-review
      "restore wired into top-up" gate).
- [ ] Tests: signature reject, balance>0 no-kill, flag-set, kill fired once,
      enforcement-flag off ⇒ flag set but NO kill.
- [ ] Commit: `feat(billing): low-balance webhook drives gate flag + kill-switch`

### Task A5: balance display (gate reads UNCHANGED until PR-C)
- [ ] Do NOT touch `provision_gate`/pre-chat reads in PR-A — they keep reading
      the DDB ledger exactly as today (simplest transition; PR-C swaps them to
      the fast-gate flag in the same PR that deletes the ledger).
- [ ] `routers/billing.py` credits endpoint: include `metronome_balance` field
      (display-only, via `getNetBalance`, None-on-error) alongside the DDB number.
- [ ] Tests: display field present, Metronome-down ⇒ None not 500.
- [ ] Commit: `feat(billing): surface Metronome balance read (display-only)`

### Task A6: verify + PR
- [ ] `uv run pytest -q` (full backend), `npx jest`, `cdk synth`, ruff.
- [ ] Open PR-A; body maps salvage → #833 and states "closes #833 on merge".

## PR-B — meter stack: two Lambdas, direct ingest (branch `meter-direct-ingest`, off main after PR-A merges)

### Task B1: Lambda rewrite (`apps/infra/lambda/bedrock-usage-meter/index.py`)
- [ ] Keep the uncommitted top-level cache-path `_extract` (already in worktree).
- [ ] Replace `_post_to_backend` with `_ingest_to_metronome(parsed)`:
      ```python
      def _ingest_to_metronome(parsed):
          event = {
              "transaction_id": parsed["request_id"],
              "customer_id": parsed["owner_id"],          # ingest_alias
              "timestamp": parsed["occurred_at"],
              "event_type": "bedrock_invocation",
              "properties": {k: str(v) for k, v in {
                  "tier": parsed["tier"],
                  "model_id": parsed["model_id"],
                  "input_tokens": parsed["input_tokens"],
                  "output_tokens": parsed["output_tokens"],
                  "cache_read_tokens": parsed["cache_read_tokens"],
                  "cache_write_tokens": parsed["cache_write_tokens"],
                  "region": parsed["region"],
                  "run_status": parsed["run_status"],
              }.items()},
          }
          req = urllib.request.Request(
              os.environ.get("METRONOME_INGEST_URL", "https://api.metronome.com/v1/ingest"),
              method="POST", data=json.dumps([event]).encode(),
              headers={"Content-Type": "application/json",
                       "Authorization": f"Bearer {_metronome_token()}"})
          urllib.request.urlopen(req, timeout=10)
      ```
- [ ] Add `_resolve_owner(record)`: one DDB GetItem on `os.environ["AIP_OWNERS_TABLE"]`
      keyed `aip_arn = record["modelId"]` → `(owner_id, tier)`; miss → DLQ +
      `meter.unattributed` + continue (NEVER ingest unattributed).
- [ ] `_metronome_token()`: cold-start fetch from `METRONOME_API_KEY_SECRET_ARN`
      (same pattern as the old `_meter_token`); literal `METRONOME_API_KEY` env
      override for tests. Delete `_meter_token`/`ALB_DNS_NAME` use.
- [ ] Module docstring: rewrite for direct ingest + the two-Lambda/env-filter design.

### Task B2: Lambda tests (`test_index.py` + fixtures)
- [ ] Fixture `inline_record.json`: add top-level `input.cacheReadInputTokenCount`/
      `cacheWriteInputTokenCount` (mirror live record); delete
      `truncated_output_record.json` + its two tests (no truncation path).
- [ ] Rewrite: cache-from-top-level test (non-zero guard stays), nested-fallback
      test, owner-resolution test (mock DDB), unattributed→DLQ test, ingest
      payload-shape test (transaction_id/customer_id/stringified properties,
      Bearer token, URL), transport-error→DLQ test (kept), unparseable→DLQ (kept).
- [ ] `python -m pytest apps/infra/lambda/bedrock-usage-meter -q` → PASS.
- [ ] Commit: `feat(meter): Lambda ingests direct to Metronome (top-level cache counts)`

### Task B3: meter stack rework (`bedrock-meter-stack.ts`)
- [ ] TWO functions from the same asset: `MeterFnDev` / `MeterFnProd` —
      NO `vpc`/SG (drop both + ALB egress + alb props), env per fn:
      `METRONOME_API_KEY_SECRET_ARN` (dev→`isol8/dev/metronome_api_key`,
      prod→`isol8/prod/metronome_api_key`, referenced by NAME via
      `Secret.fromSecretNameV2` — cross-stage by-name per codebase posture),
      `AIP_OWNERS_TABLE` (`isol8-{env}-bedrock-aip-owners`), per-fn DLQ + grants
      (DDB GetItem by table ARN, secret read + KMS decrypt).
- [ ] Rename the log group `/isol8/prod/bedrock-invocation` →
      **`/isol8/bedrock-invocation`** (env-neutral — it receives BOTH envs'
      records; the old name was a prod-stage-instantiation artifact). DESTROY
      policy + the logging-config custom resource make this a clean replace;
      delete the old group post-deploy if CFN leaves it.
- [ ] TWO subscription filters on the one log group:
      dev = `'"application-inference-profile" "isol8-dev-owner-bedrock"'`,
      prod = `'"application-inference-profile" "isol8-prod-owner-bedrock"'`.
      Comment: consumes BOTH CW filter slots (documented constraint).
- [ ] Alarms: per-env DLQ-depth + the skip metrics (unattributed/unknown_model/
      transport_error/parse_failed — `output_body_truncated` is DELETED), wired
      to the existing `isol8-{env}-alerts-warn` topics (dev alarms → dev topic).
- [ ] Drop `meterTokenSecretName`/`meterKmsKeyArn`/`vpc`/`alb`/`albSecurityGroup`
      props; update `isol8-stage.ts` instantiation (stack stays prod-stage-only +
      account-scoped — it now contains both env Lambdas).
- [ ] Update `bedrock-meter-stack.test.ts`: 2 fns, 2 filters w/ env terms, 0 SG,
      0 VPC config, alarms→both topics, no SNS topics minted.
- [ ] `npx jest && npx cdk synth` → PASS. Commit.

### Task B4: backend deletions
- [ ] Delete `routers/internal_usage.py` + `tests/unit/routers/test_internal_usage.py`;
      unmount in `main.py`.
- [ ] Delete `service_token.mint_meter`/`verify_meter`/`METER_TOKEN_KIND` + tests.
- [ ] auth-stack: delete `bedrockMeterToken` secret (+ its test assertions).
- [ ] Repurpose `METER_IS_LIVE` → `BEDROCK_ENFORCEMENT_LIVE` (default False);
      `connection_pool` legacy-deduct gate now keys on the new name (deduct still
      ACTIVE until PR-C — only renamed).
- [ ] Full backend pytest + jest + synth. Commit. Open PR-B.

### Task B5: post-merge runbook (PR-B body) — per env (sandbox first)
- [ ] Operator: `aws secretsmanager put-secret-value` the sandbox token into
      `isol8/dev/metronome_api_key`, prod token into `isol8/prod/metronome_api_key`
      + the webhook signing secrets (user runs via `!` — tokens never in chat).
- [ ] Metronome console per env: 4 SUM billable metrics (grouped by `tier`,
      filtered `event_type=bedrock_invocation`) + rate card (AWS `us.` CRIS ×
      1.4) + webhook endpoint (spec §4).
- [ ] Wire `METRONOME_RATE_CARD_ID` env (from the console rate card) and flip
      `METRONOME_LIFECYCLE_ENABLED=true` for that env (service-stack env vars —
      via CDK, never console edits).
- [ ] Validate in dev sandbox (these are the review-flagged UNVERIFIED items):
      chat → event visible with correct tokens incl. cache; **denomination is
      cents** at all four boundaries (alert threshold, commit amount, webhook
      remaining_balance, getNetBalance); **add_commits schema** (PREPAID +
      access_schedule + description) accepted; contract-create schema accepted;
      alert fire-once behavior + whether `-zero` fires after `-warn` for the
      same burn-down; balance/alert webhook latency (sizes kill-switch lag).

## PR-C — delete the DDB ledger (branch `metronome-cutover`, off main after PR-B; ⛔ gated)

**Gate (both required):** sandbox e2e validated (ingest→rated→balance→alert
webhook→gate flip observed in dev); prod rate card configured. **NO backfill**
(decision 2026-06-09: balances abandoned — dev is test money, the only prod
org's container is dead at desired=0; owners start fresh via the top-up flow).

### Task C2: flip + delete
- [ ] `BEDROCK_ENFORCEMENT_LIVE` default True. Swap `provision_gate` + pre-chat
      reads from the DDB ledger to the fast-gate flag, with lazy `getNetBalance`
      refresh when the row is stale (> `METRONOME_GATE_STALENESS_S`, default
      900s; Metronome-down ⇒ use stale row, never 500 the gate).
- [ ] Delete: `credit_ledger.py`, `ddb_ledger_backend.py`, `ledger_interface.py`
      (+ `get_ledger` callers — `provision_gate`, billing router → fast-gate/
      `getNetBalance`), connection_pool deduct block (+ its tests), `credits` +
      `credit-transactions` tables from database-stack, `bedrock_pricing` demoted
      to display/reconciliation with a docstring saying so.
- [ ] CreditsPanel: balance from `metronome_balance` (salvage #834's change).
- [ ] `reconcile_bedrock_vs_ledger.py` → compares CE marketplace total vs
      Metronome (adapt #834's version).
- [ ] Full verification suite; open PR-C with the ⛔ gate checklist in the body.

---

## Self-review notes
- Exactly-once during transition: legacy connection_pool deduct (DDB) and
  Metronome ingest run in PARALLEL until PR-C — two ledgers, one display.
  Acceptable: DDB stays the billed truth until cutover; Metronome accrues
  shadow usage. No double-charge because only the DDB number gates/bills
  pre-cutover, only Metronome after.
- Unattributed dev noise in the prod Lambda dies with the env-split filters
  (dev records never reach the prod Lambda).
- The 34-day dedup window only matters for DLQ redrives older than 34 days —
  document on the DLQ alarm runbook.
