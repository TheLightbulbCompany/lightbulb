# Observability Audit — Metrics, Alarms, Logs (2026-06-17)

**Why:** The chaos suite (SP2 FIS) asserts against our telemetry — both for stop-conditions (abort on real blast) and recovery signals. A bad alarm config already cascaded into a prod-deploy block. Before re-landing FIS, audit whether what we emit is *trustworthy and valuable*. Three parallel audits (metrics, alarms, logs) over `apps/backend/core/observability/` + emission sites + `apps/infra/lib/stacks/observability-stack.ts`.

**Headline:** The infrastructure is sound (EMF metrics with a cardinality guard; structured JSON logs; a real alarm framework). The problems are **false confidence** (alarms that can never fire), **dev-fragile alarm configs** (breach-on-missing that self-trip), **missing coverage** for the exact failure modes chaos will induce, and a **structural logging gap** (correlation lost in async tasks). Net: today's telemetry is **not yet a safe chaos foundation** — fix this first.

---

## A. Phantom alarms — alarm references a metric that is NEVER emitted (verified by full-repo grep). These give false confidence: they can never fire.

| Alarm | Metric referenced | Status | Impact |
|---|---|---|---|
| **W9** chat-e2e-latency-p99 | `chat.e2e.latency` | never emitted (only in a docstring) | **The platform's headline latency SLO can never fire; both SLO dashboards are blank.** #1 fix. |
| **P8** dynamodb-throttle-sustained (page) | `dynamodb.throttle` | never emitted | DynamoDB throttling — a core failure mode — never pages. |
| **P13** reaper-crash (page) | `gateway.idle_checker.crash` | never emitted (reaper deleted in flat-fee cutover) | never fires. |
| **W12** chat-bedrock-throttle | `chat.bedrock.throttle` | never emitted | custom Bedrock-throttle never alarms (partial AWS-native cover via W46). |
| **W15** channel-webhook-inbound-absent | `channel.webhook.inbound` | never emitted + `treatMissingData=BREACHING` | **permanently in ALARM / flapping.** |
| **W16/W17/W18/W19** stripe meter/sub-latency/api-error/budget | `stripe.meter_event.fail` / `stripe.subscription.latency` / `stripe.api.error` / `billing.budget_check.error` | never emitted | Stripe metering/API errors + budget-check failures never alarm (W18 numerator absent → error-rate permanently 0%). |
| **W25/W26** proxy 5xx / budget | `proxy.upstream` / `proxy.budget_check.fail` | never emitted | proxy faults never alarm. |

## B. Misconfigured alarms — `treatMissingData=BREACHING` that is wrong / dev-fragile / self-tripping under chaos

| Alarm | Issue | Fix |
|---|---|---|
| **P12** reaper-dead | Stale (reaper deleted); is really a backend-liveness heartbeat. **Self-trips any backend-touching chaos scenario** (stopping the backend stops the gauge). | Rename → `backend-liveness` + rewrite description, OR delete as redundant with W48/W28. Never a FIS stop-condition. |
| **W5** gateway-connection-drop | `<= 0` on Average of a gauge + BREACHING → **fires constantly in idle dev**; self-trips gateway/network chaos. | NOT_BREACHING + anomaly band, or remove. |
| **W15** channel-webhook-inbound-absent | BREACHING absence + never-emitted metric → permanent alarm. | Emit the metric or delete; if kept, prod-gate + NOT_BREACHING. |
| **W32** apigw-ws-connect-drop | BREACHING zero in quiet dev; self-trips network-partition chaos. | NOT_BREACHING (or prod-gate). |
| **W45** efs-client-connections | BREACHING zero when no per-user container mounted (common in dev); self-trips EFS chaos. | NOT_BREACHING (or prod-gate). |

(P7 update-worker-stalled is the **one correct** BREACHING heartbeat — emitted unconditionally each 60s loop. Keep.)

## C. Other alarm defects
- **W8** gateway-rpc-error-rate: numerator `gateway.rpc.error` (all RPC) over denominator `chat.message.count` (chat only) → ratio inflated, can exceed 100%. Fix denominator.
- **W39** fargate-task-stopped: EventBridge matches **every** STOPPED task (incl. normal per-user teardown + deploys) → chronic noise; fires by design during container-stop chaos. Filter on stoppedReason/exitCode.
- **W40/W41/W50/W51**: placeholder round-number thresholds ("adjust after baselining") → convert to anomaly bands or baseline.

## D. Dead metrics (emitted, never consumed) + cardinality risk
- Large dead set: `gateway.connection`(event=disconnect/reconnect_throttled — **valuable, just unwired**), `teams.*`, `metronome.*`, `paperclip.*`, `stripe.subscription`, `billing.provider.changed`, seed metrics, `marketplace.path_traversal_attempt` (security — should page), etc.
- **Cardinality risk** (slip past the `_DENIED_DIMENSIONS` guard): `admin_user_id` on `admin_api.*` (unbounded, and dead), `tool_id` on `key_service.secret_delete_orphan` (user-supplied, dead). Add both to the deny list + drop the dims.

## E. Coverage gaps — failure mode → no usable signal (these are exactly what chaos induces)
| Failure mode | Coverage | Gap |
|---|---|---|
| Per-user container CPU / MCP-storm crash-loop + healthcheck SIGKILL | weak | No per-user-container CPU/restart/crash-loop alarm (W36/W37 are backend-only; W39 noisy). #1 missing. |
| EFS SQLite WAL corruption (two-task-on-one-AP) | none | No corruption signal, no >1-task-per-AP detector. |
| Gateway WS drops / reconnect storms | partial | `gateway.connection` emitted but unconsumed; no reconnect-throttle-rate alarm. |
| DynamoDB throttling | none (phantom P8) | neither custom nor AWS-native `ThrottledRequests` alarmed. |
| Paperclip / Aurora outage | none | **Zero RDS/Paperclip alarms in the stack.** Only indirect signal: W53 relay churn. |
| Billing-provider outage | weak | Metronome metrics all dead; no provision-gate-block-rate alarm (a provider outage silently gates users). |
| Config-brick on boot | none | surfaces only indirectly via container.error_state. |

## F. Logging — content is GOOD; the gap is structural
- **The "no useful chat-pipeline logging" memory is STALE/false** — verified. Chat dispatch, gateway connect/park/reopen, container lifecycle, Stripe handlers all log well (the gateway park→reopen path is exemplary for chaos recovery verification).
- **P0 structural gap:** correlation IDs (`request_id`/`user_id`) are bound in HTTP middleware via contextvars but **do NOT propagate into `asyncio.create_task` children** — and the chat pipeline, provisioning, and gateway reader loop all run in those detached tasks. So those JSON fields log as `null` exactly where incidents happen; the data survives only as free-text `%s`, so `filter user_id=…` in Insights misses the chat pipeline. Fix: `copy_context()` or re-bind context at each background-task entry point; thread the existing `clientRunId` as a structured `run_id`.
- **Silent/thin spots:** `webhook_dedup` (no log/metric on dedup decisions or table-error re-raise), `background_provision` (failure = one log line, no metric/alarm), `node_connection.py` (thin), `billing.py:59` (swallowed Clerk lookup).

## G. Validated chaos stop-condition set (cross-confirmed by the alarm audit)
Trustworthy FIS stop-conditions today (rate/count-based, NOT_BREACHING, customer-impact, not self-tripping): **P9 alb-5xx-rate, P10 apigw-ws-5xx-rate, W10 chat-error-rate**, + **P1 container-error-state** for container scenarios. Per scenario, EXCLUDE the metric the scenario induces (P8/W40/W41 for DDB; W36/W37 for backend-CPU stress; W43 for EFS-IO stress) and ALL breach-on-missing alarms (P12/W5/W15/W32/W45). This matches the curated set the FIS hotfix already chose — but note **P8 is also a phantom**, so the DDB scenario needs a real throttle signal before it has either a stop-condition exclusion that matters or a recovery assertion.

## Bottom line
Before chaos re-lands: kill the false confidence (phantom alarms), fix the dev-fragile BREACHING configs, close the coverage gaps for the failure modes chaos induces, and fix the correlation-propagation logging gap. Then the curated stop-condition set (§G) is a sound foundation.
