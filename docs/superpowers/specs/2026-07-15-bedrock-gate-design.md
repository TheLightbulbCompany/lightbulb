# bedrock-gate: synchronous Bedrock metering gateway — design

**Status:** approved-direction (2026-07-15 session with founder); spec for review before implementation plan executes.
**Decided in dialogue:** gateway = ONE new Lambda (Node, response streaming, Function URL); meter Lambda RETAINED as auditor/backstop; bearer-token auth only; covers trial AND paid `bedrock_claude`; Metronome pipeline untouched.

## 1. Problem

Bedrock has **no native per-tenant spend cap** — AWS's own guidance is to build enforcement yourself, either asynchronously (usage signal → revoke) or synchronously (gateway pre-flight). Everything Isol8 runs today is asynchronous:

- **Trial ($3/day)**: the invocation-log meter Lambda counts spend 30–90s behind the calls and kills via IAM revoke + `desiredCount=0`. At full agentic burn (~$0.30/min measured 2026-07-15) a $3.00 threshold realized **$3.39**. PR #1253 mitigates with an enforce-early margin ($2.60), which is correct for async enforcement but is a margin, not a guarantee.
- **Paid (`bedrock_claude` credits)**: enforcement lag is **minutes** — Metronome threshold alert (poll-based, ~3-min tick) → webhook → fast-gate row → revoke. Between webhooks an owner can spend past a zero balance.

An IAM revoke also cannot cancel an already-admitted streaming response, so both paths structurally overshoot. The industry floor (AWS re:Post gatekeeper pattern; AWS's LiteLLM-based Multi-Provider Gateway guidance) is: **pre-flight admission check + per-request exposure bound + synchronous post-stream debit**. Nobody cuts a stream mid-generation.

## 2. Goals

1. **Trial**: no admission when today's spend ≥ cap; cap returns to a clean **$3.00** (both lockstep constants); overshoot bounded to in-flight requests only.
2. **Paid**: no admission when the credit balance ≤ 0 — per-request enforcement instead of minutes-lagged.
3. **Metronome/Stripe billing pipeline untouched**: the meter Lambda remains the sole Metronome writer; invoices, credit burn-down, 1.03 margin identical.
4. **Attribution unchanged**: gateway invokes each owner's OWN application inference profile; the AIP-ARN→owner reverse table keeps working for the meter.
5. **Fence, not UX**: all three direct-invoke routes (OpenClaw provider, ACP/Claude Code harness, `exec: full` shell) closed by removing `bedrock:Invoke*` from owner roles.
6. **Reconciliation by construction**: every invocation is counted exactly once whether the gateway debits it or not (shared requestId dedup; meter = auditor).

### Non-goals

- Cutting off a stream mid-generation (no shipping system does; `maxTokens` bounds the tail).
- Replacing the meter Lambda (it is the AWS-sourced ground truth: aborted/crashed streams are visible only in invocation logs).
- Gating BYOK / ChatGPT-OAuth owners (no Bedrock in their path).
- Multi-provider gateway (one provider, one API family — that's why we don't need LiteLLM's footprint).

## 3. Current state (verified 2026-07-15; file refs)

- Provider block emitted by `apps/backend/core/containers/config.py:536-570`: `models.providers["amazon-bedrock"] = { baseUrl: "https://bedrock-runtime.{region}.amazonaws.com", api: "bedrock-converse-stream", auth: "aws-sdk", models: [per-owner AIP-ARN entries] }`. **`baseUrl` at config.py:539 is the seam.** Hot-swap path `config_patcher.replace_provider_block` reuses the same builder.
- Container authenticates via the AWS SDK default chain → per-owner ECS task role `isol8-{env}-owner-bedrock-{owner}`; inline policy `owner-bedrock` (`core/billing/bedrock_aip.py:90-149`) allows Invoke on that owner's AIPs (+ FM ARNs conditioned on those AIPs), denies `CreateInferenceProfile`.
- OpenClaw fork (`v2026.7.1-beta.3-isol8.1`, checkout `~/Desktop/agent.nosync`): custom endpoints are first-class (`extensions/amazon-bedrock/stream.runtime.ts:1049-1060`, "preserves custom endpoints (VPC/proxy)"); **bearer auth native** — `AWS_BEARER_TOKEN_BEDROCK` → `authSchemePreference: ["httpBearerAuth"]` (`stream.runtime.ts:182-183,225-228`); `AWS_BEDROCK_FORCE_HTTP1=1` available (SDK defaults to HTTP/2). Only ConverseStream is used by the provider. Control-plane trait lookup `GetInferenceProfile` goes DIRECT regardless of endpoint override (`register.sync.runtime.ts:260-267`), fails soft.
- Second/third direct-invoke paths: ACP harness env (`ecs_manager.py:175-200`: `CLAUDE_CODE_USE_BEDROCK=1`, `ANTHROPIC_MODEL=<AIP ARN>`) and the agent shell (`exec: full` → `aws bedrock-runtime` CLI).
- Meter Lambda (`apps/infra/lambda/bedrock-usage-meter/index.py`): attributes via `modelId`(=AIP ARN)→`isol8-{env}-bedrock-aip-owners`; paid → Metronome ingest (`transaction_id`=requestId, 34-day exactly-once); trial → local rate table → conditional-put dedup (`isol8-{env}-bedrock-invocation-dedup`, 15-day TTL) → atomic ADD to `billing-accounts.trial_daily_spend_microcents.{date}` → kill at cap. **Env split**: subscription filter matches `identity.arn` containing `isol8-{env}-owner-bedrock` (`bedrock-meter-stack.ts:416-418`); both filter slots on the log group are consumed.
- Paid gate state: `METRONOME_FAST_GATE_TABLE` row `{owner_id, remaining_microcents, updated_at}` (`core/billing/metronome_fast_gate.py`), refreshed by webhook + lazy `getNetBalance` when >60s stale (`core/services/metronome_gate.py`).
- Network: containers in private subnets, egress-all through 1 NAT; no bedrock VPC endpoint. Lambda Function URLs cannot live in a VPC → gateway endpoint is public, token-gated (same posture as AWS's own gateway samples).
- Interaction with in-flight work: **PR #1253** (limit-card fixes + $2.60 margin) merges independently and first; this project later restores `CAP_MICROCENTS = 3_000_000` in its final phase. PR-2a/2b (paperclip skill/run-id fixes) is owned by another session and does not touch these files.

## 4. Design overview

```
OpenClaw container                         bedrock-gate (Node Lambda, Function URL, streaming)
  provider amazon-bedrock                    per request:
  baseUrl = GATE_URL          ──HTTPS──▶      1 auth: bearer token → token row (owner_id, denied)
  AWS_BEARER_TOKEN_BEDROCK=<owner token>      2 denied? → 403
  AWS_BEDROCK_FORCE_HTTP1=1                   3 AIP-ownership check: path modelId ∈ owner's AIPs
ACP harness (Claude Code)                     4 pre-flight budget (plan-split, DDB):
  ANTHROPIC_BEDROCK_BASE_URL = GATE_URL           trial: today's counter < CAP  else 429
  AWS_BEARER_TOKEN_BEDROCK=<owner token>          paid : fast-gate remaining > 0 else 429
(shell path: dead — owner IAM has no Invoke)   5 clamp inferenceConfig.maxTokens (trial)
                                               6 SigV4-sign with GATEWAY role → bedrock-runtime
                                               7 stream response bytes back VERBATIM
                                                 (tee-decode eventstream for the usage event)
                                               8 debit synchronously, deduped on requestId:
                                                    trial: claim + ADD daily counter
                                                    paid : decrement fast-gate estimate
                                                      │
Bedrock invocation log (unchanged, AIP modelId)◀──────┘ (AWS writes it regardless)
      │
      ▼
meter Lambda (AUDITOR — unchanged pricing/ingest code):
  paid  → Metronome ingest (sole writer, exactly-once)      [unchanged]
  trial → dedup claim: already claimed by gateway → skip;
          NOT claimed → gateway missed it (crash/abort) → ADD (backstop)
  cap crossed anyway → kill = deny flag + desiredCount=0    [IAM revoke retired in final phase]
```

**Key passthrough decision — no re-framing.** The gateway proxies at the **HTTP byte level**: it parses/modifies only the *request* JSON (clamp, validation), SigV4-signs the outbound call itself, then pipes the response body bytes to the client untouched while tee-decoding `application/vnd.amazon.eventstream` frames just enough to extract the final `metadata.usage` event. Byte-identical passthrough guarantees thinking signatures, cache points, `stop_details`, and usage metadata reach OpenClaw exactly as Bedrock emitted them. `x-amzn-requestid` comes from the response headers.

## 5. Components

### 5.1 Gateway Lambda (`apps/infra/lambda/bedrock-gate/`, Node 20/22, `streamifyResponse`)

- **Two instances** (dev/prod), same account-scoped stack pattern as the meter — new `bedrock-gate-stack.ts` (do not grow the meter stack). Per-instance env: token table, billing table, fast-gate table, dedup table, region, `GATE_ENV`.
- **Routes** (path shape mirrors bedrock-runtime so the SDK "just works" with a custom endpoint):
  - `POST /model/{modelId}/converse-stream` (OpenClaw)
  - `POST /model/{modelId}/converse` (non-streaming safety)
  - `POST /model/{modelId}/invoke-with-response-stream`, `POST /model/{modelId}/invoke` (ACP/Claude Code + embeddings if any)
  - anything else → 404.
- **Auth**: `Authorization: Bearer <token>`; SHA-256(token) → GetItem on token table. Missing/unknown → 401. `denied=true` → 403 with Bedrock-shaped error body. No SigV4 verification (structurally impossible for us — SigV4 is symmetric HMAC verifiable only by AWS; bearer is the scheme AWS added for exactly this).
- **Budget pre-flight** (after auth, before invoke): trial → `get_trial_daily_spend(owner, today) < CAP_MICROCENTS`; expiry (`trial_end`) also checked (whole predicate, mirroring `trial_cap.reason_for_account`). Paid → fast-gate `remaining_microcents > 0` (missing row = allow; Metronome webhook/refresh populates it — fail-open here is backstopped by the meter+enforcement path exactly as today).
- **429 contract**: body shaped like a Bedrock `ThrottlingException` with a distinguishable message (`isol8: daily free limit reached` / `isol8: credit balance exhausted`) so OpenClaw's provider surfaces a retryable-looking, human-readable error; the existing UX gates (limit card, ChatErrorCard) remain the primary user-facing surface.
- **maxTokens clamp**: trial requests get `inferenceConfig.maxTokens = min(requested, 16_384)`; paid passthrough. (Exposure math in §7.)
- **Debit**: after the stream's final usage event — price with the same rate table (4th copy, lockstep-tested like the existing 3), then:
  - trial: conditional-put claim `{request_id, source: "gateway"}` into the existing dedup table → on success, atomic ADD to the daily counter (same expression as the meter).
  - paid: `ADD remaining_microcents -<estimate>` on the fast-gate row (estimate = local price × margin factor; Metronome refresh overwrites with truth).
  - Client aborts / missing usage event → debit nothing; the meter auditor counts it from the invocation log within ~90s (dedup claim will be unclaimed).
- **Concurrency guard**: reserved concurrency per instance (e.g. 100) so a runaway cannot exhaust account concurrency; alarm on throttles.

### 5.2 Token service (backend)

- Mint at provision (`ecs_manager.provision_user_container` / token module in `core/billing/`): 32 random bytes, base64url. Store **hash only**: new per-env DDB table `isol8-{env}-bedrock-gate-tokens` `{token_hash (PK), owner_id, denied (BOOL), created_at}` + GSI or duplicate pointer `owner_id → token_hash` on the containers row for rotation/revocation.
- Plaintext injected into the task definition env as `AWS_BEARER_TOKEN_BEDROCK` (+ `AWS_BEDROCK_FORCE_HTTP1=1`); rotated on every reprovision (precedent: `gateway_token`).
- **Deny flag = the new kill switch**: meter Lambda kill and `enforce_on_low_balance` set `denied=true`; restore paths clear it. Compromised token → rotate (reprovision) or deny.

### 5.3 Meter Lambda (auditor — minimal diff)

- Pricing, parsing, Metronome ingest, DLQ, alarms: **unchanged**.
- Trial counting logic literally already reconciles: its conditional-put dedup claim fails when the gateway claimed first → skip; succeeds when the gateway missed → ADD (the backstop). Add a `source` attribute on claims + a `meter.reconciled_gateway_miss` metric so gateway accounting bugs are *visible*.
- Kill path: keep IAM revoke **during transition** (it's harmless once owner roles have no policy), add deny-flag write + keep `desiredCount=0` + status stamp. Final phase: IAM revoke becomes a no-op naturally.
- **Subscription filter**: identity term becomes an OR during transition — `?"isol8-{env}-owner-bedrock" ?"isol8-{env}-bedrock-gate"` — so both direct-signed and gateway-signed records route to the right env instance (gateway role name carries the env).

### 5.4 IAM

- New per-env gateway role: `bedrock:InvokeModel(+WithResponseStream)` on `application-inference-profile/*` in-account + the FM ARN list **conditioned** `ArnLike bedrock:InferenceProfileArn: arn:aws:bedrock:*:{acct}:application-inference-profile/*` (the container-stack boundary at `container-stack.ts:285-338` already expresses this shape); DDB access to the four tables; explicit deny `bedrock:CreateInferenceProfile`.
- Owner roles (final phase): `build_owner_bedrock_policy` drops both Invoke statements for gateway'd owners; keeps `bedrock:GetInferenceProfile` (trait lookup, read-only, fails-soft anyway). Boundary keeps Invoke allowed until every env is cut over, then tightens.

### 5.5 Provision/config changes (backend)

- `config.py` provider block: `baseUrl` becomes the per-env gate URL **when the owner's containers row has `bedrock_via_gateway=true`** (per-owner cutover flag; default false → flip per env cohort). `auth: "aws-sdk"` stays (the bearer env overrides scheme inside the plugin).
- ACP harness env (`ecs_manager.py:175-200`): add `ANTHROPIC_BEDROCK_BASE_URL=<gate URL>` (verify exact var name against the Claude Code release in the image — **open item V1**) alongside the bearer var.
- Restore/kill call sites migrate: `maybe_restore_trial_bedrock`, `metronome_gate._maybe_restore_bedrock`, `revoke_owner_bedrock`/`restore_owner_bedrock` gain deny-flag semantics behind the same function names (IAM half retained during transition).

## 6. Data model changes

| Table | Change |
|---|---|
| `isol8-{env}-bedrock-gate-tokens` | **new**: `{token_hash PK, owner_id, denied, created_at}` |
| `containers` | + `bedrock_via_gateway` (BOOL), + `bedrock_gate_token_hash` (rotation pointer) |
| `bedrock-invocation-dedup` | + `source` attr on claims (`gateway` \| `meter`) — no key change |
| `billing-accounts`, fast-gate | unchanged schemas; new writer (gateway) using existing expressions |

## 7. Enforcement semantics & exposure math (honest)

- Admission is denied **before** any token is generated once the budget line is crossed. Overshoot = cost of requests already in flight at crossing.
- Trial worst case: `maxConcurrent` ≈ 2–3 effective streams (0.5 vCPU box saturates ~4) × (clamped 16k output ≈ $0.26 + typical ~90k cache-read input ≈ $0.03) ≈ **$0.6–0.9 absolute worst, cents typical** — vs. today's lag-window overshoot ($0.39 measured at moderate burn, unbounded in principle). Cap constant returns to `3_000_000` in the final phase (both lockstep copies, same PR).
- Paid: exposure = in-flight requests at balance-zero, then 429s — versus minutes of unbounded burn today. A v2 **reservation** scheme (debit `maxTokens`-worth up front, settle on usage) can shrink overshoot to ~zero later; explicitly out of v1 (the deleted pre-send-reserve incident earns v1 simplicity).
- Latency added: 1 DDB auth read + 1 budget read (~5–10ms) + Lambda hop; cold starts ~100–300ms occasional.

## 8. Failure modes & break-glass

| Failure | Behavior | Recovery |
|---|---|---|
| Gateway Lambda erroring/throttled | Bedrock inference 5xx/429 for cut-over owners (no corruption; chat shows error) | Alarms on errors/throttles; **break-glass**: flip `bedrock_via_gateway=false` cohort-wide + config hot-swap (`replace_provider_block`) + owner IAM still valid during transition phases → traffic reverts to direct in minutes; post-IAM-drop phases, re-grant via `restore_owner_bedrock` (existing code) |
| Gateway crashes mid-stream / client aborts | Tokens Bedrock billed but gateway didn't debit | Meter auditor ADDs from invocation log ≤ ~90s (`reconciled_gateway_miss` metric alarms if frequent) |
| DDB unavailable at pre-flight | Fail-closed for trial (429), fail-open for paid missing-row only (matches today's posture); alarm | — |
| Token leak | Holder can spend that owner's budget through the owner's own caps, then 429 | deny flag / rotate on reprovision |
| Deny flag stuck | Same restore paths as today (rollover / Metronome-positive / admin) | runbook |
| Function URL discovered without token | 401; reserved concurrency bounds abuse cost | WAF/API-GW front is a later option |

## 9. Rollout

1. **Phase 0**: spike — byte-passthrough + tee-parse against real dev Bedrock (de-risks the only novel mechanics).
2. **Phase 1**: deploy gate (both env instances) + token table; no traffic.
3. **Phase 2**: dev canary owner (`bedrock_via_gateway=true` + reprovision) → validate: chat streams, thinking/caching intact, counters reconcile (gateway claims vs meter misses), 429 at cap shows limit card.
4. **Phase 3**: dev fleet → prod trial cohort → prod paid cohort (flag flips + Track-1 config patch/reprovision).
5. **Phase 4**: drop Invoke from owner policies (per cut-over owner), filter OR-term stays, boundary tightened after full fleet.
6. **Phase 5**: `CAP_MICROCENTS` → `3_000_000` (both copies); margin retired. Meter kill demoted to deny-flag+scale-only. Burn-in report: 2 weeks of `reconciled_gateway_miss` ≈ 0 before phase 5.

## 10. Open verification items

- **V1**: exact Claude Code env for Bedrock endpoint override + bearer support in the pinned image (`ANTHROPIC_BEDROCK_BASE_URL`, `AWS_BEARER_TOKEN_BEDROCK` honored?) — verify before Phase 2; if unsupported, ACP falls back to direct-with-IAM until it is (its spend still metered by the auditor as today).
- **V2**: enumerate non-Converse operations in real invocation logs (embeddings via `InvokeModel`?) — gateway must proxy whatever appears (routes exist; confirm coverage).
- **V3**: Function URL + `streamifyResponse` passthrough of `vnd.amazon.eventstream` bytes verified in the spike (content-type + chunking fidelity, FORCE_HTTP1 client).
- **V4**: OpenClaw `GetInferenceProfile` control-plane call — confirm task role keeps this read post-IAM-drop (fails soft regardless).

## 11. Alternatives rejected

- **LiteLLM gateway (AWS guidance)**: ~$380/mo RDS+Redis footprint, budgets in its own store requiring DDB sync, fragile history of budget enforcement on Bedrock passthrough routes (issue closed "not planned"; fixed incidentally later). Overkill for one provider + one API + budgets we already own.
- **API GW + Lambda**: streaming-capable since Nov 2025 but adds 5-min idle ceiling, 2MB/s tail, per-request cost for a public edge in-VPC callers don't need. Revisit only if WAF/custom-domain needs appear.
- **ECS Fargate service + internal ALB**: better at high sustained concurrency (no duration billing) but adds an always-on service + ALB (~$55/mo fixed) and ops surface; founder preference + current scale favor Lambda. Handler stays transport-agnostic so this remains a later migration, not a redesign.
- **OpenAI-compat translation**: loses signed thinking replay for Claude (breaks multi-turn tool-use reasoning). Disqualified.
- **Anthropic-API-compatible gateway** (OpenClaw `anthropic` provider, SSE, ~1:1 payloads): viable fallback if eventstream passthrough spike fails; costs touching every model ref in config generation + `canonicalModelId` mapping. Back pocket.
- **SigV4 inbound auth**: unverifiable by anything except AWS itself (scope mismatch for Function URL/API-GW IAM auth: SDK signs service=`bedrock`). Bearer is the AWS-provided scheme for fronting Bedrock and is strictly narrower than the STS creds containers hold today.

## 12. Cost estimate

Lambda duration ≈ streaming wall-clock: ~$0.003 per 3-min stream at 1GB → single-digit $/mo at current fleet; DDB reads/writes noise; no ALB, no NAT change. Crossover to Fargate ≈ 1–2 continuously-active streams 24/7 fleet-wide — revisit at scale.
