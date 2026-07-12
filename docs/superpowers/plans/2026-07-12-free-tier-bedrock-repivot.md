# Free Tier v2 — Bedrock Sonnet, AWS-Layer Metering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-point the no-card free tier from Meta Muse Spark onto **Bedrock Claude Sonnet** with a single per-owner inference profile, move the $3/day counter from our WebSocket plumbing to the **Bedrock invocation-log meter Lambda**, and enforce the cap with the codebase's own documented hard stop (**revoke the owner's AIP + `desiredCount=0`**). Net effect: the counter becomes complete by construction, the stop becomes unbypassable, and the entire pre-send reserve subsystem — the source of most of the 15 defects the whole-branch review found — gets **deleted** rather than fixed.

**Architecture:** A trial owner is provisioned exactly like a paid Bedrock owner, except they get **one** application-inference profile (the `sonnet` tier) instead of three. That single AIP is the load-bearing security boundary: `build_owner_bedrock_policy` pins them to that model at the IAM layer (raw foundation-model invoke is denied, `CreateInferenceProfile` is denied), so a trial user cannot escalate to Opus even by editing their own `openclaw.json` — and unlike Meta there is no shared fleet API key to exfiltrate from the container env. Every model call the container makes routes through that AIP under the per-owner ECS task role, which is exactly what the existing `bedrock-usage-meter` Lambda's CloudWatch subscription filter keys on — so the meter sees **every** invocation (webchat, cron, channels, Paperclip/Teams runs with no browser tab, memory compaction, embeddings, the raw RPC proxy) with AWS-sourced, cache-inclusive token counts, deduped on Bedrock's `requestId`. The Lambda prices each invocation locally, ADDs it to the daily-spend counter we already built, and on cap-cross revokes the AIP and stops the container. Our gates degrade to what they always really were: a fast, synchronous **read** of that counter, for UX.

**Tech Stack:** Python 3 / FastAPI (backend), Python (meter Lambda), AWS CDK (TypeScript), pytest + moto, React 19 / TypeScript.

## Global Constraints

- **The meter Lambda is the SOLE writer of the daily-spend counter.** No in-band path may ADD to it. `settle`'s counter writes are deleted, not kept "as a backstop" — two writers means double-counting.
- **Money unit stays MICROCENTS** (1 USD = 1,000,000). `CAP_MICROCENTS = 3_000_000` ($3/day). The counter attribute (`meta_daily_spend_microcents.{YYYY-MM-DD}` on `billing-accounts`) already has exactly the right shape — **do not change the schema**, only its writer. Rename the *module*, not the attribute (a rename would strand live dev rows).
- **Replay safety is mandatory, not a nit.** CloudWatch Logs retries whole batches on any raised error and DLQ redrives are declared "always safe to replay" — a DynamoDB `ADD` has no dedup. Every counter write MUST be guarded by a per-`requestId` conditional put. Skipping this double-counts and false-locks-out users.
- **Trial owners are never ingested to Metronome.** They have no Stripe customer and therefore no Metronome contract. The Lambda branches on a `plan` attribute it already reads for free (the aip-owners reverse row), and skips ingest for `plan == "trial"`.
- **Paid behavior must not change.** `ensure_owner_aips` still mints three tiers by default; the meter still ingests to Metronome for paid owners; `is_owner_blocked` / the Metronome credit gate is untouched. Every task must assert the paid path is byte-identical.
- **The emitted `openclaw.json` must validate against the pinned 7.1 schema** — `tests/chaos/invariants/test_config_schema.py` samples every `provider_choice`; run it after any `config.py` change. A wrong key crash-loops real containers.
- **`trial_local` is a `subscription_status`, never a `provider_choice`.** It resolves to a Bedrock-trial *config mode*, exactly as `meta` did.
- Backend tests: `cd apps/backend && uv run pytest <path> -v`. Frontend: SINGLE vitest files only, then `node_modules/.bin/tsc --noEmit`. Lambda tests live beside it under `apps/infra/lambda/bedrock-usage-meter/`.
- Branch: continue on `feat/free-tier-daily-cap`. Commit per task. **No pushes without user approval.**

---

## THE ENFORCEMENT MODEL (settled 2026-07-12 — read this before writing any code)

**The kill switch is load-bearing. The config bounds are cheap insurance. Do not confuse them.**

The container holds its own credentials and calls Bedrock directly — the backend is not on the inference path. So the ONLY thing that provably stops spend is removing the container's ability to invoke: `kill_owner_bedrock` (revoke the per-owner inline IAM policy + `desiredCount=0`). **That function is already written in `ecs_manager.py` and has never been called once.** Wiring it is the single change that makes a free tier safe.

Loss is then bounded by `burn rate × detection window`, and both are known: the Bedrock invocation log reaches our CloudWatch subscription in seconds-to-3-minutes (AWS's documented ceiling), and IAM propagation is 5-15s. An honest user overshoots by cents; an adversary running every lane in parallel overshoots by ~$60 and is then cut off, needing a fresh signup to try again. Bounded, observable, per-account.

The config bounds below shrink that per-incident blast radius (~$60 → ~$5). They are NOT the fence — the agent has `exec: full` and can rewrite its own `openclaw.json`. They cost nothing because they are values in a file we already write at provision. Set them; do not rely on them.

**Rejected, explicitly, do not reintroduce:** a pre-send reserve (deleted in Task 0 — it was the source of most of a 15-defect review); an RPC method allowlist as a *cost* control (the expensive paths — agent tool loops, cron, Paperclip, channels — never touch the RPC proxy, so it bounds nothing); Metronome-balance-as-fence (the vendor's own docs: threshold alerts are evaluated on a ≥3-minute polling tick and fire "within 5 minutes", Metronome never blocks, and usage past an exhausted credit is "rated and charged in arrears" — i.e. an ungated exhausted trial keeps burning real AWS money on an invoice nobody collects).

---

## EXECUTION ORDER — delete first, then build

**Task 0 (below) runs FIRST.** It removes the pre-send reserve subsystem and the entire Meta provider path in one coherent change, so every task after it builds on a clean tree instead of on machinery being torn out. It replaces the old "Part 3" (Tasks 8 + 9), which were written as trailing cleanup — they are merged here and hoisted to the front, because deleting `meta_cap`'s reserves while `config.py` still emits a `meta` provider leaves a broken intermediate state.

Order: **Task 0 (delete)** → Part 1 (Bedrock trial provider, Tasks 1-3) → Part 2 (meter counter + stop, Tasks 4-7) → Part 4 (surviving defects, Tasks 10-14) → Part 5 (gate).

Branch history: the 59 commits on `feat/free-tier-daily-cap` include ~30 that survive this re-pivot (scale-to-zero, conversion, expiry, telemetry, Teams guards, liveness). Do **not** rewrite history to "throw them away" — delete forward, and squash the branch into a clean commit set before opening the PR. PR #1219 (remote head `12715613c`) contains only the superseded Meta-cap design and should be closed in favor of a fresh PR.

---

## Task 0: THE DELETION — pre-send reserves + the entire Meta path — ✅ DONE (commit dd73852ea, 45 files, +685/-2747, 3523 tests green)

**Files (delete / gut):**
- `core/services/meta_cap.py` → rename to `core/services/trial_cap.py`; **DELETE** `_reserves`, `reserve_or_block`, `release`, `reconcile`, `settle`'s counter writes, the session-key `.lower()` normalization (it existed only so reserve and reconcile agreed), the `is_webchat` source branch. **KEEP** `is_over_daily_cap` (read-only; already what Teams guards, the run-allowance endpoint, the steer path and the container-wake check call) and the cap constants.
- `core/gateway/connection_pool.py` — gate_chat Layer 2 becomes a read-only `is_over_daily_cap` check (no reserve, no container-row provider read); **DELETE** the `settle` call from `_fetch_and_record_usage` and the `_settled_run_ids` dedup (nothing to dedup once nothing writes).
- `routers/websocket_chat.py` — **DELETE** every `meta_cap.release(...)` call, the side-run refusal, and `suppressCommandInterpretation`. With the meter counting every invocation at the AWS layer, `/btw` is safe for the free tier and the reason to refuse it is gone. Keep the `session_id` stamp on error frames (that fix survives).
- `core/containers/config.py` — **DELETE** the `meta` provider branch (its trial lockdown — cron off, no heartbeat, no channel re-injection — is re-homed onto the bedrock branch in Task 2, do not lose it).
- `core/billing/meta_pricing.py` — **DELETE**; fold `CAP_MICROCENTS` + `utc_today` into `trial_cap`. Drop `RESERVE_MICROCENTS`, the Meta rate table, `is_meta_model`, and the context/output clamps (the Bedrock trial config sets its own).
- `core/services/usage_service.py` — drop the `is_meta_model` branch.
- `core/containers/ecs_manager.py` + `core/services/key_service.py` + `apps/infra` — **DELETE** the `MODEL_API_KEY` injection, its Secrets Manager entry and CDK wiring. *This is also the shared-fleet-key exfiltration risk: any trial user with `exec: full` could read it out of their own container env.*
- `core/repositories/container_repo.py` / `ecs_manager.py` — drop the `provider_choice` container-row stamp added for the upgrade window; the gate no longer needs it (the predicate is `subscription_status == "trial_local"`, and the counter is written by the meter).
- Tests: delete the suites that exist only to cover the reserve subsystem; **name every deleted test in the report** so the reviewer can confirm nothing load-bearing went with them.

**KEEP (verify each still passes):** the `billing-accounts` daily-counter helpers (`add_meta_daily_spend` / `get_meta_daily_spend` — the meter Lambda becomes their caller; **do not rename the DDB attribute**, live dev rows carry it), `has_always_on_entitlement` + the hooks/channels 402s, the Teams cap guards, the run-allowance endpoint, the 3-day expiry, the conversion reprovision, scale-to-zero, ping liveness, the waking UX, and the trial telemetry.

- [ ] **Step 1: Inventory.** `grep -rn "meta_cap\|meta_pricing\|is_meta_model\|MODEL_API_KEY\|provider_choice.*meta\|\"meta\"" apps/backend apps/infra` — write the full list to the report BEFORE deleting, so the reviewer can check nothing survives and nothing extra died.
- [ ] **Step 2: Delete + rewire** in one pass (a half-deleted state does not compile).
- [ ] **Step 3: Run the full backend suite** — `uv run pytest tests/unit -q`. Expect failures ONLY in tests that assert deleted behavior; fix or delete each, and justify every deletion in the report.
- [ ] **Step 4: Chaos schema** — `uv run pytest tests/chaos/invariants/test_config_schema.py -v` (drop `"meta"` from the sampled `provider_choice` strategy).
- [ ] **Step 5: Ruff clean. Commit** — `refactor(billing): delete the pre-send reserve subsystem and the Meta provider path`

---

## Verified anchors (grounded 2026-07-11/12)

- `resolve_provider_choice` — `core/services/container_provision.py:19-34`; returns `("meta", None)` for `trial_local` today.
- `bedrock_pricing` — `_RATES` (`core/billing/bedrock_pricing.py:62`) already carries `sonnet` / `opus` / `haiku` rows; `price_from_tier(tier, …)` (`:182-209`) is **PURE** (tier → `TIER_TO_FM` → rate → microcents), no DDB — directly copyable into the Lambda.
- `ensure_owner_aips(owner_id)` — `core/billing/bedrock_aip.py:290`; mints all 3 tiers, persists via `persist_owner_aips` (`:163`) → `_put_reverse_row(table, arn, owner_id, tier)` (`:185`), which writes exactly `{aip_arn, owner_id, tier}`.
- `revoke_owner_bedrock(owner_id)` — `bedrock_aip.py:536`; deletes the per-owner role's scoped inline policy, stamps `bedrock_revoked=True`. **Its own docstring: "IAM is ~5-15s eventually consistent — Plan 4 pairs this with ECS desired-count=0 for the guaranteed instant stop."** That pairing is our enforcement. `restore_owner_bedrock` exists; its only caller (`metronome_gate._maybe_restore_bedrock`) refuses to fire without a positive Metronome balance, which a card-less trial can never have — so the trial reset needs a new trigger.
- Meter Lambda — `apps/infra/lambda/bedrock-usage-meter/index.py`: `_extract` (`:182-232`) reads `input.inputTokenCount`, `output.outputTokenCount`, `input.cacheReadInputTokenCount`, `input.cacheWriteInputTokenCount`, `requestId`, and the AIP ARN as `modelId`; `_resolve_owner_and_tier` (`:235-259`) does ONE `GetItem` on `isol8-{env}-bedrock-aip-owners` keyed by `aip_arn` → `(owner_id, tier)`; `_ingest_to_metronome` (`:262-298`) POSTs token counts with `transaction_id = requestId`. **The Lambda has NO rate table and computes no dollars today** — Metronome rates everything.
- Meter CDK — `apps/infra/lib/stacks/bedrock-meter-stack.ts`: subscription filter term pattern `"application-inference-profile" "isol8-{env}-owner-bedrock"` (`:251-259`) — it keys on the per-owner **role name + AIP**, not on any session concept. Current Lambda IAM (`:210-239`): `dynamodb:GetItem` on the aip-owners table only; `kms:Decrypt` (NOT `GenerateDataKey`); no VPC, no backend hop (deliberate, `:30-33`). **Both of CloudWatch's 2 subscription-filter slots on the log group are consumed** — a third consumer must fan out behind a Lambda.
- Counter — `billing_repo.add_meta_daily_spend` (`core/repositories/billing_repo.py:399-451`): guarded two-step (conditional map init, then `ADD meta_daily_spend_microcents.#d :delta` with `ReturnValues=ALL_NEW`) because DDB rejects SET-map + nested-ADD in one expression. The `billing-accounts` table is CMK-encrypted (`database-stack.ts:102-110`) — a *writer* needs `kms:GenerateDataKey`, which the Lambda does not have.
- IAM pin — `build_owner_bedrock_policy`: the AIP `Allow` lists only the owner's profiles; the raw-FM `Allow` is gated on `ArnEquals bedrock:InferenceProfileArn`; `CreateInferenceProfile` is explicitly `Denied`. **Minting one tier pins the owner to one model.**
- `config.py` bedrock branch **hard-fails unless opus + sonnet + haiku AIPs are all present** — this is the single blocker to a one-tier trial owner.
- Review defects that SURVIVE the re-pivot (the rest die with the reserve): `_active_runs` mixed-case store vs lowercased clear (`connection_pool.py` `mark_run_active` :335 vs the chat-terminal branch :1280) → scale-to-zero silently dead; scale-to-zero veto blind to Paperclip runs → over-kill; grace never re-arms after an in-flight run; conversion reprovision races the `/container/status` auto-retry (`routers/container.py:183-209`) → double-provision onto the EFS WAL; conversion is one fire-and-forget task with no self-heal; `POST /container/provision` (`container.py:301-319`) restarts a stopped container with **no cap check**; the generic RPC proxy (`websocket_chat.py:864` → `_process_rpc_background`) forwards **any** method with no allowlist.

---

# PART 1 — Bedrock trial provider

### Task 1: `ensure_owner_aips(tiers=…)` + a `plan` attribute on the reverse row

**Files:** `core/billing/bedrock_aip.py`; Test: `tests/unit/billing/test_bedrock_aip*.py` (find with `ls`)

**Interfaces:**
- Produces: `ensure_owner_aips(owner_id, *, tiers: list[str] | None = None) -> dict[str, str]` — `None` keeps today's 3-tier behavior verbatim (paid path unchanged); `tiers=["sonnet"]` mints only that one.
- Produces: `persist_owner_aips(..., plan: str = "paid")` → `_put_reverse_row(..., plan=plan)` writes `{aip_arn, owner_id, tier, plan}`. **The Lambda reads this row already — `plan` rides along for free in the same GetItem.**

- [ ] **Step 1: Failing tests** — `tiers=["sonnet"]` mints exactly one AIP and persists one reverse row with `plan="trial"`; `tiers=None` mints all three with `plan="paid"` (assert the paid call shape is unchanged); the idempotent short-circuit still re-puts reverse rows and now carries `plan`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (thread `tiers` through the mint loop; thread `plan` through persist). **Step 4: Run** → PASS. Ruff clean.
- [ ] **Step 5: Commit** — `feat(billing): single-tier AIP minting + plan attribute for the meter`

### Task 2: `config.py` — a one-tier Bedrock trial container, with the runaway bounds

**Files:** `core/containers/config.py` (bedrock branch); Test: `tests/unit/containers/test_config_provider_routing.py` + the chaos schema gate

Three things in one branch, all of them plain values in the config we already write. Keep the paid three-tier path byte-identical.

**(a) One-tier shape.** The bedrock branch today hard-fails unless all three AIPs exist. Accept a single-AIP (sonnet) trial owner: one model, no premium/cheap ladder.

**(b) Re-home the trial lockdown** (deleted with the meta branch in Task 0 — do NOT lose it): `cron: {"enabled": False}`, no `agents.defaults.heartbeat`, no channel-account re-injection.

**(c) The runaway bounds.** All schema-verified against the pinned 7.1 `.strict()` schema — a wrong key crash-loops the container, so run the chaos test. Every value below is a bound on how far a run can *run away*, never on what an agent can *do*:
- `agents.defaults.timeoutSeconds: 1800` — **the big one.** The default is **172800 (48 HOURS)** and Isol8 has never set it. The agentic loop is a literal `while(true)` with NO step, tool-call, or token cap (the strict schema rejects `maxSteps`/`maxToolCalls`/`budget` outright), so this wall-clock deadline is the ONLY thing that stops a runaway loop. 30 minutes sits far above any real turn (our Paperclip adapter already runs with a 600s cap in production) while turning a $2,600 runaway into a $27 one.
- `agents.defaults.runRetries.max: 8` — default is **160**, and each retry re-arms the deadline.
- `agents.defaults.subagents.maxConcurrent: 2` — default 8, and this lane does **not** count against the main semaphore.
- `tools.loopDetection.enabled: true` — upstream's own circuit breaker, defaulting to **false**. Free.
- `agents.defaults.contextTokens: 200_000` — currently UNSET on the bedrock path, so a single call may take input up to the model's declared 1M window (~$6.74/call). Also triggers compaction earlier, which is independently desirable.
- Leave `agents.defaults.maxConcurrent` at its default of 4 (already a real global semaphore that every entrance passes through) and leave `cacheRetention: "long"` alone (setting it to `"none"` kills cache *reads* too and makes long loops dramatically MORE expensive).

- [ ] **Step 1: Failing tests** — a trial (one-AIP) owner emits exactly one bedrock model, the lockdown keys, and every bound above with its exact value; a paid (three-AIP) owner is unchanged (heartbeat present, no cron block, ladder intact) EXCEPT that the runaway bounds apply to paid too (they are sane product defaults — assert them on both paths); a one-AIP owner no longer raises.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run unit tests AND `uv run pytest tests/chaos/invariants/test_config_schema.py -v`** → PASS (proves no key can crash-loop a container).
- [ ] **Step 5: Commit** — `feat(billing): one-tier Bedrock trial config + runaway bounds`

### Task 3: Route `trial_local` to the Bedrock trial mode

**Files:** `core/services/container_provision.py` (`resolve_provider_choice`), the provision path in `core/containers/ecs_manager.py` (where `ensure_owner_aips` / `skip_bedrock_grant` are decided — grep both), `routers/container.py:_resolve_provider_choice` (the mirror noted in the docstring); Tests for each

**Interfaces:** `resolve_provider_choice` returns `("bedrock_trial", None)` for `trial_local`. `bedrock_trial` is a **config-path mode**, never a stored `provider_choice` — same contract `meta` had. Provision mints `tiers=["sonnet"]`, `plan="trial"`, and does NOT skip the Bedrock grant (trial owners get a real, single-model-scoped role).

- [ ] **Step 1: Failing tests** — a `trial_local` owner resolves to `bedrock_trial`; provisioning them mints exactly one (sonnet) AIP with `plan="trial"` and grants the scoped role; a paid `bedrock_claude` owner still mints three with `plan="paid"`; **the task role's policy for a trial owner permits ONLY the sonnet profile** (assert on the generated policy document — this is the IAM pin, the security boundary).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (read how `skip_bedrock_grant` is passed today for the meta path and invert it for trial). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(billing): provision trial owners onto a single-model Bedrock profile`

# PART 2 — The counter and the stop move to the AWS layer

### Task 4: Replay-safe invocation dedup (CDK + Lambda)

**Files:** `apps/infra/lib/stacks/database-stack.ts` (new table) or the meter stack — put it wherever the meter's own resources live; `apps/infra/lambda/bedrock-usage-meter/index.py`; Tests beside the Lambda

New DDB table `isol8-{env}-bedrock-invocation-dedup`: PK `request_id`, TTL attribute `ttl` (48h — comfortably past CloudWatch's retry + DLQ-redrive horizon). Before any counter write, the Lambda does a conditional `PutItem(attribute_not_exists(request_id))`; on `ConditionalCheckFailedException` it **skips the counter write but still proceeds to the Metronome ingest** (which has its own 34-day dedup and is the paid path's correctness story — do not regress it).

- [ ] **Step 1: Failing tests** — the same record delivered twice ADDs the counter once; a new requestId ADDs again; a dedup-store failure does not crash the batch (log + skip the counter, still ingest).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (table in CDK + `dynamodb:PutItem` grant; guard in the Lambda). **Step 4: Run** → PASS. **Step 5: Commit** — `feat(infra): per-invocation dedup store for the Bedrock meter`

### Task 5: The Lambda prices locally, counts, and skips Metronome for trials

**Files:** `apps/infra/lambda/bedrock-usage-meter/index.py`, `apps/infra/lib/stacks/bedrock-meter-stack.ts` (IAM); `apps/backend/core/billing/bedrock_pricing.py` (nothing to change — but see the lockstep test); Tests

- Embed a copy of the rate table + `price_from_tier` in the Lambda (it is a pure function; the Lambda has no backend import path and deliberately no VPC). **This makes a THIRD copy of the rates — add a lockstep test** in the style of the existing `test_config_cost_blocks_match_bedrock_pricing_rates`, asserting the Lambda's table equals `bedrock_pricing._RATES`.
- On each record: price it, then (guarded by Task 4's dedup) ADD to `billing-accounts.meta_daily_spend_microcents.{UTC date}` via a raw-boto3 reimplementation of `add_meta_daily_spend`'s guarded two-step (conditional map init, then nested `ADD` with `ReturnValues=ALL_NEW` — DDB rejects doing both in one expression).
- Branch on the reverse row's `plan`: `trial` → count only, **skip the Metronome ingest**; `paid` → count nothing, ingest exactly as today.
- CDK: add `dynamodb:UpdateItem` on `billing-accounts` **and `kms:GenerateDataKey`** to the ViaService statement (today's grant is `Decrypt`-only — enough for `GetItem`, silently insufficient for a write).

- [ ] **Step 1: Failing tests** — a trial record ADDs the priced amount to the right date key and does NOT POST to Metronome; a paid record POSTs to Metronome and does NOT touch the counter; cache tokens are included in the price; the lockstep rate test.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** — `feat(infra): meter Lambda prices trial invocations into the daily counter`

### Task 6: Cap enforcement from the meter — revoke + stop

**Files:** `apps/infra/lambda/bedrock-usage-meter/index.py`, `apps/infra/lib/stacks/bedrock-meter-stack.ts` (IAM); Tests

When the `ALL_NEW` day total returned by the ADD crosses `CAP_MICROCENTS` **and** `plan == "trial"`: (a) delete the owner's scoped inline policy (`iam:DeleteRolePolicy` on `role/isol8-{env}-owner-bedrock-*`) and (b) `ecs:UpdateService` the owner's service to `desiredCount=0`. This is precisely the pairing `revoke_owner_bedrock`'s docstring prescribes ("IAM is ~5-15s eventually consistent — pair with ECS desired-count=0 for the guaranteed instant stop"). Stamp `bedrock_revoked=True` on the container row so the backend can see it. Fire exactly once per crossing (the dedup store makes the ADD idempotent; make the enforcement idempotent too — a missing policy is already-revoked, a stopped service is already-stopped).

- [ ] **Step 1: Failing tests** — crossing the cap revokes + stops exactly once; staying under does neither; a paid owner crossing $3 is untouched; re-delivery of the crossing record does not re-fire; a revoke failure still attempts the stop (and vice versa) — the two must not be coupled.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (+ CDK IAM: `iam:DeleteRolePolicy` scoped to the owner-role name pattern, `ecs:UpdateService` scoped to the cluster). **Step 4: Run** → PASS. **Step 5: Commit** — `feat(infra): revoke + stop a trial owner at the daily cap`

### Task 7: Daily restore

**Files:** `core/services/provision_gate.py` (or wherever the container-status path already reads billing + container rows — do NOT add a new read); Tests

`restore_owner_bedrock` exists but its only caller demands a positive Metronome balance a card-less trial can never have. Add the trial trigger: on the status/provision path, if the container row says `bedrock_revoked` **and** today's counter is under the cap (i.e. the day rolled over) **and** the owner is still `trial_local` → restore the policy and clear the flag. Reuse rows already in hand.

- [ ] **Step 1: Failing tests** — revoked + new day (spend under cap) → restore called, flag cleared; revoked + same day still over cap → no restore; a paid owner's restore path is untouched; restore failure is logged and does not break the status poll.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** — `feat(billing): restore a trial owner's Bedrock access at the daily reset`

# PART 3 — Delete the reserve subsystem and the Meta path

### Task 8: `meta_cap` → `trial_cap` — delete reserve/reconcile/release

**Files:** `core/services/meta_cap.py` → rename to `core/services/trial_cap.py`; `core/gateway/connection_pool.py` (gate_chat Layer 2, `settle` call site, `_settled_run_ids`); `routers/websocket_chat.py` (release calls, side refusal, `suppressCommandInterpretation`); `routers/teams/_guards.py`, `routers/paperclip_runcheck.py`, `routers/container.py` (import rename only); Tests across all

**DELETE:** `_reserves`, `reserve_or_block`, `release`, `reconcile`, `settle`'s counter writes (the Lambda is now the sole writer), the session-key `.lower()` normalization (it existed only to make reserve and reconcile agree), the `is_webchat` source branch, the `_settled_run_ids` dedup (nothing to dedup once settle doesn't write), every `meta_cap.release(...)` call in `websocket_chat.py`, the side-run refusal, and the `suppressCommandInterpretation` param. **With the meter counting every invocation, `/btw` side runs are safe for the free tier — the reason to refuse them is gone.**

**KEEP:** `is_over_daily_cap(owner_id)` (read-only, one DDB read) — it is already what the Teams guards, the run-allowance endpoint, the steer path, and the container-wake check call. `gate_chat` Layer 2 becomes the same read-only check (no reserve). Keep the predicate keyed on `subscription_status == "trial_local"`.

- [ ] **Step 1: Failing/updated tests** — gate_chat blocks an over-cap trial owner with an unchanged 402 payload and places NO reserve; an under-cap owner passes and NOTHING is written to the counter by any backend path; `/btw` from a trial owner dispatches normally; no `suppressCommandInterpretation` is sent. Delete the tests that only existed to cover the reserve subsystem (name them in the report).
- [ ] **Step 2: Implement the deletion.** **Step 3: Run the whole cap + gateway + teams + websocket suites** → PASS. Ruff clean.
- [ ] **Step 4: Commit** — `refactor(billing): delete the pre-send reserve subsystem; the meter is the counter`

### Task 9: Delete the Meta provider path

**Files:** `core/containers/config.py` (meta branch), `core/billing/meta_pricing.py` (fold `CAP_MICROCENTS` + `utc_today` into `trial_cap`, delete the rest), `core/services/key_service.py` + `core/containers/ecs_manager.py` (MODEL_API_KEY injection), `apps/infra` (the `meta-model-api-key` secret + its CDK wiring), `core/services/usage_service.py` (`is_meta_model` branch), plus every `meta` provider-mode reference (grep `"meta"` across `apps/backend`); Tests

Delete the whole Meta path: the provider branch, the **shared fleet `MODEL_API_KEY`** and its secret/CDK wiring (this is also the exfiltration risk — any trial user with `exec: full` could read it from their container env), and `is_meta_model`. The chaos schema test's `provider_choice` strategy must drop `"meta"` and gain the trial-bedrock shape.

- [ ] **Step 1: Grep-driven inventory** — list every `meta` reference in the report before deleting, so the reviewer can check nothing survives.
- [ ] **Step 2: Delete + update tests.** **Step 3: Run full backend suite + chaos schema** → PASS.
- [ ] **Step 4: Commit** — `refactor(billing): remove the Meta provider path and its shared API key`

# PART 4 — Review defects that survive the re-pivot

### Task 9b: LIVE BUG — trial users cannot pay us

**Files:** `core/repositories/billing_repo.py` (new `set_stripe_customer_id`), `core/services/billing_service.py` (`create_customer_for_owner`); Tests

`create_customer_for_owner` short-circuits on ANY existing row (`billing_service.py:71-72`), and a `trial_local` row exists but carries no `stripe_customer_id` (the trial row deliberately writes no `stripe_*` fields). So `create_flat_fee_checkout` raises `No Stripe customer` → **HTTP 400 on every free user who clicks Upgrade.** `POST /billing/trial-checkout` is the only conversion entry point. This is live on prod in merged Phase A code and no test covers it.

- [ ] **Step 1: Failing test** — a `trial_local` owner hitting the checkout path gets a session, not a 400.
- [ ] **Step 2: Implement** — add `billing_repo.set_stripe_customer_id` (conditional update on an existing row; no such setter exists today) and have `create_customer_for_owner` backfill a Stripe customer onto a row that exists but lacks one. Card-less `stripe.Customer.create` already works (`billing_service.py:97-102`) — no new Stripe integration.
- [ ] **Step 3: Run** → PASS. **Commit** — `fix(billing): let trial owners reach checkout (backfill the Stripe customer)`

### Task 10: Fix the `_active_runs` key casing (scale-to-zero is currently dead)

**Files:** `core/gateway/connection_pool.py`; Test: `tests/unit/gateway/`

`mark_run_active` stores under the dispatcher's **mixed-case** `build_chat_session_key`; the chat-terminal branch clears with the event's `sessionKey`, which **OpenClaw lowercases**. The entry never clears → `has_active_runs` is stale-True forever → `_grace_close` permanently skips the trial-stop arm for any owner who has ever chatted. Normalize the key on **both** sides (store and clear), and add a round-trip regression test using a realistic mixed-case Clerk user id.

- [ ] **Step 1: Failing test** — a completed run leaves `_active_runs` EMPTY when the terminal event carries the lowercased key. **Step 2: Run** → FAIL (proves the bug). **Step 3: Fix.** **Step 4: Run** → PASS + the scale-to-zero suite.
- [ ] **Step 5: Commit** — `fix(gateway): normalize the active-run session key so runs actually clear`

### Task 11: Scale-to-zero must see Paperclip runs, and must re-arm

**Files:** `core/gateway/connection_pool.py`, `core/services/node_proxy.py` / `routers/operator_proxy.py` (wherever the operator relay lives — grep `handle_operator_message`); Tests

Two defects, one cause: the relay's runs are invisible to the pool. (a) **Over-kill** — the stop veto reads only webchat `_active_runs`, so a trial owner who posts a Teams comment and closes their tab gets their container killed mid-run. (b) **Never re-arms** — if a run IS in flight at the 30s grace mark, the trial-stop arm is skipped and no later event ever re-arms it, so the container idles until a deploy. Fix: track relay run-state (sniff `lifecycle` start/end on the operator relay's upstream frames — the same events the pool already parses), veto the stop while any relay run is live, and re-arm the grace when the last run ends with no clients attached.

- [ ] **Step 1: Failing tests** — an in-flight relay run vetoes the stop; the stop fires once that run ends and no client is attached; a webchat run in flight at grace-end still eventually stops after it completes.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** — `fix(billing): scale-to-zero sees Paperclip runs and re-arms after one completes`

### Task 12: Conversion — kill the double-provision race and self-heal a stuck upgrade

**Files:** `routers/container.py` (the `retryable_states` auto-retry), `core/services/stripe_webhook_handlers.py`; Tests

(a) **Race:** `reprovision_for_user` sets the row to `stopped` then blocks in a ~90s drain wait; the just-paid owner's ~3s status poll sees `stopped` and dispatches its **own** `background_provision` with no drain wait — two tasks on the EFS WAL SQLite, our documented corruption mode. Fix: mark the row with a `substatus` the auto-retry refuses to act on (e.g. `converting`), cleared when the reprovision completes. (b) **Stuck:** the conversion is one fire-and-forget task; if it dies, the *paying* customer keeps the trial container and stays capped forever, and the webhook can never re-fire. Fix: self-heal on the status path — billing says paid but the container is still the trial shape → re-dispatch the reprovision (idempotent, rate-limited).

- [ ] **Step 1: Failing tests** — the status poll does NOT auto-retry a `converting` row; a stuck conversion (paid billing + trial container) re-dispatches exactly once per poll window; a normal `stopped` trial row still auto-retries.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** — `fix(billing): serialize the conversion reprovision and self-heal a stuck upgrade`

### Task 13: A capped owner cannot wake their own container

**Files:** `core/containers/ecs_manager.py` (`start_user_service`), `routers/container.py` (`POST /provision`); Tests

The cap check lives only on the `GET /container/status` auto-retry branch — `POST /container/provision` happily restarts a stopped container with no check. Move the check **down into `start_user_service`** so it is a single chokepoint no caller can forget, returning the same `meta_daily_cap` / `trial_expired` gate payload.

- [ ] **Step 1: Failing test** — `POST /provision` for an over-cap trial owner is refused (402, the limit card), and `start_user_service` itself refuses; a paid owner and an under-cap trial owner still start normally.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** — `fix(billing): refuse to wake a capped container at the single start chokepoint`

### Task 14: Remove the duplicate chat entrance in the RPC proxy

**Files:** `routers/websocket_chat.py` (`_process_rpc_background` and its dispatch); Tests

The generic RPC proxy forwards **any** method to the container — including `chat.send`, which is a **second door into chat** that skips the gate, the run bookkeeping, and the UX. This is NOT a cost control (the expensive paths never touch this proxy, and the kill switch bounds them anyway); it is a correctness bug: there should be exactly one way to start a chat, and the frontend already uses the typed `agent_chat` message.

Fix by removing the duplication, not by maintaining a list of blessed strings: the proxy refuses run-dispatching methods (`chat.send` and any sibling that starts a run — grep the fork's gateway server-methods for what qualifies) with a structured error pointing at the real path. Everything else the frontend genuinely calls keeps working — enumerate those from the frontend's `useGatewayRpc` call sites so the test is grounded in reality, not guesswork.

- [ ] **Step 1: Failing tests** — `chat.send` through the raw RPC proxy is refused; every method the frontend actually uses still works.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS + the full websocket suite. **Step 5: Commit** — `fix(gateway): chat has one entrance; the RPC proxy no longer dispatches runs`

# PART 5 — Close-out

### Task 15: Full gate + docs

- [ ] `cd apps/backend && uv run pytest tests/unit -q` (full suite) + `uv run pytest tests/chaos/invariants/test_config_schema.py -v` + `uv run ruff check . && uv run ruff format --check .`
- [ ] Lambda tests: run the meter Lambda's own suite.
- [ ] Frontend: `node_modules/.bin/tsc --noEmit` + any touched vitest file.
- [ ] Update `CLAUDE.md` (the free tier is Bedrock-Sonnet, not Meta; the meter Lambda is the counter; the `meta` provider is gone) and the master spec `docs/superpowers/plans/2026-07-10-no-card-free-tier.md` with a re-pivot note.
- [ ] Update `.superpowers/sdd/progress.md` per task as you go — it is the recovery map.
- [ ] **Do not push.** Final whole-branch review + user approval first.

---

## Go-live gates (replaces the Meta list)

- ~~Meta ToS / no-train review~~ — **gone** (Bedrock is the AWS posture we already run for paid users).
- ~~Meta billing card~~ — **gone.**
- ~~`isol8/prod/meta-model-api-key` secret~~ — **gone** (and with it the shared-key exfiltration risk).
- **NEW:** confirm Bedrock model access for the sonnet tier is enabled in the prod account/region (it already is — paid users run it).
- **NEW:** verify the meter Lambda's prod instance picks up trial AIPs (the subscription filter keys on the owner-role name pattern, which trial roles already match — validate with one live trial container before opening signups).
- Dev trials provisioned under the old Meta path must be reprovisioned (they carry a meta container + no AIP). Dev-only; prod has none.

## Self-review

- **Every review defect is accounted for:** #1 (RPC allowlist → T14), #2/#10/#11 (reserve bugs → deleted in T8), #3/#4/#5 (fleet backfill → moot; trial containers are reprovisioned onto the new path), #6/#15 (conversion → T12), #7/#14 (scale-to-zero → T11), #8/#13 (uncounted Paperclip / fail-open settle → moot; the meter counts at the AWS layer), #9 (active-run casing → T10), #12 (per-turn overshoot → bounded by the IAM pin + the blind-window analysis; the revoke+stop is the abort the user asked for).
- **Consistency:** `bedrock_trial` (config mode) / `trial_local` (status) / `plan: "trial"` (reverse row) / `CAP_MICROCENTS` / `meta_daily_spend_microcents` (attribute name kept deliberately) used identically across tasks.
