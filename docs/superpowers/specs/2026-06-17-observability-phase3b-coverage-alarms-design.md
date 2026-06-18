# Observability Phase 3B — Coverage Alarms for Chaos Failure Modes

**Date:** 2026-06-17
**Status:** Approved design, pending implementation plan
**Goal:** Close the observability coverage gaps for the failure modes the chaos suite (SP2 FIS) will induce, so SP2 Phase B / Phase 3C has real recovery signals to assert against — using native CloudWatch metrics + the existing event Lambdas, at **negligible cost** (Container Insights stays off).
**Source:** `docs/superpowers/research/2026-06-17-observability-audit.md` §E. Sibling: Phase 1 (alarm hygiene, merged), Phase 2 (backend telemetry, merged), Phase 3A (alarm wiring, merged).

## Scope

The audit listed 7 coverage gaps; **3A already closed three** (gateway reconnect-throttle → W54, DynamoDB throttle → P8 AWS-native, Metronome → W55/W56). 3B closes the four remaining gaps that have **no usable signal today**:

1. Per-user container CPU saturation + crash-loop (the #1 gap)
2. Aurora / Paperclip outage (zero RDS/Paperclip alarms exist)
3. Provision-gate-block-rate (a provider outage silently gates users)
4. EFS `>1-task-per-access-point` (the SQLite-WAL-corruption precondition)

**Out of scope:** config-brick-on-boot detection — no clean signal exists; it surfaces indirectly via `container.error_state`. Deferred.

## Cost posture (a hard constraint of this design)

Every mechanism uses either a **native AWS metric** (`AWS/ECS`, `AWS/RDS` — published automatically, no cost beyond standard CloudWatch) or a small inline **event Lambda** (the existing `TaskStoppedFn` + the new `MultiTaskFn`), which fire only on ECS task START/STOP events. Honest cost (not "free"): Lambda has a *perpetual* 1M-request + 400k-GB-s/month free tier, and these handlers' volume sits well under it on its own — so likely $0; **even if the account is over the aggregate, the marginal cost is pennies/month** (<$0.20). The small real cost is the custom CloudWatch metrics themselves (~$0.30/metric/month). Net: **~<$1/month total** — vs **~$10s/month** for the rejected alternative, Container Insights (which publishes many metrics per task/service across a dynamic per-user fleet). **Container Insights stays OFF** (`container-stack.ts:92`); the Lambda path is the cost-*minimizing* choice, ~50–100× cheaper.

## Mechanism per gap

### 1. Per-user CPU-storm crash-loop (W58)
Per-user services are dynamic (`openclaw-{user_id}-{hash}`) and share one ECS cluster with the backend + paperclip services.

- **Crash-loop (the signal we build)** — a new **windowed-count alarm** on the existing `TaskStoppedUnexpected` metric (the `TaskStoppedFn` Lambda already emits it): `Sum >= 3 in 5min` distinguishes a CPU-storm restart *loop* from W39's existing `Sum > 0` (any single unexpected stop). Reuses the free Lambda + metric; no new emission. This catches the CPU-storm failure mode that *actually causes an incident*: the 0.5-vCPU box starves → healthcheck SIGKILL → restart → repeat.
- **No standalone per-user-CPU-saturation alarm.** A direct "any per-user container >90% CPU" alarm is **not cleanly expressible**: CloudWatch alarms can't use `SEARCH()`, Metrics Insights has no `LIKE`/wildcard (only `=`/`!=`/`AND`), and per-user/backend/paperclip share one cluster so an unfiltered `MAX(CPUUtilization)` would wrongly include the others. Tag-based Metrics Insights filtering on `AWS/ECS` metrics is fragile/conditional; a dedicated per-user cluster is out of scope. **Decision: rely on W58 (crash-loop) + the live `chat.e2e.latency` SLO** (latency spikes as the box starves) to cover the storm; pure CPU-saturation-without-kill is a documented, accepted gap (degraded ≠ down). Revisit later with service-tagging + live validation if needed.

### 2. Aurora outage + Paperclip outage
Thread `paperclipDbCluster` (Aurora) + the Paperclip Fargate `service` into `ObservabilityStackProps` from the stage (they exist as `database.paperclipDbCluster` + `paperclip.service`; ObservabilityStack does not currently receive them).
- **Aurora saturation** — `AWS/RDS` alarms on the cluster: `CPUUtilization` (W60) + `DatabaseConnections` high-watermark (W61), dims `DBClusterIdentifier`.
- **Aurora outage/failover** — saturation alarms don't fire when an outage drops connections toward zero, so add an **RDS failover/failure EventBridge rule** (`source: aws.rds`, cluster events, `EventCategories: ['failover','failure']`) → warn SNS.
- **Paperclip CPU saturation** — `AWS/ECS CPUUtilization` on the singular Paperclip service (W62) — works while running.
- **Paperclip DOWN** — Paperclip has **no ALB target group** (it's Cloud-Map-discovered + reached *through the backend*), and `AWS/ECS` metrics vanish at zero tasks, so neither an ALB-health nor an ECS-metric alarm works. The honest signal is **backend-side**: a new `paperclip.upstream.error{kind}` metric emitted where the backend's calls to Paperclip fail (the `paperclip_proxy.py` `except httpx.HTTPError` handler + `PaperclipAdminClient`'s request helpers), alarmed on `kind="unreachable" > 0` (W63). This ships in **Part B** (backend), not Part A.

### 3. Provision-gate-block-rate
`evaluate_provision_gate(*, owner_id: str, is_admin: bool = True)` (`core/services/provision_gate.py`) returns a `Gate` (blocked) or `None` and emits **no metric today** (the module has no `put_metric` import + no logger — both must be added). Wrap it (rename the body to `_evaluate_provision_gate_inner`, add a thin same-signature async wrapper) so a single emit covers all branches: on a non-None Gate, emit **both** a dimensionless `provision.gate.blocked` (for the total-across-codes alarm — EMF one-stream-per-set means the alarm querying `{env,service}` needs a no-dim stream) **and** a `provision.gate.blocked{code}` for the per-code breakdown. The `code` set is bounded (the 7 real Gate codes: `signup_required`, `payment_past_due`, `subscription_required`, `provider_required`, `credits_required`, `oauth_required`, `byo_provider_required`). Warn alarm on a *spike* of blocks (a provider outage gating all new chats), not `>0` (some blocks are normal unsubscribed traffic). Best-effort emission (never raises into the gate path).

### 4. EFS `>1-task-per-access-point`
The SQLite-WAL-corruption precondition is two tasks writing one per-user EFS access point (root cause already mitigated by the `maxPercent=100` single-writer deploy fix; this detects a *regression* of that). Basic `AWS/ECS` has no `RunningTaskCount` (that's a Container-Insights metric we're deliberately not enabling), so **extend the existing free Lambda pattern**: add a sibling EventBridge rule on `ECS Task State Change → RUNNING` for the per-user cluster; the handler reads the event's `detail.group` (`service:<name>`), filters to `openclaw-*` services (the rule is cluster-wide so it also sees the backend/paperclip tasks — must exclude them), calls `ecs:list_tasks(serviceName, desiredStatus=RUNNING)` and emits `MultiTaskPerService` when a per-user service has `>1` running task → warn alarm `Sum > 0`. IAM: `ecs:ListTasks` (with `ecs:cluster` condition) and `cloudwatch:PutMetricData` (with a `cloudwatch:namespace` condition) must be **separate statements** — `PutMetricData` doesn't support the `ecs:cluster` key. Reuses the free-Lambda mechanism; no Container Insights, no managed cost.

### 5. `chat.e2e.latency` late-terminal guard (Part B)
The shipped Phase 2 metric (`connection_pool.py`) emits `elapsed_ms = max(0.0, now - started_at)` at the terminal — clamping only the floor, with **no ceiling**. A run that hangs and then emits a terminal minutes later (or a stale/re-delivered terminal after a reconnect) emits a multi-minute elapsed that inflates the p99 and can **false-fire W9 (p99 > 20s SLO)**. ("End never comes" is fine — nothing emits — only a *late* end spikes.) The deleted bounded-tracker had a 30-min orphan-drop; the `_active_runs` rewrite lost it. Fix: **skip the latency emit when `elapsed_ms` exceeds a sane ceiling** (a hung run isn't a real latency datapoint), optionally bumping a separate `chat.run.abandoned` counter. Restores the protection in one guard, best-effort, on the same passive path.

## Architecture / PR split

Two PRs, by risk + deploy path:

- **3B-infra** (`apps/infra/lib/stacks/observability-stack.ts` + the cross-stack prop in `isol8-stage.ts` + the Lambda extension): CPU `SEARCH` alarm, crash-loop count alarm, EFS multi-task Lambda + alarm, Aurora/Paperclip alarms.
  - ⚠️ **deploy.yml:** adding `paperclipDbCluster` + `paperclip.service` to `ObservabilityStackProps` creates a **new stack dependency** (observability → paperclip/database), which changes the generated `.github/workflows/deploy.yml` `needs:` graph. The regenerated `deploy.yml` MUST be committed — `cdk-pipelines-github` has a synth-time drift check (`pipeline.ts:469`) that fails the build and blocks ALL dev+prod deploys if it's stale. Never revert the regen.
- **3B-backend** (`core/services/provision_gate.py` + its alarm in observability-stack): the `provision.gate.blocked{reason}` emission + alarm + tests. Backend code change, like Phase 2.

## Thresholds + treatMissingData

- All new alarms default `treatMissingData: NOT_BREACHING` (absent metric = healthy) — no breach-on-missing, per the Phase 1 lesson. Security/precondition trip-wires (EFS multi-task) use `Sum > 0`, 1×1m.
- The per-user CPU `SEARCH` threshold and the crash-loop `Sum > N` threshold are starting values to be **baselined after observation** (flagged provisional, like the audit's W40/W41/W50/W51) — `NOT_BREACHING` so they can't false-page on absence.

## Ties to 3C (why this is the prerequisite)

These are exactly the recovery signals the parked SP2 Phase B / 3C scenarios assert against: CPU + crash-loop ↔ the CPU-stress scenario; Aurora/Paperclip ↔ the Aurora-failover + Paperclip-kill scenarios; provision-gate ↔ the billing-egress scenario. Without 3B, those scenarios have nothing trustworthy to verify recovery against.

## Open risks

- **`SEARCH` expression correctness:** the metric-math `SEARCH('{AWS/ECS,ClusterName,ServiceName} ...')` must scope to the per-user cluster and match `openclaw-*` service names; the plan verifies the exact expression syntax + that it MAX-aggregates (an empty match must not alarm). Validate against `cdk synth` + the real metric names.
- **EFS Lambda RUNNING-event semantics:** an `ECS Task State Change → RUNNING` fires per task; counting `>1 per service` needs the handler to query current running tasks for that service (describe_services / list_tasks) rather than rely on a single event — the plan specifies the exact API. Keep it best-effort + low-IAM.
- **Provision-gate emission placement:** must cover every blocking branch in `evaluate_provision_gate` with the right bounded `reason`, and never raise into the gate.
- **deploy.yml drift** (above) — the one prod-blocking risk; the plan makes committing the regen an explicit step.
