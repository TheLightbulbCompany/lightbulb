# bedrock-gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Read the spec first:** `docs/superpowers/specs/2026-07-15-bedrock-gate-design.md`. Every requirement here traces to it; where they disagree, the spec governs.

**Goal:** Synchronous per-request Bedrock budget enforcement (trial $3/day + paid credit balance) via one new streaming Node Lambda ("bedrock-gate"), with the existing meter Lambda retained as reconciliation auditor and the Metronome billing pipeline untouched.

**Architecture:** Containers keep the `amazon-bedrock` provider but point `baseUrl` at a Function URL and authenticate with per-owner bearer tokens. The gate authenticates, pre-flights the budget from DynamoDB, clamps `maxTokens`, re-signs to Bedrock under a gateway role invoking the owner's own AIP, byte-streams the response back verbatim while tee-decoding the eventstream for usage, and debits synchronously deduped on Bedrock's requestId. The meter Lambda backfills anything the gate misses via the shared dedup table.

**Tech Stack:** Node 20 Lambda (`streamifyResponse`, `@smithy/eventstream-codec`, `@smithy/signature-v4`), CDK TypeScript (new `bedrock-gate-stack.ts`), DynamoDB, existing Python backend (FastAPI) for token mint + config emission.

## Global Constraints

- Trial cap constant stays `2_600_000` microcents until Phase 5 (Task 10); NEVER move only one of the two lockstep copies (`apps/backend/core/services/trial_cap.py`, `apps/infra/lambda/bedrock-usage-meter/index.py`).
- The meter Lambda's Metronome ingest path must not change in any task.
- Gateway must never invoke an AIP that does not belong to the authenticated owner (cross-tenant check is mandatory in every route).
- Response bytes from Bedrock are forwarded VERBATIM — no re-framing, no JSON round-trip of response bodies.
- All new DDB writes use conditional/atomic expressions (this repo's incident history is racy read-modify-write; see `container_repo.try_claim_provision` precedent).
- Per-owner cutover flag `bedrock_via_gateway` gates every behavior change; default absent/false = exactly today's behavior.
- No `sleep`/`tail` in bash; CI is the gate (open PR per phase, Codex + Lint & Test green before merge; merge only on the founder's word).
- Branch per PR off current `origin/main`; never push to main.

## PR structure

- **PR A** (Tasks 1–2): spike artifact + CDK stack (no traffic, no behavior change).
- **PR B** (Tasks 3–6): gateway Lambda implementation + meter auditor diff.
- **PR C** (Tasks 7–8): backend token mint + config/env cutover flag + runbook.
- **PR D** (Tasks 9–10): IAM drop + cap restore (after burn-in sign-off).

---

### Task 1: Spike — verified byte passthrough + usage tee (Phase 0, de-risk)

**Files:**
- Create: `apps/infra/lambda/bedrock-gate/spike/passthrough-spike.mjs` (kept in-repo as reference; not deployed)
- Create: `apps/infra/lambda/bedrock-gate/spike/SPIKE-RESULTS.md`

**Interfaces:**
- Produces: written proof (SPIKE-RESULTS.md) that (a) a locally-run Node HTTP server can SigV4-sign and forward a `ConverseStream` request to real dev Bedrock and pipe the eventstream response bytes to a client unmodified; (b) `@smithy/eventstream-codec`'s `EventStreamCodec.decode` over the teed bytes yields the final `metadata` event with `usage.{inputTokens,outputTokens,cacheReadInputTokens,cacheWriteInputTokens}`; (c) the `x-amzn-requestid` response header is present; (d) an OpenClaw-equivalent client (AWS SDK `BedrockRuntimeClient` with `endpoint` pointed at the spike server + `AWS_BEDROCK_FORCE_HTTP1=1` semantics) parses the piped stream successfully end-to-end including a thinking block and a cachePoint.

- [ ] **Step 1:** Write the spike server: Node `http` server; on request, read JSON body; construct `HttpRequest` for `https://bedrock-runtime.us-east-1.amazonaws.com/model/{AIP-ARN}/converse-stream`; sign with `SignatureV4` (service `bedrock`, creds from `isol8-admin` profile); send via `NodeHttpHandler`; pipe `response.body` to client response AND through a tee that feeds `EventStreamCodec` frame decode, logging each event type and the final metadata usage.
- [ ] **Step 2:** Run against the dev trial AIP for a test owner with a cache-heavy prompt (thinking enabled). Capture: usage from tee vs the invocation-log record for the same requestId (must match), and client-side SDK stream completion.
- [ ] **Step 3:** Repeat for `invoke-with-response-stream` with an Anthropic-Messages body (ACP shape).
- [ ] **Step 4:** Write SPIKE-RESULTS.md: what worked, exact library versions, any content-type/chunking gotchas, verdict for V3 (spec §10). **If the spike fails**, STOP — escalate to the founder with the Anthropic-compatible fallback (spec §11) before proceeding.
- [ ] **Step 5:** Commit: `git add apps/infra/lambda/bedrock-gate/spike && git commit -m "spike(bedrock-gate): verified eventstream byte passthrough + usage tee"`

### Task 2: CDK — bedrock-gate stack (two instances, token tables, no traffic)

**Files:**
- Create: `apps/infra/lib/stacks/bedrock-gate-stack.ts`
- Modify: `apps/infra/lib/app.ts` (instantiate once, prod stage, account-scoped — mirror `BedrockMeterStack` wiring)
- Create: `apps/infra/test/bedrock-gate-stack.test.ts`
- Modify: `apps/infra/lib/stacks/database-stack.ts` — NO (token table lives in the gate stack: it is gate-private state)

**Interfaces:**
- Produces per env (`dev`,`prod`): Node 20 Lambda `isol8-{env}-bedrock-gate` (arm64, 1024MB, timeout 900s, reserved concurrency 100, `invokeMode: RESPONSE_STREAM` Function URL, authType NONE), DDB table `isol8-{env}-bedrock-gate-tokens` `{token_hash S PK}` (+ attrs owner_id, denied, created_at; PAY_PER_REQUEST, RETAIN), gateway IAM role (Invoke on `application-inference-profile/*` + FM ARNs conditioned on in-account AIPs — copy the boundary shape from `container-stack.ts:285-338`; deny `CreateInferenceProfile`; RW on gate-tokens + dedup table + billing-accounts + fast-gate for that env), CfnOutputs `GateUrlDev`/`GateUrlProd`.
- Consumes: nothing from other stacks (table names by convention, same as meter).

- [ ] **Step 1:** Write the stack test first: asserts 2 functions, RESPONSE_STREAM URLs, reserved concurrency, token tables, the conditioned-Invoke policy shape, deny CreateInferenceProfile, alarms on Errors/Throttles wired to the existing per-env SNS topics (reuse the meter stack's topic lookup pattern).
- [ ] **Step 2:** `cd apps/infra && npx jest test/bedrock-gate-stack.test.ts` → FAIL (stack missing).
- [ ] **Step 3:** Implement the stack (copy the two-instance loop pattern from `bedrock-meter-stack.ts:144`), Lambda code from `lambda.Code.fromAsset("apps/infra/lambda/bedrock-gate")` with a placeholder handler for now.
- [ ] **Step 4:** Test → PASS. `npx cdk synth 'prod/*' -q` clean.
- [ ] **Step 5:** Commit: `feat(infra): bedrock-gate stack — streaming gate Lambdas + token tables (no traffic)`.

### Task 3: Gateway — auth, deny, AIP-ownership (TDD)

**Files:**
- Create: `apps/infra/lambda/bedrock-gate/package.json`, `tsconfig.json` (or plain .mjs — match repo norm: the meter is plain-file; use ESM .mjs + jest)
- Create: `apps/infra/lambda/bedrock-gate/auth.mjs`
- Create: `apps/infra/lambda/bedrock-gate/auth.test.mjs`

**Interfaces:**
- Produces: `authenticate(headers, ddb) -> {ownerId, tokenHash} | GateError(401/403)`; `assertOwnAip(modelIdFromPath, containersRow) -> void | GateError(403)` (modelId must appear in the owner's `bedrock_aip_arns` map values — read via containers row cached per invocation).
- Test cases: missing header → 401; unknown token → 401; `denied=true` → 403 with Bedrock-shaped JSON body `{"__type":"AccessDeniedException","message":"isol8: access disabled"}`; valid token → ownerId; AIP not owned → 403; AIP owned → ok.

- [ ] Steps: write tests (aws-sdk-client-mock for DDB) → fail → implement (SHA-256 via `node:crypto`, GetItem on `TOKENS_TABLE`) → pass → commit `feat(bedrock-gate): bearer auth + deny flag + AIP ownership`.

### Task 4: Gateway — pre-flight budget + maxTokens clamp (TDD)

**Files:**
- Create: `apps/infra/lambda/bedrock-gate/preflight.mjs`, `preflight.test.mjs`
- Create: `apps/infra/lambda/bedrock-gate/rates.mjs` (4th lockstep rate-table copy) + `rates.test.mjs` with a lockstep test importing the JSON-exported table from the meter (parse `index.py`'s `_RATES` literal via a small extractor script, exactly like `test_index.py`'s backend-lockstep test does in reverse — if too brittle, duplicate the numbers and lockstep-test against `apps/infra/lambda/bedrock-usage-meter/index.py` by regex, matching the existing precedent)

**Interfaces:**
- Produces: `preflightAndReserve(ownerId, plan, ddb, env) -> {hold} | GateError(429, code)`. Plan = billing row `subscription_status === "trial_local"` → trial else paid. Trial: `trial_end` expiry → 429 `trial_expired`; then ONE conditional UpdateItem that both admits and holds — `ADD reserved_microcents.{today} :estimate` guarded so the request is admitted only if `spent + reserved + :estimate < :cap` (condition over the current item; if a single atomic condition on the summed maps isn't expressible, use a version-attr optimistic loop — NEVER a bare read-modify-write, per repo incident history). Condition-fail → 429 `trial_daily_cap`. Paid: same mechanic on the fast-gate row (`remaining - reserved - :estimate > 0`); missing row = allow. Returns `hold {ownerId, date, estimate}` for settle/release.
- Constants (env vars, each CDK-set from ONE source, documented lockstep): `CAP_MICROCENTS` (lockstep `trial_cap.py`), `RESERVE_ESTIMATE_MICROCENTS` (≈ 300_000 = $0.30 — realistic p95 turn, NOT worst case; sizing rationale in spec §7), `RESERVE_TTL_S` (≈ 1200; holds auto-expire so a crash can't leak one).
- Produces: `clampMaxTokens(requestBodyObj, op, modelMax)` — **ceiling only, NEVER a small cap** (adaptive traffic sends no maxTokens → truncating it clips thinking): absent → leave absent; present ≤ modelMax → passthrough; present > modelMax → set modelMax. `modelMax` = 131_072 (Sonnet 4.6/Opus 4.6) / 65_536 (Haiku 4.5) from catalog/name. Converse key = `inferenceConfig.maxTokens`; Invoke Anthropic body key = `max_tokens`. Non-adaptive guard: if body has `thinking.budget_tokens`, ensure clamped `maxTokens > budget_tokens` else leave absent (Bedrock rejects `budget ≥ max`).
- 429 body: `{"__type":"ThrottlingException","message":"isol8: daily free limit reached — resets at midnight UTC"}` (etc.), plus header `x-isol8-gate-code: trial_daily_cap`.

- [ ] Steps: tests (expiry; admitted when spent+reserved+estimate < cap; denied at the boundary; hold actually written; paid remaining-minus-reserved; clamp absent→absent / ≤max→passthrough / >max→max / budget<max invariant) → fail → implement → pass → commit.

### Task 5: Gateway — proxy handler: sign, stream, tee, debit (the core)

**Files:**
- Create: `apps/infra/lambda/bedrock-gate/proxy.mjs` (sign+forward+tee), `debit.mjs`, `handler.mjs` (streamifyResponse wiring, route parse), tests for each.

**Interfaces:**
- `handler` (streamifyResponse): parse route `POST /model/{modelId}/(converse-stream|converse|invoke|invoke-with-response-stream)`; authenticate → assertOwnAip → clamp → `preflightAndReserve` (hold) → `forward(modelId, op, bodyBuf, responseStream, tee)` → `settle(hold, usage)` (release hold + write actual). Explicit `Deny` any non-AIP `InvokeModel` modelId (blocks a tampering agent routing Titan/embeddings through the gateway).
- `forward`: build signed request to `https://bedrock-runtime.{REGION}.amazonaws.com/model/{encodeURIComponent(modelId)}/{op}` (SignatureV4 service `bedrock`, gateway role creds from the Lambda's own chain), stream response: status+headers mirrored (incl. `content-type`, `x-amzn-requestid`), body piped verbatim to `responseStream`; tee pipes into `UsageTee`.
- `UsageTee` (from spike): eventstream decode → returns `{requestId, usage}|null` on stream end; for `/invoke-with-response-stream`, usage from the trailing `amazon-bedrock-invocationMetrics` in the final chunk; for non-streaming ops, parse the JSON body's `usage` and headers `X-Amzn-Bedrock-{Input,Output}-Token-Count`.
- `settle(hold, ownerId, plan, requestId, usage, ddb)`: price via rates.mjs; trial → conditional PutItem claim `{request_id, source:"gateway", expires_at: now+15d}` on `INVOCATION_DEDUP_TABLE` (`attribute_not_exists(request_id)`), on success ONE UpdateItem: `ADD spent_microcents.{today} :actual` (same map-init guard the meter uses — `index.py:_add_daily_spend`, copy the two-step conditional exactly) **and** `ADD reserved_microcents.{today} :negEstimate` (release the hold); claim exists → still release the hold but skip the spend ADD (meter got it — retries only). Paid → `ADD remaining_microcents :negActual` + release hold on fast-gate; Metronome refresh overwrites `remaining`.
- Hold release is separate from settle so it ALWAYS runs (finally-block): client-abort / missing-usage / upstream-error paths release the hold but write no spend — the hold would otherwise sit until TTL. `spent` is only ever ADDed by a successful dedup claim, so gateway and meter can't double-count.
- Upstream non-2xx: mirror status + body verbatim (containers must see real AccessDenied/Validation errors); release hold, no spend. Errors before forward: Bedrock-shaped JSON per Tasks 3–4. Never throw past the handler: catch-all → 502 `{"__type":"InternalServerException","message":"isol8: gate error"}` + error metric.
- Client-abort: `responseStream` close → abort upstream request; release hold; skip spend (auditor covers via the unclaimed dedup row); emit `gate.client_abort` metric.

- [ ] Steps: tests with a mocked upstream server emitting recorded spike frames (commit a small recorded fixture, scrubbed) covering: happy converse-stream settle (spent ADDed, hold released); abort → hold released, no spend; dedup collision → hold released, no double ADD; paid decrement; upstream 403 passthrough + hold released; unknown route 404 → implement → pass.
- [ ] EMF metrics (mirror meter's `_emit_metric` shape, namespace `Isol8/BedrockGate`, env dim): `gate.allowed`, `gate.denied_budget` (per code), `gate.auth_failed`, `gate.debit_ok`, `gate.debit_skipped_dup`, `gate.client_abort`, `gate.upstream_error`, `gate.error`. Alarms in Task 2's stack (already asserted).
- [ ] Commit: `feat(bedrock-gate): streaming proxy with pre-flight budgets + synchronous debit`.

### Task 6: Meter auditor diff (small, Python)

**Files:**
- Modify: `apps/infra/lambda/bedrock-usage-meter/index.py` (`_claim_request`: add `source="meter"` attr; emit `meter.reconciled_gateway_miss` when a TRIAL claim SUCCEEDS for a record whose identity.arn is the gateway role — that means the gateway missed it; kill path `_kill_owner_bedrock`: add step writing `denied=true` to the gate-tokens row via new `_deny_gate_token(owner_id)` — table name env `GATE_TOKENS_TABLE`, lookup by the containers row's `bedrock_gate_token_hash`; keep all existing steps)
- Modify: `apps/infra/lib/stacks/bedrock-meter-stack.ts` (filter pattern term → OR: `?"isol8-{env}-owner-bedrock" ?"isol8-{env}-bedrock-gate"`; grant meter Lambda RW on gate-tokens; pass `GATE_TOKENS_TABLE` env)
- Modify: `apps/infra/lambda/bedrock-usage-meter/test_index.py`, `apps/infra/test/bedrock-meter-stack.test.ts`

**Interfaces:**
- Consumes: gate-tokens table (Task 2), `source` attr convention (Task 5).
- Produces: reconciliation metric the burn-in report (Task 10) reads; deny-flag kill the gateway honors.

- [ ] Steps: extend moto tests (claim carries source; gateway-identity record + unclaimed → ADD + `reconciled_gateway_miss`; gateway-identity + already-claimed → skip, no metric; kill writes denied=true; token-row missing → kill still completes other steps) → implement → all meter tests pass (`uv run --with moto python -m pytest ../infra/lambda/bedrock-usage-meter/test_index.py -q`) → jest stack tests pass → commit.

### Task 7: Backend — token mint + cutover flag + config/env emission

**Files:**
- Create: `apps/backend/core/billing/bedrock_gate_tokens.py` (+ unit tests `tests/unit/billing/test_bedrock_gate_tokens.py`)
- Modify: `apps/backend/core/containers/ecs_manager.py` (mint/rotate in `provision_user_container` for Bedrock-path owners; inject task env `AWS_BEARER_TOKEN_BEDROCK`, `AWS_BEDROCK_FORCE_HTTP1=1`, and ACP `ANTHROPIC_BEDROCK_BASE_URL` when `bedrock_via_gateway`)
- Modify: `apps/backend/core/containers/config.py` (`build_provider_block`: `base_url = settings.BEDROCK_GATE_URL if via_gateway else f"https://bedrock-runtime.{region}..."` — thread `via_gateway` from the containers row through `build_openclaw_config_dict`)
- Modify: `apps/backend/core/config.py` (+ `BEDROCK_GATE_URL` setting, per env; empty disables)
- Modify: kill/restore call sites — `core/billing/bedrock_aip.py` `revoke_owner_bedrock`/`restore_owner_bedrock` also set/clear `denied` on the token row (keep IAM half); `provision_gate.maybe_restore_trial_bedrock` + `metronome_gate` restores unchanged in signature.
- Tests: extend `tests/unit/containers/test_ecs_manager*.py`, `tests/unit/containers/test_config*.py` (find exact names with `ls`), new gate-token tests.

**Interfaces:**
- Produces: `mint_gate_token(owner_id) -> plaintext` (32B urlsafe; PutItem hash row; write `bedrock_gate_token_hash` on containers row; delete old hash row on rotation); `deny_gate_token(owner_id, denied: bool)`.
- Rule: token minted/rotated on EVERY provision of a Bedrock-path owner regardless of flag (so the flag flip needs only a config patch, not a secret rollout); env vars + baseUrl only when flag true.

- [ ] Steps: TDD per unit; full backend targeted suites; commit. **Verify V1 here** (Claude Code env var support in the pinned image — read the ACP harness docs/binary in `~/Desktop/agent.nosync` or the image; if unsupported, emit ACP env only when supported and record fallback in the runbook).

### Task 8: Cutover tooling + runbook

**Files:**
- Create: `apps/backend/scripts/bedrock_gate_cutover.py` (dry-run default; `--owner|--cohort trial|paid --env dev|prod --apply`: sets `bedrock_via_gateway`, triggers Track-1 config patch + task-def env update via existing update paths; `--rollback` reverses)
- Create: `docs/runbooks/bedrock-gate-break-glass.md` (exact commands: flag off + `replace_provider_block` hot-swap + task-def revert; IAM re-grant via `restore_owner_bedrock` post-Phase-4; validation queries for reconciliation metrics)
- Test: `apps/backend/tests/unit/scripts/test_bedrock_gate_cutover.py` (dry-run plans, apply calls patched services)

- [ ] Steps: TDD script; write runbook; commit.

### Task 9: IAM drop (Phase 4 — separate PR, after burn-in)

**Files:**
- Modify: `apps/backend/core/billing/bedrock_aip.py` (`build_owner_bedrock_policy(owner_id, aip_arns, via_gateway)`: when via_gateway, emit ONLY `bedrock:GetInferenceProfile` on the owner's AIPs — drop both Invoke statements; keep deny CreateInferenceProfile)
- Modify: `apps/infra/lib/stacks/container-stack.ts` boundary — ONLY after the entire fleet is cut over (add TODO + guard test now; execute at Phase 4 end)
- Tests: `tests/unit/billing/test_bedrock_aip*.py` matrix.

- [ ] Steps: TDD; per-owner rollout piggybacks the cutover script's reprovision; commit.

### Task 10: Cap restore + burn-in report (Phase 5)

**Files:**
- Modify: `apps/backend/core/services/trial_cap.py` + `apps/infra/lambda/bedrock-usage-meter/index.py`: `CAP_MICROCENTS = 3_000_000` (both, one commit, comments updated to name the gateway as the reason the margin retired)
- Modify: meter kill comment — IAM revoke step retained but documented as no-op for gateway'd owners.

- [ ] Gate: 2 weeks with `meter.reconciled_gateway_miss ≈ 0`, no `gate.error` alarms, spend counters reconciling (report queries in the runbook). Founder sign-off required before this task runs.
- [ ] Steps: constants + comments; full test suites; commit; PR D.

---

## Self-review notes (author)

- Spec coverage: every §5 component maps to Tasks 2–9; §9 phases map to PR A–D + cutover script; §10 V1→Task 7, V2→Task 5 fixtures + Task 8 validation, V3→Task 1, V4→Task 9.
- The one deliberately-duplicated artifact is the rate table (4th copy) — locked by test like the existing three; consolidation is out of scope.
- Type consistency: `GateError(status, code, bedrockType, message)` used in Tasks 3–5; `mint_gate_token`/`deny_gate_token` names consistent across Tasks 6–7.
