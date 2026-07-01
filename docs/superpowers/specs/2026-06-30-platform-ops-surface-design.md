# Platform-Ops Surface — Design Spec

**Date:** 2026-06-30
**Status:** Draft for review (v3 — 5-lens adversarial verification folded in; §13 decisions resolved)
**Author:** brainstormed with the founder (prasiddha@gmail.com)

## 1. Goal

Turn `admin.isol8.co` into a real **operator console** for running Isol8 itself — cost, logs, live ECS/fleet health, deploys/stack status, metrics/alarms — backed by a **single server-side ops layer**, and make that same ops data available to the owner's agent (**Friday**) so it can monitor and debug the platform the way a human operator does. Owner-only; never exposed to tenants.

## 2. Why this shape (and what we rejected)

The founder wants Friday to operate/debug the platform's AWS infrastructure. The tempting path — point the read-only AWS integration (the one we just shipped for tenants) at Isol8's own account `877352799272` — was **rejected**: that account is the shared, multi-tenant platform account (every tenant's EFS/DynamoDB data + **all secrets** in Secrets Manager), so `ReadOnlyAccess` there = read every customer's data and keys. An "owner carve-out" (relaxing the confused-deputy guard for the owner) was designed and then **dropped** because it deliberately weakens the highest-stakes guard on the crown-jewels account.

Instead: the ops data is read **server-side** (the backend service task role's IAM), passed through a **secret redactor**, and exposed through a controlled ops layer. No agent ever assumes a role in our account. This is strictly safer and reuses IAM the backend already holds.

## 3. Architecture — one ops layer, two consumers

```
                         ┌─────────────────────────────────────────┐
                         │  Backend ops layer  (core/services/ops/) │
                         │  reads AWS via the backend task role →    │
                         │  redact at the response boundary →        │
                         │  normalized DTOs w/ freshness+source_state│
                         │  Shared cache/refresher where required.   │
                         └───────────────┬───────────────┬──────────┘
                                         │               │
                     Clerk-admin auth ───┤               ├─── platform_ops token
                                         ▼               ▼
                    ┌──────────────────────────┐   ┌──────────────────────────────┐
                    │ Consumer 1: Admin UI      │   │ Consumer 2: ops API + SKILL   │
                    │ admin.isol8.co (React)    │   │ owner-only; Friday calls it;  │
                    │ full detail (redacted     │   │ status/health DTOs (raw ARNs/ │
                    │ secrets)                   │   │ Outputs withheld); SKILL.md   │
                    └──────────────────────────┘   └──────────────────────────────┘
```

- **Backend ops layer** — new `core/services/ops/*` (one module per domain: `ecs_ops.py`, `deploys.py`, `logs_ops.py`, `cost.py`, `metrics.py`) + `routers/ops.py`. Reads AWS with the backend task role. Redaction happens at the **AWS-response boundary** (§7) before anything can log or return it. Returns normalized DTOs — the single source of ops truth; the UI and the agent API are thin views over it.
- **Consumer 1 — human admin UI** (`admin.isol8.co`, Clerk-admin auth): the operator-console dashboards (§5, §6), full detail (with secrets redacted).
- **Consumer 2 — the owner's agent** (Friday): the same ops layer exposed on `routers/ops.py`, authenticated by a **`platform_ops` token** (§3.1). Friday's usage is a hand-placed `SKILL.md` on the owner's container EFS (§9). The agent DTO is deliberately **thinner** than the admin DTO — status/health only; raw resource ARNs, CFN Outputs/Parameters, and account-structure detail are withheld from Consumer 2. **The skill lives only on the owner's container EFS — NOT committed to git, NOT baked into the image, NOT distributed.** No CLI binary for now.

### 3.1 Ops-API auth (rewritten — the reused model was insecure)

**Do NOT reuse the `paperclip_service` token kind.** `service_token.verify()` today accepts any token with `kind=='paperclip_service'` + non-empty `sub`; every tenant already holds one (embedded in their agents' `adapterConfig`), so that model would let any tenant call `/ops/*`. `routers/ops.py` accepts **either** of two paths, checked explicitly with **default-deny** (no missing-credential fall-through):

1. **Human/UI path** — `require_platform_admin` (Clerk JWT `@isol8.co` or the `_PLATFORM_ADMIN_EXTRA_EMAILS` allowlist). Governs the admin UI only. **The email allowlist is NOT reused for owner-id scoping.**
2. **Agent path** — a **`platform_ops`** token (a NEW distinct `kind`), signed with a **DEDICATED signing key** (not `PAPERCLIP_SERVICE_TOKEN_KEY`, so ops tokens are independently revocable). The router enforces `kind == 'platform_ops'` **AND** `sub == settings.PLATFORM_OPS_OWNER_ID` (constant-time compare). `PLATFORM_OPS_OWNER_ID` is a **new per-env config primitive** = the founder's `owner_id`.

**Token delivery (decided):** the `platform_ops` token reaches the founder's container as a **Secrets-Manager-backed env var** (`$PLATFORM_OPS_TOKEN`) injected at the task-definition level — exactly how other container secrets (Attio/Stripe) already arrive — **never a plaintext EFS file.** The SKILL.md reads it from the env var. This is the right mechanism *because* Friday consumes the ops layer via **outbound HTTPS** (the skill `curl`s the API); an outbound request's identity is the token in its header, not the gateway WS connection. Every ops call emits an audit metric and is rate-limited. Accepted residual: co-resident code in the founder's own container (Atlas, deployed marketplace agents) can read the env var and reach whatever Friday can — blast radius is bounded to ops-read for the founder's own platform, and the sensitive surface (cross-tenant logs) is separately gated in §7. **Future hardening (not v1):** gate the agent path on the container's existing authenticated gateway connection identity, eliminating the at-rest credential — deferred because it requires an agent→backend RPC path that does not exist today.

**Token lifecycle:** the `platform_ops` token is **longer-lived with `jti`-based revocation** (matches the env-var delivery; manual rotation = rotate the secret + redeploy, documented) via the dedicated key. Its issuance is a **P1 deliverable** — an admin-only mint/revoke path (NOT self-renewal by a bare token) with a typed **401-on-expiry** the skill can detect and surface.

### 3.2 DTO response envelope (mandatory — blocks silent false-negatives)

Every ops DTO returns:
```
{ data, freshness_ts, source_state: "ok"|"degraded"|"stale"|"unavailable"|"partial", errors: [{domain, reason}] }
```
The attention strip and fleet grid aggregate 4+ independent AWS reads; the reused `cloudwatch_logs.recent_errors_fleet` swallows errors to empty. **The swallow-to-empty pattern is forbidden for operator surfaces.** A failed/throttled/timed-out read MUST surface as `source_state != "ok"` with the failing domain in `errors[]` — never as empty `data` that renders "all clear." Each source read has a per-call timeout + bounded retry/backoff.

## 4. Design direction (interface-design)

An **operator console for a live agent fleet**, not a generic SaaS dashboard.

- **Domain:** fleet (per-user containers), tenants, agents (Friday/Atlas), provisioning lifecycle, deploys/rollouts, burn-rate, incidents, logs/traces.
- **Color world:** the existing isol8 **cream/forest** brand in an *operator register* — cream canvas, forest structure, and status that means something: **forest = healthy, amber = warn, restrained clay-red = incident/error, cool slate = in-progress/deploying, muted grey = unknown/unable-to-read** (the `source_state != ok` state — distinct from healthy). Warm and calm when healthy; loud only for incidents.
- **Signature:** the **live fleet** as the platform's heartbeat — an at-a-glance grid of tenant containers (status + health) sourced from live ECS (§5.3), paired with a top-of-landing **"needs attention now"** strip. Quiet by default; surfaces trouble instantly. Every tile has an explicit unknown state so a degraded source never masquerades as green.
- **Rejected defaults:** hero-metric-boxes-in-a-row → attention strip + live fleet; DDB-derived static counts → live ECS health; per-page hand-rolled chips → one semantic `StatusChip`/`Tile`/`Card` system.
- **Depth/typography/spacing:** locked by a shared design system extracted in Phase 1 (§5). Implementation MUST use the `interface-design` skill (dashboards) and `frontend-design` skill (component craft), and extract/commit `.interface-design/system.md` so all five new views stay consistent.

## 5. Phases

Each phase = human dashboards **and** the ops API/DTOs over the same data (Consumer 2 comes along per domain). Each phase ends with an explicit **Done-when**.

### Phase 1 — Foundation + runtime health + deploys
**IAM:** a small, **free, read-only** grant (§8 P1): the four ECS Describe/List actions are **already held**; this phase ADDS `cloudformation:DescribeStackEvents` (for deploy root-cause; `DescribeStacks` — already held — enumerates stacks, so `ListStacks` is not needed) and `cloudwatch:DescribeAlarms` (so the attention strip is whole at launch). Not "zero new IAM" — a minimal read grant that ships through the normal deploy (+ `deploy.yml` drift-check regen, §8).
1. **Shared UI kit + design system** — extract `.interface-design/system.md`; build `StatusChip`, `Tile`, `Card`, `MetricStat` on the cream/forest operator direction (including the muted-grey unknown state); consolidate the two divergent admin fetch layers (`adminFetch` vs `_shared`) onto one client. Confirm no stray `admin_catalog` imports/routes remain (the module is already gone; nothing to delete).
2. **`/admin` dashboard landing** — replace the `→ /admin/users` redirect with the operator overview: the **"needs attention now"** strip + the **live fleet** grid + summary tiles. Strip inputs at P1 = failed deploys (CFN), unhealthy containers (live ECS), backend-app error spikes (backend log group — the tenant-container-log grant is P2), and alarms in ALARM (`DescribeAlarms`, now P1). Any strip tile whose source read fails renders the **unknown** state, never green.
3. **Live ECS health** — replace `system_health._fleet_counts` (DDB-derived, drifts) with live ECS. **Enumeration source:** the DDB `containers`/`service_name` list is the index (fleet membership); enrich via `ecs:DescribeServices` in **≤10-service batches** (desired/running/pending, `deployments[].rolloutState`) — the health classification is `DescribeServices`-based, and a service present in the index but absent from `DescribeServices` classifies as **unknown** (never masked as a real state). The `DescribeTasks` stopped-task diagnostics (`stopCode`/`stoppedReason` capture per §6) are a **deferred follow-up** — not P1. Reads are served from a **short-TTL (~15–30s) shared cache** — the generic **`ops-cache` DDB table** (§9); **P1 is read-through with a short TTL + a read-side TTL check** (an expired cached item is ignored, since DynamoDB TTL deletion is lazy), so it never serves stale data behind a fresh timestamp. The §5.3-ideal **decoupled poll-and-cache refresher** (off the render path, to avoid sharing the account ECS throttle with provisioning) is the follow-up when the fleet grows.
4. **Deploy/stack status view** (`/admin/deploys`) — `DescribeStacks` (status, `StackStatusReason`, `LastUpdatedTime`) + `DescribeStackEvents` for root cause. The event walk is **scoped to the current/last operation window** (page newest-first, bound to the most recent operation boundary / `LastUpdatedTime`, cap page count, handle `NextToken`) and returns the earliest `*_FAILED` `ResourceStatusReason` **within that window** — never a months-old failure surfaced as the current cause. Drift = a later, on-demand increment (§8 defers its IAM).
5. **Coherence fixes** — surface the orphaned per-user Logs viewer in the tab bar (or fold its filters into Activity); add a **platform-wide audit view** (the `admin_user_id` filter already exists on `GET /admin/actions`); single-source the region for the frontend via a **`NEXT_PUBLIC_AWS_REGION`** env (or surface region on the container DTO the page already consumes) — the frontend cannot import backend `settings.AWS_REGION`. Fix **both** hardcoded occurrences: `container/page.tsx:88` and `ContainerActionsPanel.tsx:289`.
6. **Ops API (P1 slice) + token issuance:** `GET /ops/fleet`, `/ops/deploys` returning the §3.2 envelope under **both** auth paths; the `platform_ops` token mint/revoke path (§3.1).

**Done when:** `/admin` renders the attention strip (unhealthy containers + failed deploys + alarms-in-ALARM, degraded sources shown as a distinct unknown state, never green) + live-ECS fleet from `DescribeServices`; `/ops/fleet` + `/ops/deploys` + `/ops/alarms` return the DTO envelope under admin **and** `platform_ops` token, with a tenant `paperclip_service` token **rejected**; the deploy view shows current-operation root cause (incl. auto-rollback stacks); `.interface-design/system.md` committed; region single-sourced (both sites); backend mocked-boto3 + targeted vitest green; new IAM deployed + `deploy.yml` regen committed.

### Phase 2 — Debugging & logs
1. **Widen the CloudWatch Logs read grant** — **no new actions** (`logs:FilterLogEvents`/`StartQuery`/`GetQueryResults`/`GetLogEvents`/`DescribeLogStreams` are already granted for the backend group); **add `arn:...:log-group:/isol8/{env}/openclaw:*` to the existing statement's resources.** All per-user OpenClaw containers log to that **single shared group** (`container-stack.ts:427-428`) — there are no per-user log groups; "per-container" views filter by log **stream** (task id), not by group. (Optionally add `/isol8/{env}/ecs-task-events` as a crash-diagnostics source.)
2. **Secret redactor** (§7) applied to all log content the ops layer returns — admin UI **and** agent.
3. **Log-debug surface** — a fleet-wide + per-container (per-stream) log view (level/window filters + full-text search via `FilterLogEvents`/Logs Insights `StartQuery`), consumed by the UI and by Friday via `GET /ops/logs?…`, gated per §7's cross-tenant residual.

**Done when:** the log view reads the shared container group filtered by stream; the redactor is applied on every log response with false-negative tests (§11) green; the widened grant is deployed + dev-smoked; Friday's log reads honor the §7 gate.

### Phase 3 — Cost & metrics
1. **New IAM** (§8): Cost Explorer read + CloudWatch metrics read (`DescribeAlarms` already came in P1).
2. **Cost dashboard** (`/admin/cost`) — `ce:GetCostAndUsage` grouped by `SERVICE` + `TAG$Environment`, `UnblendedCost`, DAILY/MONTHLY; `ce:GetCostForecast`. Cost is read by a **single scheduled refresher** (one worker, like `update_service.run_scheduled_worker`) writing normalized snapshots to the shared **`ops-cache` DDB table** (§9) that all backend tasks + Friday read — NOT an in-process per-task cache (the backend is multi-task autoscaled; per-task read-through would multiply the **$0.01/request** CE cost by task count and serve inconsistent numbers). **Cache key** = `cost#{env}#{granularity}#{day-rounded window}`. A **hard daily CE-request budget + circuit-breaker** on the ops layer and a **per-token rate limit** on `/ops/cost` prevent a UI refresh loop or a looping Friday from burning spend. UI shows a persistent **"Untagged/Shared"** bucket (NAT/Route53/CloudFront/tax) and an activation-date note.
3. **Metrics/alarms** (`/admin/metrics`) — `cloudwatch:GetMetricData` (ECS `AWS/ECS` service-level CPU/Mem free; ALB `AWS/ApplicationELB` request/latency/5xx/host-count) + `DescribeAlarmHistory` for history. Fan-out is bounded to `GetMetricData`'s ≤500-query cap (paginate for fleet > 500).
4. **Ops API (P3 slice):** `GET /ops/cost`, `/ops/metrics` from the shared store.

**Done when:** the cost view reads shared-store snapshots (never live per-request), with the CE budget/circuit-breaker + `/ops/cost` rate-limit enforced and the Untagged bucket rendered; metrics/alarms render from `GetMetricData`/`DescribeAlarmHistory`; the `Environment` cost-allocation tag is active and CDK-verified (§9); IAM deployed + drift regen committed.

## 6. AWS data sources (verified against AWS docs — capabilities + gotchas)

> Load-bearing gotchas inline so the plan can't miss them. Corrections from the fact-check pass are marked **[fixed]**.

### ECS live health — `ecs:Describe*` *(free, real-time; the 4 actions already permitted)*
- `DescribeServices` (**≤10 services/call**): `desiredCount`/`runningCount`/`pendingCount` (use `runningCount` as the free live-task count — **not** a CloudWatch metric, see Metrics), `deployments[].rolloutState` = `IN_PROGRESS|COMPLETED|FAILED` (+ `rolloutStateReason`, `failedTasks`), `events[]`. Healthy = single `PRIMARY` deploy, `COMPLETED`, running==desired.
- `DescribeTasks` (**≤100 tasks/call**): `lastStatus`/`healthStatus`, and for stopped tasks `stopCode` + `stoppedReason` + container `exitCode`. **Gotcha:** stopped tasks are only returned for **~1h** after stopping. → **Capture mechanism (designed, not just noted):** a lightweight background poller (or a subscription on the `/isol8/{env}/ecs-task-events` log group) persists `stopCode`/`stoppedReason`/`exitCode` to DDB on STOPPED transition; the ops layer reads that persisted history, so a container that crash-looped > 1h before anyone looks still shows its cause.
- Enumerate via the DDB fleet index (§5.3), not `ListServices` on the hot path.

### Deploys — `cloudformation:*` *(free, real-time; `DescribeStacks` already permitted, `DescribeStackEvents` added in P1)*
- `DescribeStacks`: `StackStatus` (+ `StackStatusReason`, `LastUpdatedTime`, `DriftInformation`). **Trap: `ROLLBACK_COMPLETE`** = create failed + rolled back (stack can only be deleted).
- `DescribeStackEvents` (reverse-chrono, full history): **scope the walk to the current operation window** (§5.4) — the earliest `*_FAILED` *within that window* is the root cause; the raw earliest event could be a months-old prior failure.
- Drift is **async** (`DetectStackDrift` → poll → `DescribeStackResourceDrifts`); nested stacks NOT covered. → on-demand later increment; IAM deferred (§8).
- **Structured-field caution (§7):** CFN `Outputs`/`Parameters`/`StackStatusReason` can carry ARNs/topology/plaintext values — in redactor scope + deny-list; withheld from the agent DTO.

### Logs — CloudWatch Logs *(P2)*
- **Single shared group** `/isol8/{env}/openclaw` for ALL containers; filter by **stream** (task id) for per-container. `FilterLogEvents` / Logs Insights `StartQuery`+`GetQueryResults`. **[fixed]** There is **no** "Insights sees last 3h" limit — a query range can span the group's full retention. Real bounds: 60-min query timeout, ≤10k default / ≤100k paginated results, 7-day result retention.
- **Agent-path scoping (§7):** the admin UI reads the group fleet-wide; the **agent path constrains `logStreamNamePrefix` to the founder's own container task-id streams** (resolved from the founder's `owner_id` → service/task IDs via the fleet index) so Friday debugs its own agents without cross-tenant exposure.

### Cost — Cost Explorer `ce:*` *(P3; NEW grant; the outlier — account-level, not env-local)*
- Client pinned to `us-east-1`. `GetCostAndUsage`: `Granularity` (DAILY/MONTHLY), `TimePeriod` (End exclusive; 13-month reach), `Metrics=[UnblendedCost]`, `GroupBy` **max 2** = `DIMENSION:SERVICE` + `TAG:Environment`. `GetCostForecast` (`DataUnavailableException` on thin history). `NextPageToken` pagination.
- **$0.01 per paginated request; data lags ~24h** (+ `Estimated:true` on unfinalized periods). → single scheduled refresher + shared store + budget/circuit-breaker (§5.3 P3).
- **Account-level:** one call returns dev+prod together (split by the `Environment` tag GroupBy); a large **untagged/shared remainder** (NAT, Route53, CloudFront, tax) is expected — render it as its own bucket, don't present a clean dev/prod split.

### Metrics — CloudWatch `GetMetricData` *(P3; NEW grant)*
- **≤500 queries / ≤100,800 datapoints per call.** Retention→resolution tiers (1-min→15d, 5-min→63d, 1-hr→455d).
- **ECS Fargate WITHOUT Container Insights (free):** `AWS/ECS` service-level `CPUUtilization`/`MemoryUtilization`, dims `ClusterName[+ServiceName]`, 1-min. **Gotcha: only published when tasks are RUNNING** — null ≠ 0%, can mean "service down"; handle null as a distinct state. **[fixed] `LiveTaskCount` is a Container-Insights (PAID) metric — do NOT use it in the free v1 list;** source live task count from `DescribeServices.runningCount` (already permitted).
- **Per-task/network/storage metrics need Container Insights (PAID, pre-enable, no backfill)** — explicit opt-in cost line; not v1.
- **ALB (`AWS/ApplicationELB`, free):** **[fixed] split the gotcha** — `RequestCount`(Sum)/`TargetResponseTime`/`HTTPCode_Target_5XX`/`HTTPCode_ELB_5XX` are **only reported when traffic flows** (null = distinct idle state), but `Healthy/UnHealthyHostCount` are emitted **continuously from health checks regardless of client traffic** — their **absence means "no registered targets"** (a real problem), not idleness.
- `DescribeAlarms` (`StateValue`; ≤100/page; include `AlarmTypes` for composite; IAM `*`) — **moved to P1**. `DescribeAlarmHistory` (**2-week** retention) — P3.

## 7. Security model

### Secret redactor — `core/security/secret_patterns.py` (redaction work, NOT a file move)
`marketplace_safety._PATTERNS` is a **byte-level detect-and-FLAG** scanner (returns `SafetyFlags`, never redacts) mixing credential + command-injection + PII patterns. The new module exposes **two APIs** over a shared **credential/secret subset only** (command-injection and email/phone PII are excluded from redaction — they over-scrub log lines):
- **bytes → flags** (marketplace, existing behavior).
- **str → redacted str** (ops logs, new).

The redactor MUST ADD the log-shaped patterns the current library lacks: `Authorization`/`Bearer` (unquoted), `api-key`/`x-api-key` headers, `sk_`/`sk-`/`rk_live` underscore forms, 40-char AWS secret access keys, URL/DSN-embedded creds, STS session tokens, and a high-entropy catch-all. **False-negative tests per shape** (including a deliberately-not-in-patterns secret to document residual risk).

### Where redaction runs (boundary, not just response)
1. **At the AWS-response boundary, BEFORE anything can log it** — a botocore DEBUG line or a traceback would otherwise write the raw secret into `/ecs/isol8-{env}` (the very group ops reads → circular re-exposure). **Forbid botocore/boto3 DEBUG logging in prod**; add the backend's **own** log group to redactor scope; test that raw AWS payloads never reach the logger.
2. **Structured (non-log) fields** — CFN `Outputs`/`Parameters`/`StackStatusReason` and ECS env/task-def — are in redactor scope + an explicit **deny-list**. The **agent DTO (Consumer 2) withholds raw ARNs/Outputs entirely** (status/health only); the Clerk-admin UI may see full (redacted) detail.

### Access boundaries
- **Owner-only agent access** — enforced by `kind=='platform_ops'` + `sub==PLATFORM_OPS_OWNER_ID` + dedicated key (§3.1). A tenant `paperclip_service` token is rejected (regression-tested).
- **Intra-container residual** — any code in the founder's container (Atlas, third-party marketplace agents) can reach whatever Friday can. Bounded to ops-read for the founder's own platform; mitigated by connection-identity gating (preferred), secret-env delivery (not EFS file), per-call audit + rate-limit.
- **Cross-tenant log residual (decided).** The backend task role can read the fleet-wide container group after P2, but the **agent path is scoped by `logStreamNamePrefix` to the founder's own container task-id streams + the platform's own service logs (`/ecs/isol8-{env}`)** — so Friday debugs its own agents (Friday/Atlas/deployed marketplace agents) as the founder requested, without reaching any *other* tenant's chat/tool/file logs. A prompt-injected Friday therefore cannot exfiltrate cross-tenant data through logs. The **admin UI (human, Clerk-admin) reads fleet-wide** — human-in-the-loop, not prompt-injectable. Reading a *specific tenant's* logs via the agent is a future capability behind an egress control (not v1).
- **No new prod-account trust relaxation** — nothing here touches the confused-deputy guards; all reads use the backend's existing server-side IAM.

### Audit & self-observability
- **Audit Friday's reads** on sensitive ops endpoints (at minimum `/ops/logs` + the log-search path): actor (Friday-token vs admin), query params, result counts. Reuse `@audit_admin_action` for any (future) mutating ops endpoints — P1–P3 are read-only.
- **Ops-layer self-observability** — EMF metrics for per-domain AWS-call latency/throttle/error counts + a **CE-request counter with an alarm** (the cost circuit-breaker's telemetry).

## 8. IAM additions (`service-stack.ts` backend task role)

The role today holds an **explicit 4-action ECS enumeration** (`DescribeServices`, `DescribeTasks`, `ListServices`, `ListTasks` — cluster-scoped; **not** a `Describe*/List*` wildcard, so other ECS reads are not covered), `cloudformation:DescribeStacks`, and CloudWatch Logs read scoped to `/ecs/isol8-{env}`.

| Phase | Grant | Actions | Resource |
|---|---|---|---|
| P1 | Deploy root-cause | `cloudformation:DescribeStackEvents` | `*` |
| P1 | Alarms (whole attention strip) | `cloudwatch:DescribeAlarms` | `*` |
| P2 | Logs read widen | *(no new actions)* — **add `/isol8/{env}/openclaw:*` to the existing Logs statement's resources** | existing statement + shared container group ARN |
| P3 | Cost Explorer | `ce:GetCostAndUsage`, `ce:GetCostForecast`, `ce:GetDimensionValues`, `ce:GetTags` | `*` |
| P3 | CloudWatch metrics | `cloudwatch:GetMetricData`, `ListMetrics`, `DescribeAlarmHistory` | `*` |
| Deferred (drift) | on-demand drift | `cloudformation:DetectStackDrift`, `DescribeStackDriftDetectionStatus`, `DescribeStackResourceDrifts` | `*` |

The ops layer needs nothing beyond the four ECS actions already held; if a later need arises (e.g. `DescribeContainerInstances`), add it as an explicit new grant — do not assume a wildcard exists. Every IAM change goes through the CDK synth-time `deploy.yml` drift check — commit the regenerated file.

## 9. Prerequisites / operational steps (not code)

- **Activate the `Environment` cost-allocation tag** in Billing (or `ce:UpdateCostAllocationTagsStatus`). **[fixed]** Forward-looking by default, but AWS supports **~12-month retroactive backfill** (Billing-console opt-in, ~24h to populate) **for periods where the resource already carried the tag** — so the real gate is historical tagging, not activation timing. **Verify the CDK stacks actually stamp an `Environment` tag** on billed resources (ECS services/tasks, EFS, DynamoDB, ALB); that determines whether env-grouped history exists. Do this at the START of P3.
- **(Optional) Container Insights** — only for per-task metrics; paid + no backfill. Default OFF; free service-level `AWS/ECS` metrics for v1.
- **Shared `ops-cache` DDB table** — a single generic cache table (CDK `database-stack.ts`): partition key = cache-key string (e.g. `fleet#{env}`, `cost#{env}#{granularity}#{window}`, `metrics#{env}#…`), attribute `value` = JSON snapshot, `ttl` attribute for DynamoDB auto-expiry. One `ops_cache_repo.py` serves the P1 fleet short-TTL cache and the P3 cost/metrics snapshots. Provision it + the scheduled cost refresher before the cost UI (§5.3 P3). (DDB over S3: snapshots are small, keyed, structured, and frequently read — matches the all-metadata-in-DDB convention; S3 is a mismatch.)
- **Place the owner ops `SKILL.md` + credential** after the ops API ships (operational, not committed). Specify: the persistent EFS path under `/home/node/.openclaw` + the `skills.entries` registration Friday needs to load it; the credential delivered as a secret env var / over the gateway channel (**not** a plaintext EFS file); how it is re-placed after a reprovision (which rewrites `openclaw.json`). SKILL.md content = endpoint catalog, auth-header format, worked examples, boundaries — **guiding, not dictating** (per `feedback_guide_agents_not_dictate`).

## 10. Coherence fixes folded in (from the audit)

Orphaned Logs viewer surfaced; platform-wide audit view added; `/admin` landing; live-ECS fleet (not DDB); one admin fetch client; shared design-system components; region single-sourced at **both** `container/page.tsx:88` and `ContainerActionsPanel.tsx:289` via `NEXT_PUBLIC_AWS_REGION`. (The `admin_catalog.pyc` "deletion" was a no-op — the module is gone and no `.pyc` is tracked — so it's dropped; just confirm no stray imports.)

## 11. Testing

- **Backend ops services — HAND mocks, not moto for the paid paths:** moto does not faithfully emulate `ce:GetCostAndUsage`/`GetCostForecast` or `cloudwatch:GetMetricData` (canned/empty), and does not reproduce the stopped-task 1h TTL. Use hand mocks for the `ce` + `GetMetricData` DTO normalization + gotchas (`Estimated:true`, empty-metrics-as-down). Keep moto for ECS Describe + CFN status classification.
- **Gotcha assertions:** empty-metrics-as-down (null distinct from 0), earliest-`*_FAILED`-within-current-window root cause, `ROLLBACK_COMPLETE` classification, missing-task-from-`DescribeTasks` → graceful degrade (reframed from the AWS 1h TTL).
- **DTO envelope:** a boto3 error/throttle surfaces as `source_state != "ok"` with the domain in `errors[]` — **never empty `data`**.
- **Redactor:** false-negative tests per secret shape (§7) + a boundary test that raw AWS payloads never reach the logger.
- **Ops API auth:** Clerk-admin OR `platform_ops`-token accepted; **a tenant `paperclip_service` token REJECTED**; agent DTO withholds raw ARNs/Outputs; redactor applied on log responses.
- **Frontend:** targeted vitest per new component (never full `pnpm test` — ~17 GB RAM); `tsc --noEmit` over test files (annotate mock-builder return types); interface-design `audit` against `system.md`.
- **No live AWS in CI** — everything mocked; live smoke on dev after deploy.

## 12. Decomposition → plans

Three phase-plans (P1 foundation+health+deploys, P2 logs/debug, P3 cost/metrics), each independently shippable. **P1 adds a small free read-only IAM grant** (CFN events + alarms) — it is not zero-IAM. Each phase's plan is written + executed subagent-driven with review gates, like the AWS integration. Each carries its **Done-when** from §5.

## 13. Decisions (resolved — architect's call)

1. **Phase ordering — foundation → logs → cost.** P1 is prerequisite (design system + ops layer + DTO envelope + auth build here); logs is the founder's explicit debugging need; cost is last (24h-lagged data + most operational overhead).
2. **Container Insights — OFF for v1.** Free service-level `AWS/ECS` metrics + `DescribeServices.runningCount` cover the need; the paid per-task metrics aren't worth it yet.
3. **Drift detection — deferred, on-demand.** Async 2-step flow; IAM sits in the deferred §8 row. `DescribeStacks.DriftInformation` (cheap, already in `DescribeStacks`) gives a staleness hint without the full detect flow.
4. **Agent-path auth — `platform_ops` token via Secrets-Manager env var (§3.1).** Distinct kind + dedicated key + owner-id binding closes the actual hole; env-var delivery is the right mechanism because Friday's consumption is outbound HTTPS. Connection-identity is future hardening (needs an agent→backend RPC path that doesn't exist).
5. **Friday's log scope — own-container streams + platform service logs (§7).** Preserves "Friday debugs with agent logs" while closing cross-tenant exfil; fleet-wide is admin-UI-only; per-tenant agent reads await an egress gate.
6. **Cost/cache store — DynamoDB (`ops-cache` table, §9).** Small keyed structured snapshots; matches platform convention; one generic table serves fleet + cost + metrics caches.

None of these block implementation. Flag any you'd overturn; otherwise P1 planning proceeds on them.
