# Observability Remediation Plan (2026-06-17)

**Source:** `docs/superpowers/research/2026-06-17-observability-audit.md`.
**Goal:** Make our telemetry trustworthy + valuable, so the chaos suite (SP2 FIS) can safely re-land on it. Phased so each PR is reviewable and low-risk; the riskier backend-code changes are isolated from the safe infra-config fixes.

---

## Phase 1 — Infra alarm hygiene (THIS PR; `observability-stack.ts` only; verified by `tsc` + `cdk synth`)

Safe, self-contained, no backend code. Kills false confidence and fixes dev-fragile/self-tripping configs.

**1a. Delete dead alarms whose features were removed (cutover) or were never built** — verified the referenced metric is never emitted:
- `P13` reaper-crash (`gateway.idle_checker.crash` — reaper deleted in flat-fee cutover)
- `W12` chat-bedrock-throttle (`chat.bedrock.throttle` — never emitted; AWS-native W46 covers Bedrock throttles)
- `W16` stripe-meter-event-fail (`stripe.meter_event.fail`)
- `W17` stripe-subscription-latency (`stripe.subscription.latency`)
- `W19` billing-budget-check-error (`billing.budget_check.error`)
- `W25` proxy-upstream-5xx (`proxy.upstream`)
- `W26` proxy-budget-check-fail (`proxy.budget_check.fail` — free-tier construct, removed)
- Remove their matching blank dashboard widgets + the Row-3.5 scale-to-zero widgets (`gateway.running.count.by_tier`, `record_activity.count`, `idle.scale_to_zero`).
- **W18** stripe-api-error-rate: numerator `stripe.api.error` never emitted → delete the alarm now; re-add in Phase 2 when the error counter is emitted.

**1b. Fix `treatMissingData=BREACHING` misconfigs (dev-fragile / self-tripping):**
- `W5` gateway-connection-drop → `NOT_BREACHING` (keeps "was non-zero → 0" transition; stops constant idle-dev firing). Note in comment it's not anomaly detection.
- `W15` channel-webhook-inbound-absent → delete (metric never emitted; re-add prod-gated + NOT_BREACHING in Phase 2 if we emit `channel.webhook.inbound`).
- `W32` apigw-ws-connect-drop → `NOT_BREACHING`.
- `W45` efs-client-connections → `NOT_BREACHING`.
- `P12` reaper-dead → rename `backend-liveness`, rewrite description (it's a backend-heartbeat now, not a reaper), keep BREACHING (legit liveness). Add an inline comment: **never use as a FIS stop-condition** (self-trips backend scenarios).

**1c. Other alarm fixes:**
- `W8` gateway-rpc-error-rate → fix denominator (divide by a true RPC total, not `chat.message.count`). If no RPC-total metric exists, switch the threshold to an absolute error count for now + TODO(Phase 2) to emit `gateway.rpc.count`.
- `P8` dynamodb-throttle-sustained → repoint to AWS-native `AWS/DynamoDB ThrottledRequests` (Sum, per-table or account) so it's real without a backend change. (Keeps a genuine DDB-throttle page.)
- `W39` fargate-task-stopped → filter the EventBridge pattern / Lambda on `stoppedReason` ∉ {scaling, deployment, user-initiated} and/or non-zero `exitCode`, so normal per-user teardown + deploys don't fire it.

**1d. Verify:** `cd apps/infra && npx tsc --noEmit` clean; `npx cdk synth dev/isol8-dev-observability` succeeds; no dangling references to deleted alarms; `deploy.yml` unchanged (no stacks added/removed). Confirm prod synth still fine.

> Deliverable of this PR: the audit doc, this plan, and Phase 1 (1a–1c) implemented.

## Phase 2 — Backend telemetry fixes (next PR; `apps/backend`; needs tests)

Higher-risk code changes, isolated from Phase 1.
- **Emit `chat.e2e.latency`** — wrap the chat request→`done` path with `timing("chat.e2e.latency")` (site: `websocket_chat.py` dispatch / `connection_pool.py` chat path). Re-add `W9` SLO alarm + dashboards. **#1 value item.**
- **Correlation propagation (P0 logging)** — capture `contextvars.copy_context()` (or re-bind `request_id`/`user_id`) at each background-task entry point (`_process_agent_chat_background`, `_process_rpc_background`, `background_provision`, gateway `_reader_loop` on connect); thread `clientRunId` as a structured `run_id`. Makes Insights `filter user_id=…` work for the chat pipeline.
- **`webhook_dedup`** — emit `webhook.dedup{source,outcome}` + log/alarm the table-error re-raise.
- **`background_provision`** — emit `container.provision{status:error,phase:dispatch}` on failure.
- **Cardinality** — drop `admin_user_id` (admin_api.*) and `tool_id` (key_service.*) dims; add both to `_DENIED_DIMENSIONS`.
- **Wire valuable dead metrics**: alarm on `gateway.connection{event:reconnect_throttled}` rate; Metronome outage (`metronome.gate.refresh_error`, `metronome.webhook` bad-sig/unknown-customer); promote `marketplace.path_traversal_attempt` to a page alarm.
- Re-add `W18` (stripe-api-error-rate) once `stripe.api.error` is emitted.

## Phase 3 — Coverage for chaos failure modes + FIS re-land (after Phase 2)

- **New alarms** for the failure modes chaos induces (currently uncovered): per-user-container CPU + crash-loop (repeated STOPPED+RESTART in window), EFS >1-task-per-AP / SQLite-corruption signal, provision-gate-block-rate, Aurora (RDS) + Paperclip-Fargate target-health/CPU/connections.
- **Re-land SP2 FIS** (re-instantiate `FisStack` in the dev stage) using the validated curated stop-condition set: **P9, P10, W10** universal + **P1** for container scenarios, with per-scenario exclusion of the induced metric and all breach-on-missing alarms (audit §G). Resume the parked Phase B scenario harnesses, now with real recovery signals (chat.e2e.latency, dynamodb.throttle/ThrottledRequests, per-container crash-loop) to assert against.

---

## Verification of this plan (self-review)
- **Spec coverage:** every audit finding maps to a phase — phantom alarms (§A)→1a/2; BREACHING (§B)→1b; other defects (§C)→1c; dead metrics+cardinality (§D)→2; coverage gaps (§E)→3; logging (§F)→2; stop-condition set (§G)→3. ✓
- **Risk isolation:** Phase 1 touches only `observability-stack.ts` (no behavior change beyond alarm config; deleting never-firing alarms is inert). Backend code changes (metric emission, logging) are quarantined to Phase 2 with tests. FIS only re-lands in Phase 3 once signals are real. ✓
- **Ambiguity check:** "fix the phantom" is resolved per-alarm — delete (feature gone) vs repoint-to-AWS-native (P8) vs emit-in-Phase-2 (chat.e2e.latency, W18). ✓
- **Sequencing:** Phase 1 is independently shippable and unblocks nothing dangerous; Phase 3 (FIS) explicitly depends on Phases 1–2. ✓
