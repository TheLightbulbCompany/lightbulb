# Observability Phase 3B — Coverage Alarms Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add CloudWatch alarms for the uncovered chaos failure modes (per-user crash-loop, EFS >1-task-per-AP, Aurora/Paperclip outage) plus a provision-gate-block signal, a Paperclip-upstream-error signal, and a chat-latency late-terminal guard — using native AWS metrics + the existing event Lambdas + a few bounded custom metrics, at negligible cost (~<$1/month), no Container Insights.

**Architecture:** Two PRs. **Part A (infra)** edits `observability-stack.ts` (+ cross-stack props in `isol8-stage.ts`, + extend the inline `TaskStoppedFn` Lambda). **Part B (backend)** adds metric emissions in `provision_gate.py`, `paperclip_proxy.py`/`paperclip_admin_client.py`, and a latency ceiling guard in `connection_pool.py`, plus their alarms. Per-user services are dynamic (`openclaw-{user_id}-{hash}`), so there is NO standalone per-user-CPU alarm — see the Decision note in Task 1.

**Tech Stack:** AWS CDK (TypeScript, aws-cdk-lib 2.190.0), CloudWatch (Metric, MathExpression, Alarm), EventBridge, inline Python Lambda (boto3), FastAPI backend (Python), pytest.

**Spec:** `docs/superpowers/specs/2026-06-17-observability-phase3b-coverage-alarms-design.md`

## Global Constraints

- **No Container Insights** — it stays `false` (`container-stack.ts:92`); use native `AWS/ECS`/`AWS/RDS` metrics + the existing event Lambdas + a few bounded custom metrics only.
- **Cost:** negligible cost (~<$1/month: event Lambdas are pennies even if past the perpetual free tier, plus ~$0.30/metric/month for the custom metrics) — vs ~$10s/month for the rejected Container Insights alternative. Container Insights stays off.
- **All new alarms `treatMissingData: NOT_BREACHING`** (absent metric = healthy) — never breach-on-missing (Phase 1 lesson).
- **No high-cardinality metric dimensions** — `_DENIED_DIMENSIONS` in `metrics.py` forbids `user_id`/`owner_id`/`container_id`/`request_id`/`admin_user_id`/`tool_id`; the gate `code` set is bounded (safe).
- **deploy.yml drift (Part A only):** adding cross-stack props to `ObservabilityStackProps` changes the generated `.github/workflows/deploy.yml` `needs:` graph. After `cdk synth`, COMMIT the regenerated `deploy.yml` — `cdk-pipelines-github` (`pipeline.ts:469`) has a synth-time drift check that fails the build and blocks ALL dev+prod deploys if it's stale. NEVER revert the regen.
- **Branch:** `chore/observability-phase3b`. Commit per task. Never push without approval.
- All new alarm SNS actions: page-tier → `this.pageTopic`, warn-tier → `this.warnTopic` (mirror existing alarms). Page alarms append their ARN to `this.pageAlarmArns` only via the `createAlarm` helper; hand-built alarms that should page must `this.pageAlarmArns.push(alarm.alarmArn)` explicitly IF they belong in the FIS page set — but a per-user-fleet CPU/crash-loop/multi-task alarm must NOT go in `pageAlarmArns` if it could self-trip a chaos scenario (these are warn-tier anyway).

---

# PART A — Infra alarms (PR 1: `feat(observability): Phase 3B-infra — coverage alarms`)

## Task 1: Per-user crash-loop alarm (W58, windowed count on existing metric)

> **Decision — NO standalone per-user-CPU alarm.** The earlier draft built a W57 per-user-CPU alarm via a Metrics Insights query with `WHERE ServiceName LIKE 'openclaw%'`. This is **un-implementable**: CloudWatch Metrics Insights has **no `LIKE`/wildcard operator** (only `=`, `!=`, `AND`), and the per-user + backend + paperclip services **share one ECS cluster** — so an unfiltered `MAX(CPUUtilization)` would wrongly fold in the backend/paperclip services with no way to name-filter to the `openclaw-*` fleet. Tag-based filtering is fragile and a separate cluster is out of scope. So **no W57 is built.** The CPU-storm failure mode that actually causes incidents (0.5-vCPU starve → healthcheck SIGKILL → restart) is caught by **this crash-loop alarm (W58)** plus the live `chat.e2e.latency` SLO (alarm W9). Pure CPU-saturation-*without*-a-kill is a documented **accepted gap**.

**Files:**
- Modify: `apps/infra/lib/stacks/observability-stack.ts` (right after the existing W39 TaskStopped block)

**Interfaces:**
- Consumes: the existing `Isol8/ECS` `TaskStoppedUnexpected` metric (emitted by `TaskStoppedFn`), `clusterName`, `this.warnTopic`.
- Produces: alarm `isol8-${env}-W58-per-user-crash-loop`.

- [ ] **Step 1: Add the windowed-count crash-loop alarm**

W39 already pages on `TaskStoppedUnexpected Sum > 0` (any single unexpected stop). A crash-*loop* is *many* unexpected stops in a short window. Add a distinct alarm:

```typescript
    // W58: per-user container crash-loop. W39 fires on ANY single unexpected
    // stop; a crash-LOOP (CPU-starve → healthcheck SIGKILL → restart → repeat)
    // produces a burst of unexpected stops. Reuses the same TaskStoppedUnexpected
    // metric the free TaskStoppedFn Lambda already emits — no new emission.
    // Sum >= 3 in 5min distinguishes a loop from a one-off. Provisional
    // threshold, baseline after observation.
    const crashLoopMetric = new cloudwatch.Metric({
      namespace: "Isol8/ECS",
      metricName: "TaskStoppedUnexpected",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
      dimensionsMap: { ClusterName: clusterName },
    });
    const w58 = new cloudwatch.Alarm(this, "W58", {
      alarmName: `isol8-${this.envName}-W58-per-user-crash-loop`,
      alarmDescription:
        "≥3 unexpected container stops in 5 min — a per-user container is in a crash/restart loop (likely CPU-starve healthcheck SIGKILL). Provisional threshold.",
      metric: crashLoopMetric,
      threshold: 3,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w58.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));
```

- [ ] **Step 2: Verify synth**

Run: `npx tsc --noEmit && npx cdk synth dev/isol8-dev-observability 2>/dev/null | grep -c "W58-per-user-crash-loop"`
Expected: tsc 0; grep returns ≥1.

- [ ] **Step 3: Commit**

```bash
git add apps/infra/lib/stacks/observability-stack.ts
git commit -m "feat(observability): W58 per-user crash-loop alarm (windowed TaskStoppedUnexpected)"
```

## Task 2: EFS >1-task-per-access-point detector (extend Lambda + alarm)

**Files:**
- Modify: `apps/infra/lib/stacks/observability-stack.ts` (the `TaskStoppedFn`/`TaskStoppedRule` region ~line 1130-1300 — add a sibling RUNNING rule + handler + IAM + alarm)

**Interfaces:**
- Consumes: `props.cluster` (cluster ARN + name), `this.warnTopic`.
- Produces: metric `Isol8/ECS MultiTaskPerService` (dim `ClusterName`); alarm `isol8-${env}-W59-efs-multi-task-per-ap`.

- [ ] **Step 1: Add a RUNNING-event rule + a Lambda that flags >1 running task per per-user service**

After the existing `TaskStoppedFn` wiring, add a second EventBridge rule on RUNNING task transitions and a small Lambda that queries running-task count for the event's service. The corruption precondition is two RUNNING tasks on one per-user service (one EFS access point). Add:

```typescript
    // W59: EFS >1-task-per-access-point detector. Two RUNNING tasks on one
    // per-user service write the same EFS access point → OpenClaw WAL-SQLite
    // corruption (root cause mitigated by maxPercent=100 single-writer deploys;
    // this detects a REGRESSION). Basic AWS/ECS has no RunningTaskCount (that's
    // Container Insights, which we keep off for cost), so a tiny EventBridge→
    // Lambda checks running-task count on each RUNNING transition. Reuses the
    // free-Lambda pattern; events are low-volume.
    const multiTaskRule = new events.Rule(this, "TaskRunningRule", {
      ruleName: `isol8-${this.envName}-ecs-task-running`,
      description: "Captures ECS task RUNNING transitions for >1-task-per-service detection",
      eventPattern: {
        source: ["aws.ecs"],
        detailType: ["ECS Task State Change"],
        detail: {
          clusterArn: [props.cluster.clusterArn],
          lastStatus: ["RUNNING"],
        },
      },
    });
    const multiTaskFn = new lambda.Function(this, "MultiTaskFn", {
      functionName: `isol8-${this.envName}-multi-task-metric`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.seconds(15),
      code: lambda.Code.fromInline(`
import boto3
ecs = boto3.client('ecs')
cw = boto3.client('cloudwatch')

def handler(event, _ctx):
    detail = event.get('detail') or {}
    group = detail.get('group') or ''            # e.g. "service:openclaw-<uid>-<hash>"
    cluster_arn = detail.get('clusterArn') or ''
    if not group.startswith('service:'):
        return                                   # only per-user SERVICE tasks
    service = group.split(':', 1)[1]
    if 'openclaw' not in service:
        return                                   # ignore the backend/paperclip services
    # Count currently-RUNNING tasks for this service. >1 = the double-writer
    # precondition for EFS WAL-SQLite corruption.
    running = ecs.list_tasks(cluster=cluster_arn, serviceName=service, desiredStatus='RUNNING')
    count = len(running.get('taskArns') or [])
    if count > 1:
        cw.put_metric_data(Namespace='Isol8/ECS', MetricData=[{
            'MetricName': 'MultiTaskPerService',
            'Dimensions': [{'Name': 'ClusterName', 'Value': cluster_arn.split('/')[-1]}],
            'Value': 1.0, 'Unit': 'Count',
        }])
`),
    });
    // TWO separate statements: cloudwatch:PutMetricData does NOT support the
    // ecs:cluster condition key, so combining them would make IAM evaluate the
    // condition as non-matching and DENY the metric write (the alarm would get
    // no datapoints). ecs:ListTasks keeps the cluster condition; PutMetricData
    // is scoped instead by a cloudwatch:namespace condition.
    multiTaskFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ecs:ListTasks"],
        resources: ["*"],
        conditions: { StringEquals: { "ecs:cluster": props.cluster.clusterArn } },
      }),
    );
    multiTaskFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: { StringEquals: { "cloudwatch:namespace": "Isol8/ECS" } },
      }),
    );
    multiTaskRule.addTarget(new events_targets.LambdaFunction(multiTaskFn));
```

- [ ] **Step 2: Add the alarm + verify synth**

```typescript
    const multiTaskMetric = new cloudwatch.Metric({
      namespace: "Isol8/ECS",
      metricName: "MultiTaskPerService",
      statistic: "Sum",
      period: cdk.Duration.minutes(1),
      dimensionsMap: { ClusterName: clusterName },
    });
    const w59 = new cloudwatch.Alarm(this, "W59", {
      alarmName: `isol8-${this.envName}-W59-efs-multi-task-per-ap`,
      alarmDescription:
        "A per-user service has >1 RUNNING task — two writers on one EFS access point, the OpenClaw WAL-SQLite corruption precondition. Single-writer deploy (maxPercent=100) should prevent this; firing = regression.",
      metric: multiTaskMetric,
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w59.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));
```
Run: `npx tsc --noEmit && npx cdk synth dev/isol8-dev-observability 2>/dev/null | grep -E "MultiTaskFn|W59-efs-multi-task|TaskRunningRule" | head`
Expected: tsc 0; the Lambda, rule, and alarm all appear.

- [ ] **Step 3: Commit**

```bash
git add apps/infra/lib/stacks/observability-stack.ts
git commit -m "feat(observability): W59 EFS >1-task-per-AP detector (RUNNING-event Lambda + alarm)"
```

## Task 3: Aurora + Paperclip-CPU outage alarms (cross-stack props + native alarms + deploy.yml regen)

**Files:**
- Modify: `apps/infra/lib/stacks/observability-stack.ts` (props interface + a new `createPaperclipTierAlarms` block)
- Modify: `apps/infra/lib/isol8-stage.ts` (pass the two refs + explicit deps)
- Modify (generated): `.github/workflows/deploy.yml`

**Interfaces:**
- Consumes: `database.paperclipDbCluster` (`rds.IDatabaseCluster`) and `paperclip.service` (`ecs.IBaseService`) from the stage. **NO Paperclip ALB target group** — VERIFIED: `PaperclipStack` has no ALB target group; it registers via `cloudMapOptions` on the FargateService and is reached *through* the backend, so there is no `HealthyHostCount` to alarm on. The Paperclip-down signal is delivered by the Part B `paperclip.upstream.error` metric + alarm instead (see Part B).
- Produces: alarms W60 (aurora-cpu), W61 (aurora-connections-saturation), W62 (paperclip-cpu via `AWS/ECS`); EventBridge rule `AuroraFailoverRule` (failover/failure → warn). **No W63 here** — the old ALB-`HealthyHostCount` W63 is dropped (no target group exists) and replaced by the Part B `paperclip.upstream.error` alarm, which also keeps the `W63` id.

- [ ] **Step 1: Add the props to `ObservabilityStackProps`**

In the `ObservabilityStackProps` interface (~line 34), add:
```typescript
  paperclipDbCluster: rds.IDatabaseCluster;
  paperclipService: ecs.IBaseService;
```
Add the import if absent: `import * as rds from "aws-cdk-lib/aws-rds";` (ecs is already imported). Also confirm `events_targets.SnsTopic` is importable (used by `AuroraFailoverRule`) — `aws-cdk-lib/aws-events-targets` is already imported as `events_targets` for the TaskStopped Lambda target.

- [ ] **Step 2: Thread them from the stage + add explicit deps**

In `apps/infra/lib/isol8-stage.ts`, the `new ObservabilityStack(...)` call (~line 163) is currently un-captured. Capture it (`const observability = new ObservabilityStack(...)`) and add to its props:
```typescript
        paperclipDbCluster: database.paperclipDbCluster,
        paperclipService: paperclip.service,
```
Then, after the call, add explicit dependencies:
```typescript
    observability.addDependency(database);
    observability.addDependency(paperclip);
```
VERIFIED construction order is `database` (line 40) → `paperclip` (line 143) → `observability` (line 163), so observability already follows both — these explicit deps are safe and mirror the existing `paperclip.addDependency(database)` precedent (lines 157-160). They guarantee the cross-stack `needs:` edges the deploy.yml regen (Step 5) depends on. (`paperclip.service` is `public readonly service: ecs.FargateService` and `database.paperclipDbCluster` is `public readonly paperclipDbCluster: rds.DatabaseCluster` — both already exported, no stack edits needed.)

- [ ] **Step 3: Add the native RDS + Paperclip-ECS alarms**

Add a new private method `createPaperclipTierAlarms()` called from the constructor alongside the other alarm groups:
```typescript
  private createPaperclipTierAlarms(props: ObservabilityStackProps): void {
    const dbId = props.paperclipDbCluster.clusterIdentifier;
    // W60: Aurora CPU
    const w60 = new cloudwatch.Alarm(this, "W60", {
      alarmName: `isol8-${this.envName}-W60-aurora-cpu`,
      alarmDescription: "Paperclip Aurora CPU >80% for 15 min",
      metric: new cloudwatch.Metric({
        namespace: "AWS/RDS",
        metricName: "CPUUtilization",
        statistic: "Average",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { DBClusterIdentifier: dbId },
      }),
      threshold: 80,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w60.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W61: Aurora connections — SATURATION only (high watermark). Provisional.
    const w61 = new cloudwatch.Alarm(this, "W61", {
      alarmName: `isol8-${this.envName}-W61-aurora-connections`,
      alarmDescription: "Paperclip Aurora DatabaseConnections >100 for 15 min — connection saturation. Provisional threshold.",
      metric: new cloudwatch.Metric({
        namespace: "AWS/RDS",
        metricName: "DatabaseConnections",
        statistic: "Maximum",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { DBClusterIdentifier: dbId },
      }),
      threshold: 100,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w61.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // W61b: Aurora OUTAGE / FAILOVER — distinct from saturation. W60/W61 (CPU,
    // high-connections, NOT_BREACHING) do NOT fire when a failover/outage drops
    // connections toward zero or stops reporting. RDS publishes failover +
    // failure events to EventBridge; route them straight to the warn topic so
    // the 3C Aurora-failover scenario has a recovery signal. (Event categories:
    // "failover" + "failure"; covers RDS-EVENT-0031/0071/0036 etc.)
    const auroraEventRule = new events.Rule(this, "AuroraFailoverRule", {
      ruleName: `isol8-${this.envName}-aurora-failover`,
      description: "Paperclip Aurora failover/failure events → warn",
      eventPattern: {
        source: ["aws.rds"],
        detailType: ["RDS DB Cluster Event", "RDS DB Instance Event"],
        detail: {
          SourceIdentifier: [dbId],
          EventCategories: ["failover", "failure"],
        },
      },
    });
    auroraEventRule.addTarget(new events_targets.SnsTopic(this.warnTopic));

    // W62: Paperclip Fargate service CPU. The Paperclip service is singular +
    // CDK-known, so a static per-service AWS/ECS alarm works (unlike the
    // per-user fleet).
    const clusterName = props.cluster.clusterName;
    const pcServiceName = cdk.Fn.select(2, cdk.Fn.split("/", props.paperclipService.serviceArn));
    const w62 = new cloudwatch.Alarm(this, "W62", {
      alarmName: `isol8-${this.envName}-W62-paperclip-service-cpu`,
      alarmDescription: "Paperclip Fargate service CPU >80% for 15 min",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ECS",
        metricName: "CPUUtilization",
        statistic: "Average",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { ClusterName: clusterName, ServiceName: pcServiceName },
      }),
      threshold: 80,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w62.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));

    // NO W63 here. The "Paperclip down" signal does NOT come from an ALB
    // HealthyHostCount alarm — VERIFIED: PaperclipStack has no ALB target group
    // (it registers via cloudMapOptions and is reached through the backend), so
    // there is no HealthyHostCount metric to alarm on. The down signal is the
    // Part B `paperclip.upstream.error{kind:"unreachable"}` metric + its alarm
    // (which keeps the W63 id), emitted from the backend's Paperclip-call paths.
  }
```
Call it from the constructor next to the other `this.create*Alarms()` calls. Note: the RDS-event rule filters on the cluster id via `SourceIdentifier`. Cluster-level failover events carry the cluster id in `SourceIdentifier`, so the `dbId` filter catches **failover** — the primary 3C signal. Instance-failure events carry instance ids and will NOT match the cluster-id filter — that's an accepted gap; broaden `SourceIdentifier` to include instance ids later if instance-failure coverage is wanted.

- [ ] **Step 4: Verify synth (incl. cross-stack refs resolve)**

Run (from `apps/infra`; symlink parent node_modules if absent: `ln -s /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/apps/infra/node_modules ./node_modules`, remove after):
`npx tsc --noEmit && npx cdk synth dev/isol8-dev-observability 2>/dev/null | grep -E "W60-aurora|W61-aurora|W62-paperclip|AuroraFailoverRule" | head`
Expected: tsc 0; the three alarms (W60, W61, W62) + the `AuroraFailoverRule` EventBridge rule present; W62's metric is `AWS/ECS CPUUtilization`; no `W63` in this stack from this task; no "cannot find" cross-stack errors.

- [ ] **Step 5: Regenerate + COMMIT deploy.yml (the prod-blocking step)**

Run: `npx cdk synth >/dev/null 2>&1` then from repo root `git status --short`.
Expected: `.github/workflows/deploy.yml` shows as modified (new observability→paperclip/database `needs:` edges, created by the Step 2 explicit deps). `git add` it. If it does NOT change, note that (means the dependency already existed). Then remove any node_modules symlink so the tree is clean.
> VERIFIED required: `cdk-pipelines-github` has a synth-time deploy.yml drift check (`pipeline.ts:469`); cross-stack refs change the `needs:` graph, so a stale `deploy.yml` fails Synthesize and blocks ALL dev+prod deploys. NEVER revert the regen.

- [ ] **Step 6: Commit**

```bash
git add apps/infra/lib/stacks/observability-stack.ts apps/infra/lib/isol8-stage.ts .github/workflows/deploy.yml
git commit -m "feat(observability): W60-W62 Aurora + Paperclip-CPU outage alarms (cross-stack props + deploy.yml regen)"
```

---

# PART B — Backend signals + their alarms (PR 2: `feat(observability): Phase 3B-backend — provision-gate + paperclip-upstream + latency-guard`)

> Branch off the SAME `chore/observability-phase3b` after Part A, OR a separate branch `chore/observability-phase3b-backend` off main. Each emission + its alarm ship together (no phantom-alarm window). This PR touches the backend + alarms in observability-stack.ts; those alarms do NOT add a stack dependency, so deploy.yml is unchanged.
>
> Part B adds three signals: (4) `provision.gate.blocked{code}` + the W64 spike alarm; (5) `paperclip.upstream.error{kind}` + the W63 Paperclip-down alarm (REPLACES the dropped ALB-based W63 from Part A); (6) a `chat.e2e.latency` late-terminal ceiling guard in `connection_pool.py` (no new alarm — protects the existing W9 p99 SLO).

## Task 4: Emit `provision.gate.blocked{code}` from the gate

**Files:**
- Modify: `apps/backend/core/services/provision_gate.py`
- Test: `apps/backend/tests/unit/services/test_provision_gate_metric.py` (NEW file)

**Interfaces:**
- VERIFIED real signature: `async def evaluate_provision_gate(*, owner_id: str, is_admin: bool = True) -> Gate | None`. It takes ONLY `owner_id` + `is_admin` (keyword-only). It fetches `account` internally via `billing_repo.get_by_owner_id(owner_id)` and derives `provider_choice`/`owner_role` itself — there are NO `account`/`provider_choice`/`token_owner`/`owner_role` params.
- Consumes: the `Gate` dataclass `.code` field. The 7 real `code` values (VERIFIED, use exactly): `signup_required`, `payment_past_due`, `subscription_required`, `provider_required`, `credits_required`, `oauth_required`, `byo_provider_required`.
- Produces: TWO emissions whenever `evaluate_provision_gate` returns a non-None Gate — a dimensionless `provision.gate.blocked` (for the W64 total-across-codes alarm) AND a `provision.gate.blocked{code}` (for the per-code dashboard/Insights breakdown). Both are needed because EMF publishes one stream per full dimension-key set, so an alarm querying `{env,service}` only would NOT match a `{code}`-dimensioned stream.
- Callers: there are exactly **3 production callers** — `routers/container.py` (x2), `routers/teams/agents.py`, `core/services/user_provision.py` — all invoking `evaluate_provision_gate(owner_id=..., is_admin=...)`. The single wrapper emit covers all of them; no caller changes needed.

- [ ] **Step 1: Write the failing test**

The test must call the REAL signature `evaluate_provision_gate(owner_id=..., is_admin=True)` and drive a blocked branch by mocking `core.services.provision_gate.billing_repo.get_by_owner_id` to return an account dict that triggers a gate (e.g. inactive subscription), plus mock `is_subscription_active`/`is_trial_blocked`/`metronome_gate.is_owner_blocked`/`_has_oauth_tokens` as needed — mirror the existing `tests/unit/services/test_provision_gate.py` + `test_provision_gate_credits.py` patterns.

```python
# apps/backend/tests/unit/services/test_provision_gate_metric.py
import pytest
from unittest.mock import patch, AsyncMock
from core.services import provision_gate


@pytest.mark.asyncio
async def test_blocked_gate_emits_metric_with_code():
    # An inactive subscription must block AND emit provision.gate.blocked{code}.
    account = {"subscription_status": "canceled"}  # shape that triggers a gate
    with (
        patch("core.services.provision_gate.put_metric") as mock_put,
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=account),
        ),
        patch("core.services.provision_gate.is_subscription_active", return_value=False),
        patch("core.services.provision_gate.is_trial_blocked", return_value=True),
    ):
        gate = await provision_gate.evaluate_provision_gate(owner_id="org_x", is_admin=True)
    assert gate is not None
    calls = [c for c in mock_put.call_args_list if c.args and c.args[0] == "provision.gate.blocked"]
    # Two emissions: one dimensionless (for the W64 total alarm) + one coded
    # (for the per-code breakdown). EMF = one stream per full dim set, so the
    # alarm querying {env,service} needs the dimensionless stream to exist.
    dims = [c.kwargs.get("dimensions") for c in calls]
    assert None in dims, "expected a dimensionless emission for the alarm"
    assert {"code": gate.code} in dims, "expected a coded emission for the breakdown"


@pytest.mark.asyncio
async def test_allowed_provision_emits_no_metric():
    account = {"subscription_status": "active"}
    with (
        patch("core.services.provision_gate.put_metric") as mock_put,
        patch(
            "core.services.provision_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=account),
        ),
        patch("core.services.provision_gate.is_subscription_active", return_value=True),
        patch("core.services.provision_gate.is_trial_blocked", return_value=False),
        patch(
            "core.services.provision_gate.metronome_gate.is_owner_blocked",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "core.services.provision_gate._has_oauth_tokens",
            new=AsyncMock(return_value=True),
        ),
    ):
        gate = await provision_gate.evaluate_provision_gate(owner_id="org_x", is_admin=True)
    blocked = [c for c in mock_put.call_args_list if c.args and c.args[0] == "provision.gate.blocked"]
    assert gate is None
    assert blocked == []
```
> Adjust the exact set of mocks + the account shape to match the REAL gate layers (read the function body from line 156 and the credits/oauth layers it falls through). The fixed contract: blocked → exactly one dimensionless emit + one `{code}` emit; allowed (gate None) → zero `provision.gate.blocked` emits. Confirm the actual mock targets for `metronome_gate.is_owner_blocked` (lazy-imported as `is_owner_blocked` inside the function — patch where it's imported FROM, i.e. `core.services.metronome_gate.is_owner_blocked`) before running.

- [ ] **Step 2: Run it — fails (no emission yet)**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_provision_gate_metric.py -o addopts="" -q`
Expected: FAIL (no `provision.gate.blocked` call).

- [ ] **Step 3: Wrap the gate to emit once at the return boundary**

VERIFIED the module has NO `put_metric` import and NO `logger` — both must be added. The body of `evaluate_provision_gate` is confirmed clean (no decorators, not in `__all__`, no internal self-references), so renaming it to a private inner + adding a thin same-signature wrapper is safe. The wrapper MUST reproduce the exact keyword-only signature `(*, owner_id: str, is_admin: bool = True)`. This is the single-point, all-branches-covered approach (don't instrument each `return Gate`):

```python
import logging
from core.observability.metrics import put_metric
# ... existing imports ...

logger = logging.getLogger(__name__)  # if the module has none


async def evaluate_provision_gate(
    *,
    owner_id: str,
    is_admin: bool = True,
) -> Gate | None:
    gate = await _evaluate_provision_gate_inner(owner_id=owner_id, is_admin=is_admin)
    if gate is not None:
        # Bounded `code` set → safe metric dimension. Best-effort: a metrics
        # outage must never block the gate decision (which is the request path).
        try:
            # Dimensionless: the W64 spike alarm queries {env,service} and (per
            # EMF one-stream-per-dim-set) can only match a no-dim stream.
            put_metric("provision.gate.blocked")
            # Coded: per-reason breakdown for dashboards/Insights. `code` is a
            # bounded set, so it's a safe dimension.
            put_metric("provision.gate.blocked", dimensions={"code": gate.code})
        except Exception:  # noqa: BLE001 — telemetry must not break provisioning
            logger.debug("provision.gate.blocked metric emission failed", exc_info=True)
    return gate
```
Rename the existing `async def evaluate_provision_gate(*, owner_id, is_admin=True)` to `async def _evaluate_provision_gate_inner(*, owner_id, is_admin=True)` (same keyword-only signature, body unchanged).

- [ ] **Step 4: Run — passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_provision_gate_metric.py -o addopts="" -q`
Expected: PASS. Then regression: `uv run pytest tests/unit/services/test_provision_gate.py tests/unit/services/test_provision_gate_credits.py -o addopts="" -q` (existing gate tests) → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/provision_gate.py apps/backend/tests/unit/services/test_provision_gate_metric.py
git commit -m "feat(observability): emit provision.gate.blocked{code} on a blocked gate"
```

## Task 5: Provision-gate-block alarm (ships with the emission)

**Files:**
- Modify: `apps/infra/lib/stacks/observability-stack.ts` (warn-tier custom-metric group, near the other Isol8-namespace alarms)

**Interfaces:**
- Consumes: the `provision.gate.blocked` metric (Task 4), `this.warnTopic`.
- Produces: alarm `isol8-${env}-W64-provision-gate-block-spike`.

- [ ] **Step 1: Add the alarm via the createAlarm helper**

A provider outage that silently gates all new chats shows as a spike in blocks. Use `createAlarm` (Isol8 namespace default). A spike threshold (not >0 — some blocks are normal, e.g. a genuinely-unsubscribed user):
```typescript
    this.createAlarm({
      id: "W64",
      name: "provision-gate-block-spike",
      metricName: "provision.gate.blocked",
      threshold: 10,
      evaluationPeriods: 1,
      periodMinutes: 15,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      description:
        "Provision gate blocking >10 owners in 15 min — likely a provider outage (Stripe/Metronome/OAuth) silently gating users, not normal unsubscribed traffic. Provisional threshold; sums across all codes.",
    });
```
> This alarm passes NO `dimensions`, so it queries the `{env,service}` stream — which matches the **dimensionless** `provision.gate.blocked` emission Task 4 produces (Task 4 emits both a dimensionless metric for this alarm and a `{code}` metric for the breakdown). No `SEARCH`/math needed. Do NOT add a `code` dimension here, or it would query a stream that only sees one code's blocks.

- [ ] **Step 2: Verify synth**

Run (from apps/infra, symlink node_modules if needed): `npx tsc --noEmit && npx cdk synth dev/isol8-dev-observability 2>/dev/null | grep -c "W64-provision-gate-block"`
Expected: tsc 0; ≥1.

- [ ] **Step 3: Confirm deploy.yml unchanged**

Run: `npx cdk synth >/dev/null 2>&1 && cd <repo root> && git status --short`
Expected: `deploy.yml` NOT modified (adding an alarm resource doesn't change the deploy graph). Remove any node_modules symlink.

- [ ] **Step 4: Commit**

```bash
git add apps/infra/lib/stacks/observability-stack.ts
git commit -m "feat(observability): W64 provision-gate-block-spike alarm"
```

## Task 6: Paperclip-down signal via `paperclip.upstream.error{kind}` + W63 alarm

> REPLACES the dropped ALB-based W63 from Part A. VERIFIED: PaperclipStack has no ALB target group, so there is no `HealthyHostCount` to alarm on — the Paperclip-down signal is a backend custom metric instead. VERIFIED there is NO single shared httpx client, so the emit is added in TWO call paths. The W63 alarm ships in THIS same Part B PR alongside the emission, so there is no phantom-alarm window.

**Files:**
- Modify: `apps/backend/routers/paperclip_proxy.py` (PRIMARY emit)
- Modify: `apps/backend/core/services/paperclip_admin_client.py` (SECONDARY emit)
- Modify: `apps/infra/lib/stacks/observability-stack.ts` (W63 alarm)
- Test: `apps/backend/tests/unit/routers/test_paperclip_proxy_metric.py` (NEW file)

**Interfaces:**
- Produces: metric `paperclip.upstream.error{kind}` where `kind` is the ONLY dimension and is low-cardinality/bounded (`"unreachable"`, `"5xx"`) — safe. Plus alarm `isol8-${env}-W63-paperclip-down` (warn-tier) on the `{kind:"unreachable"}` stream.

- [ ] **Step 1: Write the failing test for the proxy emit**

Mock `put_metric` and force an `httpx.HTTPError` through the proxy's request path; assert a single `paperclip.upstream.error` emit with `dimensions={"kind":"unreachable"}`.

```python
# apps/backend/tests/unit/routers/test_paperclip_proxy_metric.py
import httpx
import pytest
from unittest.mock import patch
# import the proxy handler / the function that contains the
# `except httpx.HTTPError` block (~line 1259) and drive it so the upstream
# request raises httpx.ConnectError (a subclass of httpx.HTTPError).


@pytest.mark.asyncio
async def test_proxy_upstream_unreachable_emits_metric():
    with patch("routers.paperclip_proxy.put_metric") as mock_put:
        # ... arrange the proxy call so the upstream httpx request raises
        # httpx.ConnectError("down"); execute through the handler ...
        ...
    kinds = [
        c.kwargs.get("dimensions", {}).get("kind")
        for c in mock_put.call_args_list
        if c.args and c.args[0] == "paperclip.upstream.error"
    ]
    assert "unreachable" in kinds
```
> Read the proxy function around line 1259 to wire the test to the real entry point + the existing `_record_outcome(502)` call site before running. The fixed contract: a forced `httpx.HTTPError` → exactly one `paperclip.upstream.error{kind:"unreachable"}` emit.

- [ ] **Step 2: Run it — fails (no emission yet)**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_paperclip_proxy_metric.py -o addopts="" -q`
Expected: FAIL.

- [ ] **Step 3: Add the PRIMARY emit in `paperclip_proxy.py`**

At the `except httpx.HTTPError` handler (~line 1259), alongside the existing `_record_outcome(502)`, emit:
```python
            put_metric("paperclip.upstream.error", dimensions={"kind": "unreachable"})
```
Add `from core.observability.metrics import put_metric` if not already imported. Best-effort — wrap in `try/except` if the surrounding handler is on the request path and a metrics failure must not mask the original error (mirror the gate's best-effort pattern).

- [ ] **Step 4: Add the SECONDARY emit in `paperclip_admin_client.py`**

The four request helpers `_post`/`_get`/`_delete`/`_patch` (~lines 223-303) each `await self._http.<verb>(...)` then raise `PaperclipApiError` on `status_code >= 500`. Add emissions:
- Wrap the `await self._http.<verb>` so that on `httpx.RequestError` it emits `put_metric("paperclip.upstream.error", dimensions={"kind": "unreachable"})` and **re-raises** (do not swallow the error).
- In the existing `status_code >= 500` branch (before `raise PaperclipApiError(...)`), emit `put_metric("paperclip.upstream.error", dimensions={"kind": "5xx"})`.

Add `from core.observability.metrics import put_metric` if not already imported. Keep these best-effort (a metrics failure must not mask the upstream error). Consider a tiny private helper to avoid duplicating the wrap across all four verbs.

- [ ] **Step 5: Add the W63 alarm in `observability-stack.ts`**

In the warn-tier custom-metric group (next to W64), add a W63 alarm on `paperclip.upstream.error{kind:"unreachable"}` Sum over a short window, `NOT_BREACHING`:
```typescript
    this.createAlarm({
      id: "W63",
      name: "paperclip-down",
      metricName: "paperclip.upstream.error",
      dimensionsMap: { kind: "unreachable" },
      threshold: 0,
      evaluationPeriods: 1,
      periodMinutes: 5,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      severity: "warn",
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      description:
        "Paperclip BFF is unreachable from the backend (httpx connect/transport errors) — Teams/agent flows are degraded. Replaces the dropped ALB-HealthyHostCount W63 (PaperclipStack has no ALB target group).",
    });
```
> Confirm the `createAlarm` helper accepts a `dimensionsMap`/`dimensions` field + a `treatMissingData` override; if it does not, hand-build the `cloudwatch.Alarm` mirroring W60/W62 with `dimensionsMap: { kind: "unreachable" }` and `this.warnTopic`. A short window + `> 0` over Sum means any unreachable burst trips it; `NOT_BREACHING` keeps it quiet when Paperclip is healthy (no error emissions).

- [ ] **Step 6: Run backend + synth — passes**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_paperclip_proxy_metric.py -o addopts="" -q` → PASS.
Run (from apps/infra): `npx tsc --noEmit && npx cdk synth dev/isol8-dev-observability 2>/dev/null | grep -c "W63-paperclip-down"` → tsc 0; ≥1. Remove any node_modules symlink.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/routers/paperclip_proxy.py apps/backend/core/services/paperclip_admin_client.py apps/backend/tests/unit/routers/test_paperclip_proxy_metric.py apps/infra/lib/stacks/observability-stack.ts
git commit -m "feat(observability): paperclip.upstream.error{kind} + W63 paperclip-down alarm"
```

## Task 7: `chat.e2e.latency` late-terminal ceiling guard (protect the W9 p99 SLO)

> No new alarm — this hardens the existing Phase 2 `chat.e2e.latency` emit so a late/stale terminal cannot inflate the p99 and false-fire W9 (p99 > 20s SLO).

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (~line 1373-1376)
- Test: `apps/backend/tests/unit/gateway/test_e2e_latency_ceiling.py` (NEW file)

**Interfaces:**
- VERIFIED current emit (~line 1373-1376): `if primary_started_at is not None:` → `elapsed_ms = max(0.0, (time.monotonic() - primary_started_at) * 1000.0); put_metric("chat.e2e.latency", value=elapsed_ms, unit="Milliseconds")`. It clamps the FLOOR (`max(0.0, …)`) but has NO ceiling. A hung run whose terminal arrives minutes late (or a stale/re-delivered terminal after a reconnect) emits a multi-minute `elapsed_ms` that inflates the p99 and can false-fire W9. ("End never comes" is fine — `primary_started_at` stays set but nothing emits; only a LATE end spikes.)
- Produces: same `chat.e2e.latency` emit, now bounded `0 <= elapsed_ms <= _E2E_LATENCY_CEILING_MS`; optionally a `chat.run.abandoned` counter when over the ceiling.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/gateway/test_e2e_latency_ceiling.py
# Drive the terminal-event branch (state "final") twice: once with a normal
# elapsed (started_at = now - 2s) and once with an over-ceiling elapsed
# (started_at = now - 20min). Assert chat.e2e.latency is emitted in the first
# case and NOT in the second (and that chat.run.abandoned bumps in the second,
# IF that counter is added).
```
> Wire the test to the smallest unit that contains the emit (the terminal-handling method on the pool, or extract the clamp into a tiny pure helper `_clamped_e2e_latency_ms(started_at, now) -> float | None` and unit-test that). The fixed contract: normal elapsed emits `chat.e2e.latency`; over-ceiling elapsed does NOT emit `chat.e2e.latency`.

- [ ] **Step 2: Run it — fails**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_e2e_latency_ceiling.py -o addopts="" -q`
Expected: FAIL (over-ceiling still emits).

- [ ] **Step 3: Add the ceiling guard**

Add a module constant and gate the emit:
```python
# A run longer than this is a hung/stale outlier, not a real latency
# datapoint — emitting it would inflate the chat.e2e.latency p99 and can
# false-fire W9 (p99 > 20s SLO). 10 min is far past any legitimate chat.
_E2E_LATENCY_CEILING_MS = 10 * 60 * 1000  # 10 minutes
```
Then in the emit block (~line 1373):
```python
                if primary_started_at is not None:
                    elapsed_ms = max(0.0, (time.monotonic() - primary_started_at) * 1000.0)
                    if elapsed_ms <= _E2E_LATENCY_CEILING_MS:
                        put_metric("chat.e2e.latency", value=elapsed_ms, unit="Milliseconds")
                    else:
                        # Late/stale terminal — exclude from the SLO p99; count it
                        # separately so the abandonment is still visible.
                        put_metric("chat.run.abandoned")
```
Best-effort, on the same passive event path, never raises (keep within the existing surrounding error handling).

- [ ] **Step 4: Run — passes**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_e2e_latency_ceiling.py -o addopts="" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/gateway/test_e2e_latency_ceiling.py
git commit -m "feat(observability): cap chat.e2e.latency emit at a ceiling so a late terminal can't false-fire W9"
```

---

## Final Verification

- [ ] **Part A infra:** `cd apps/infra && npx tsc --noEmit && npx cdk synth dev/isol8-dev-observability >/dev/null` succeeds; W58 (crash-loop), W59 (EFS multi-task) + the MultiTaskFn Lambda, W60/W61/W62 (Aurora + Paperclip-CPU) + the AuroraFailoverRule all synthesize (NO W57; NO ALB-based W63 — W63 ships in Part B); `deploy.yml` regenerated + committed (Task 3); prod synth still has the observability stack.
- [ ] **Part B backend:** `cd apps/backend && uv run pytest tests/unit/services/test_provision_gate_metric.py tests/unit/services/test_provision_gate.py tests/unit/services/test_provision_gate_credits.py tests/unit/routers/test_paperclip_proxy_metric.py tests/unit/gateway/test_e2e_latency_ceiling.py -o addopts="" -q` PASS; `uv run ruff check core/services/provision_gate.py core/gateway/connection_pool.py routers/paperclip_proxy.py core/services/paperclip_admin_client.py tests/unit/services/test_provision_gate_metric.py tests/unit/routers/test_paperclip_proxy_metric.py tests/unit/gateway/test_e2e_latency_ceiling.py` clean.
- [ ] **Part B infra:** W64 (provision-gate spike) + W63 (paperclip-down) synthesize; `deploy.yml` unchanged (alarm-only, no stack-dependency change).
- [ ] **Both:** every new alarm is `NOT_BREACHING`; no high-cardinality dims; the only metric dims are the bounded `code` (provision gate) and `kind` (paperclip upstream error).
- [ ] **Findings doc:** append a 3B section to `docs/superpowers/research/2026-06-17-observability-audit.md` recording which gaps are now covered (and the accepted gaps: no standalone per-user-CPU alarm; instance-level Aurora failures not caught by the cluster-id event filter) + the provisional thresholds to baseline.
