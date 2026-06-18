# Observability Phase 3B — Coverage Alarms for Chaos Failure Modes

**Date:** 2026-06-17
**Status:** Approved design, pending implementation plan
**Goal:** Close the observability coverage gaps for the failure modes the chaos suite (SP2 FIS) will induce, so SP2 Phase B / Phase 3C has real recovery signals to assert against — using native CloudWatch metrics + the one existing (free) Lambda, with **no new managed cost** (Container Insights stays off).
**Source:** `docs/superpowers/research/2026-06-17-observability-audit.md` §E. Sibling: Phase 1 (alarm hygiene, merged), Phase 2 (backend telemetry, merged), Phase 3A (alarm wiring, merged).

## Scope

The audit listed 7 coverage gaps; **3A already closed three** (gateway reconnect-throttle → W54, DynamoDB throttle → P8 AWS-native, Metronome → W55/W56). 3B closes the four remaining gaps that have **no usable signal today**:

1. Per-user container CPU saturation + crash-loop (the #1 gap)
2. Aurora / Paperclip outage (zero RDS/Paperclip alarms exist)
3. Provision-gate-block-rate (a provider outage silently gates users)
4. EFS `>1-task-per-access-point` (the SQLite-WAL-corruption precondition)

**Out of scope:** config-brick-on-boot detection — no clean signal exists; it surfaces indirectly via `container.error_state`. Deferred.

## Cost posture (a hard constraint of this design)

Every mechanism uses either a **native AWS metric** (`AWS/ECS`, `AWS/RDS` — published automatically, no cost beyond standard CloudWatch) or the **existing `TaskStoppedFn` Lambda** (low-volume task-stop events, comfortably in the Lambda free tier). **Container Insights stays OFF** (`container-stack.ts:92`) — enabling it would publish many per-task metrics and cost *more* than the Lambda. No new managed services.

## Mechanism per gap

### 1. Per-user CPU saturation + crash-loop
Per-user services are dynamic (`openclaw-{user_id}-{hash}`, created/destroyed at runtime), so a static per-service alarm (like the singular backend's W36) is impossible. Two sub-signals:

- **CPU saturation** — a CloudWatch **`SEARCH()` metric-math alarm** over `AWS/ECS CPUUtilization` for all `openclaw-*` services in the per-user cluster, MAX-aggregated, so it fires when *any* per-user container is CPU-saturated. Native; no Lambda; handles the dynamic fleet. (AWS/ECS publishes per-service `CPUUtilization` automatically without Container Insights — the W36 pattern.)
- **Crash-loop** — a new **windowed-count alarm** on the existing `TaskStoppedUnexpected` metric (the `TaskStoppedFn` Lambda already emits it): `Sum > N in 5min` distinguishes a restart *loop* from W39's existing `Sum > 0` (any single unexpected stop). Reuses the free Lambda + metric; no new emission.

### 2. Aurora / Paperclip outage
Thread `paperclipDbCluster` (Aurora) + the Paperclip Fargate `service` into `ObservabilityStackProps` from the stage (they exist as `database.paperclipDbCluster` + `paperclip.service`; ObservabilityStack does not currently receive them). Add **native** alarms:
- `AWS/RDS` on the Aurora cluster: `CPUUtilization`, `DatabaseConnections` (saturation), and failover detection (an RDS failover EventBridge event or the `DatabaseConnections`-drop signal).
- `AWS/ECS` on the Paperclip Fargate service: `CPUUtilization` + an unhealthy/zero-running-task check (mirror the backend W36/W28 patterns; Paperclip's service IS singular + CDK-known, so static per-service alarms work here, unlike the per-user fleet).

### 3. Provision-gate-block-rate
`evaluate_provision_gate` (`core/services/provision_gate.py`) returns a `Gate` (blocked) or `None` and emits **no metric today**. Add a **new backend emission**: when the gate blocks, emit `provision.gate.blocked` with a **bounded** `reason` dimension (`subscription_inactive` / `trial_blocked` / `metronome_credits` / `oauth_missing` — the existing gate branches). Then a warn alarm on the block rate/count so a provider outage that silently gates all new chats becomes visible. Best-effort emission (never raises into the gate path).

### 4. EFS `>1-task-per-access-point`
The SQLite-WAL-corruption precondition is two tasks writing one per-user EFS access point (root cause already mitigated by the `maxPercent=100` single-writer deploy fix; this detects a *regression* of that). Basic `AWS/ECS` has no `RunningTaskCount` (that's a Container-Insights metric we're deliberately not enabling), so **extend the existing free Lambda pattern**: add a sibling EventBridge rule on `ECS Task State Change → RUNNING` for the per-user cluster; the handler counts running tasks for the event's service and emits a `container.multi_task_per_ap` metric when a per-user service has `>1` running task → page/warn alarm `Sum > 0`. Reuses the free-Lambda mechanism; no Container Insights, no managed cost.

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
