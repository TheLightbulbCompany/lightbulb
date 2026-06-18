# Observability Phase 3B — Coverage Alarms Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add CloudWatch alarms for the four uncovered chaos failure modes (per-user CPU saturation, per-user crash-loop, EFS >1-task-per-AP, Aurora/Paperclip outage) plus a provision-gate-block signal — using native AWS metrics + the existing free Lambda, no Container Insights, no new managed cost.

**Architecture:** Two PRs. **Part A (infra)** edits `observability-stack.ts` (+ one cross-stack prop in `isol8-stage.ts`, + extend the inline `TaskStoppedFn` Lambda). **Part B (backend)** adds a metric emission in `provision_gate.py` and its alarm. Per-user services are dynamic (`openclaw-{user_id}-{hash}`), so per-user CPU uses a CloudWatch `SEARCH()` metric-math alarm (not a static per-service alarm).

**Tech Stack:** AWS CDK (TypeScript, aws-cdk-lib 2.190.0), CloudWatch (Metric, MathExpression, Alarm), EventBridge, inline Python Lambda (boto3), FastAPI backend (Python), pytest.

**Spec:** `docs/superpowers/specs/2026-06-17-observability-phase3b-coverage-alarms-design.md`

## Global Constraints

- **No Container Insights** — it stays `false` (`container-stack.ts:92`); use native `AWS/ECS`/`AWS/RDS` metrics + the existing free Lambda only.
- **All new alarms `treatMissingData: NOT_BREACHING`** (absent metric = healthy) — never breach-on-missing (Phase 1 lesson).
- **No high-cardinality metric dimensions** — `_DENIED_DIMENSIONS` in `metrics.py` forbids `user_id`/`owner_id`/`container_id`/`request_id`/`admin_user_id`/`tool_id`; the gate `code` set is bounded (safe).
- **deploy.yml drift (Part A only):** adding cross-stack props to `ObservabilityStackProps` changes the generated `.github/workflows/deploy.yml` `needs:` graph. After `cdk synth`, COMMIT the regenerated `deploy.yml` — `cdk-pipelines-github` (`pipeline.ts:469`) has a synth-time drift check that fails the build and blocks ALL dev+prod deploys if it's stale. NEVER revert the regen.
- **Branch:** `chore/observability-phase3b`. Commit per task. Never push without approval.
- All new alarm SNS actions: page-tier → `this.pageTopic`, warn-tier → `this.warnTopic` (mirror existing alarms). Page alarms append their ARN to `this.pageAlarmArns` only via the `createAlarm` helper; hand-built alarms that should page must `this.pageAlarmArns.push(alarm.alarmArn)` explicitly IF they belong in the FIS page set — but a per-user-fleet CPU/crash-loop/multi-task alarm must NOT go in `pageAlarmArns` if it could self-trip a chaos scenario (these are warn-tier anyway).

---

# PART A — Infra alarms (PR 1: `feat(observability): Phase 3B-infra — coverage alarms`)

## Task 1: Per-user container CPU saturation alarm (W57, SEARCH metric-math)

**Files:**
- Modify: `apps/infra/lib/stacks/observability-stack.ts` (in `createWarnAwsNativeAlarms`, after the W36/W37 backend-ECS block ~line 1110)

**Interfaces:**
- Consumes: `props.cluster.clusterName` (already on `ObservabilityStackProps`), `this.warnTopic`.
- Produces: alarm `isol8-${env}-W57-per-user-container-cpu`.

- [ ] **Step 1: Add the SEARCH metric-math CPU alarm**

Per-user services are dynamic, so use a `SEARCH` expression that matches all `openclaw-` services' `AWS/ECS CPUUtilization` in the per-user cluster and takes the MAX. Insert after W37:

```typescript
    // W57: per-user container CPU saturation (dynamic fleet → SEARCH math).
    // Per-user services are named openclaw-{user_id}-{hash} and created at
    // runtime, so a static per-service alarm is impossible. SEARCH matches every
    // AWS/ECS CPUUtilization series whose ServiceName contains "openclaw" in this
    // cluster; MAX fires if ANY one per-user container is CPU-saturated. The
    // free-text "openclaw" token scopes to per-user services and excludes the
    // backend service (whose name has no "openclaw"). Average stat per service.
    const perUserCpuSearch = new cloudwatch.MathExpression({
      expression: `MAX(SEARCH('{AWS/ECS,ClusterName,ServiceName} MetricName="CPUUtilization" ClusterName="${clusterName}" openclaw', 'Average', 300))`,
      label: "Max per-user container CPU %",
      period: cdk.Duration.minutes(5),
    });
    const w57 = new cloudwatch.Alarm(this, "W57", {
      alarmName: `isol8-${this.envName}-W57-per-user-container-cpu`,
      alarmDescription:
        "A per-user OpenClaw container is CPU-saturated (>90% for 15 min) — MCP/agent storm risk; the 0.5-vCPU box is starving and may healthcheck-SIGKILL. Provisional threshold, baseline after observation.",
      metric: perUserCpuSearch,
      threshold: 90,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w57.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));
```

- [ ] **Step 2: Verify it synthesizes + the SEARCH expression is well-formed**

From `apps/infra` (symlink parent node_modules if absent: `ln -s /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/apps/infra/node_modules ./node_modules`, remove after):
Run: `npx tsc --noEmit && npx cdk synth dev/isol8-dev-observability 2>/dev/null | grep -A8 "W57"`
Expected: tsc exit 0; the synthesized template shows a `AWS::CloudWatch::Alarm` for W57 with `Metrics` containing the `SEARCH(...)` expression string and the cluster name interpolated.
> NOTE: SEARCH results can't be fully validated without live metrics. Confirm the expression string in the template references `MetricName="CPUUtilization"`, `ClusterName="isol8-dev-container-..."`, and the `openclaw` token. The MAX-of-empty-set case returns no data → NOT_BREACHING means no false alarm when zero per-user containers exist.

- [ ] **Step 3: Commit**

```bash
git add apps/infra/lib/stacks/observability-stack.ts
git commit -m "feat(observability): W57 per-user container CPU-saturation alarm (SEARCH math)"
```

## Task 2: Per-user crash-loop alarm (W58, windowed count on existing metric)

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

## Task 3: EFS >1-task-per-access-point detector (extend Lambda + alarm)

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
    multiTaskFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ecs:ListTasks", "cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: { StringEquals: { "ecs:cluster": props.cluster.clusterArn } },
      }),
    );
    multiTaskRule.addTarget(new events_targets.LambdaFunction(multiTaskFn));
```
> `cloudwatch:PutMetricData` does not support the `ecs:cluster` condition; if synth/IAM rejects the combined statement, split into two statements (ecs:ListTasks with the cluster condition, cloudwatch:PutMetricData unconditioned on `*` with a `cloudwatch:namespace` condition of `Isol8/ECS`). Verify in Step 2.

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

## Task 4: Aurora + Paperclip outage alarms (cross-stack props + native alarms + deploy.yml regen)

**Files:**
- Modify: `apps/infra/lib/stacks/observability-stack.ts` (props interface + a new `createPaperclipTierAlarms` block)
- Modify: `apps/infra/lib/isol8-stage.ts` (pass the two refs)
- Modify (generated): `.github/workflows/deploy.yml`

**Interfaces:**
- Consumes: `database.paperclipDbCluster` (`rds.IDatabaseCluster`) and `paperclip.service` (`ecs.IBaseService`) from the stage. Both already exist as public properties (confirmed: `database.paperclipDbCluster`, `paperclip.service`).
- Produces: alarms W60 (aurora-cpu), W61 (aurora-connections), W62 (paperclip-service-cpu), W63 (paperclip-no-running-task).

- [ ] **Step 1: Add the two props to `ObservabilityStackProps`**

In the `ObservabilityStackProps` interface (~line 34), add:
```typescript
  paperclipDbCluster: rds.IDatabaseCluster;
  paperclipService: ecs.IBaseService;
```
Add the import if absent: `import * as rds from "aws-cdk-lib/aws-rds";` (ecs is already imported).

- [ ] **Step 2: Thread them from the stage**

In `apps/infra/lib/isol8-stage.ts`, find the `new ObservabilityStack(...)` call (~line 164) and add to its props:
```typescript
        paperclipDbCluster: database.paperclipDbCluster,
        paperclipService: paperclip.service,
```
ObservabilityStack is constructed after both `database` and `paperclip` in the stage (verify ordering; if observability precedes paperclip, move its construction after paperclip — it has no reverse dependency). Add `observability.addDependency(paperclip)` / `addDependency(database)` if the refs don't auto-create the dependency.

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

    // W61: Aurora connections (a failover/outage drops connections to ~0;
    // saturation spikes them). Alarm on a high ceiling — provisional.
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

    // W62 + W63: Paperclip Fargate service. It is singular + CDK-known, so
    // static per-service alarms work (unlike the per-user fleet).
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

    // W63: Paperclip running-task count drops to 0 (service down). The ECS
    // SampleCount of CPUUtilization is a proxy for "task reporting"; use the
    // AWS/ECS service metric. A genuinely-absent service stops reporting →
    // NOT_BREACHING avoids a false page in dev when Paperclip isn't running,
    // but a transition to 0 reporting datapoints over the window is the signal.
    const w63 = new cloudwatch.Alarm(this, "W63", {
      alarmName: `isol8-${this.envName}-W63-paperclip-service-down`,
      alarmDescription: "Paperclip Fargate service reporting no CPU samples for 15 min — service likely down",
      metric: new cloudwatch.Metric({
        namespace: "AWS/ECS",
        metricName: "CPUUtilization",
        statistic: "SampleCount",
        period: cdk.Duration.minutes(5),
        dimensionsMap: { ClusterName: clusterName, ServiceName: pcServiceName },
      }),
      threshold: 0,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    w63.addAlarmAction(new cloudwatch_actions.SnsAction(this.warnTopic));
  }
```
Call it from the constructor next to the other `this.create*Alarms()` calls.

- [ ] **Step 4: Verify synth (incl. cross-stack refs resolve)**

Run: `npx tsc --noEmit && npx cdk synth dev/isol8-dev-observability 2>/dev/null | grep -E "W60-aurora|W61-aurora|W62-paperclip|W63-paperclip" | head`
Expected: tsc 0; all four alarms present; no "cannot find" cross-stack errors.

- [ ] **Step 5: Regenerate + COMMIT deploy.yml (the prod-blocking step)**

Run: `npx cdk synth >/dev/null 2>&1` then from repo root `git status --short`.
Expected: `.github/workflows/deploy.yml` shows as modified (new observability→paperclip/database `needs:` edges). `git add` it. If it does NOT change, note that (means the dependency already existed). Then remove any node_modules symlink so the tree is clean.

- [ ] **Step 6: Commit**

```bash
git add apps/infra/lib/stacks/observability-stack.ts apps/infra/lib/isol8-stage.ts .github/workflows/deploy.yml
git commit -m "feat(observability): W60-W63 Aurora + Paperclip outage alarms (cross-stack props + deploy.yml regen)"
```

---

# PART B — Provision-gate-block signal (PR 2: `feat(observability): Phase 3B-backend — provision-gate-block metric + alarm`)

> Branch off the SAME `chore/observability-phase3b` after Part A, OR a separate branch `chore/observability-phase3b-backend` off main. Emission + alarm ship together (no phantom-alarm window). This PR touches backend + one alarm in observability-stack.ts; that alarm does NOT add a stack dependency, so deploy.yml is unchanged.

## Task 5: Emit `provision.gate.blocked{code}` from the gate

**Files:**
- Modify: `apps/backend/core/services/provision_gate.py`
- Test: `apps/backend/tests/unit/services/test_provision_gate_metric.py` (new)

**Interfaces:**
- Consumes: the `Gate` dataclass `.code` field (bounded: `signup_required`, `payment_past_due`, `subscription_required`, `provider_required`, `credits_required`, `oauth_required`, `byo_provider_required`).
- Produces: TWO emissions whenever `evaluate_provision_gate` returns a non-None Gate — a dimensionless `provision.gate.blocked` (for the W64 total-across-codes alarm) AND a `provision.gate.blocked{code}` (for the per-code dashboard/Insights breakdown). Both are needed because EMF publishes one stream per full dimension-key set, so an alarm querying `{env,service}` only would NOT match a `{code}`-dimensioned stream.

- [ ] **Step 1: Write the failing test**

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
        patch("core.services.provision_gate.is_subscription_active", return_value=False),
    ):
        gate = await provision_gate.evaluate_provision_gate(
            account=account, owner_id="org_x", provider_choice="bedrock_claude",
            token_owner="org_x", owner_role="admin",
        )
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
    with (
        patch("core.services.provision_gate.put_metric") as mock_put,
        patch("core.services.provision_gate.is_subscription_active", return_value=True),
        patch("core.services.provision_gate.is_trial_blocked", return_value=False),
        patch("core.services.provision_gate._has_oauth_tokens", new=AsyncMock(return_value=True)),
    ):
        gate = await provision_gate.evaluate_provision_gate(
            account={"subscription_status": "active"}, owner_id="org_x",
            provider_choice="bedrock_claude", token_owner="org_x", owner_role="admin",
        )
    blocked = [c for c in mock_put.call_args_list if c.args and c.args[0] == "provision.gate.blocked"]
    assert gate is None
    assert blocked == []
```
> Adjust the `evaluate_provision_gate(...)` kwargs + the account shape to the REAL signature (read the function head ~line 156 and `is_subscription_active`/`is_trial_blocked` inputs) before running. The assertion contract (emit once with `{code}` on block, never on allow) is the fixed part.

- [ ] **Step 2: Run it — fails (no emission yet)**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_provision_gate_metric.py -o addopts="" -q`
Expected: FAIL (no `provision.gate.blocked` call).

- [ ] **Step 3: Wrap the gate to emit once at the return boundary**

In `provision_gate.py`: import `put_metric`, and rename the existing function body to a private `_evaluate_provision_gate_inner` (keeping all its `return Gate(...)`/`return None` logic unchanged), then add a thin public wrapper that emits on a non-None result. This is the single-point, all-branches-covered approach (don't instrument each `return Gate`):

```python
from core.observability.metrics import put_metric
# ... existing imports ...

async def evaluate_provision_gate(*args, **kwargs) -> Gate | None:
    gate = await _evaluate_provision_gate_inner(*args, **kwargs)
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
Rename the existing `async def evaluate_provision_gate(...)` to `async def _evaluate_provision_gate_inner(...)` (same signature). Ensure `logger` exists in the module (add `logger = logging.getLogger(__name__)` if not).

- [ ] **Step 4: Run — passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_provision_gate_metric.py -o addopts="" -q`
Expected: PASS. Then regression: `uv run pytest tests/unit/services/test_provision_gate_credits.py -o addopts="" -q` (existing gate tests) → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/provision_gate.py apps/backend/tests/unit/services/test_provision_gate_metric.py
git commit -m "feat(observability): emit provision.gate.blocked{code} on a blocked gate"
```

## Task 6: Provision-gate-block alarm (ships with the emission)

**Files:**
- Modify: `apps/infra/lib/stacks/observability-stack.ts` (warn-tier custom-metric group, near the other Isol8-namespace alarms)

**Interfaces:**
- Consumes: the `provision.gate.blocked` metric (Task 5), `this.warnTopic`.
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
> This alarm passes NO `dimensions`, so it queries the `{env,service}` stream — which matches the **dimensionless** `provision.gate.blocked` emission Task 5 produces (Task 5 emits both a dimensionless metric for this alarm and a `{code}` metric for the breakdown). No `SEARCH`/math needed. Do NOT add a `code` dimension here, or it would query a stream that only sees one code's blocks.

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

---

## Final Verification

- [ ] **Part A infra:** `cd apps/infra && npx tsc --noEmit && npx cdk synth dev/isol8-dev-observability >/dev/null` succeeds; W57–W63 + the MultiTaskFn Lambda all synthesize; `deploy.yml` regenerated + committed (Task 4); prod synth still has the observability stack.
- [ ] **Part B backend:** `cd apps/backend && uv run pytest tests/unit/services/test_provision_gate_metric.py tests/unit/services/test_provision_gate_credits.py -o addopts="" -q` PASS; `uv run ruff check core/services/provision_gate.py tests/unit/services/test_provision_gate_metric.py` clean.
- [ ] **Both:** every new alarm is `NOT_BREACHING`; no high-cardinality dims; `code` dim is the only metric dim and it's bounded.
- [ ] **Findings doc:** append a 3B section to `docs/superpowers/research/2026-06-17-observability-audit.md` recording which gaps are now covered + the provisional thresholds to baseline.
