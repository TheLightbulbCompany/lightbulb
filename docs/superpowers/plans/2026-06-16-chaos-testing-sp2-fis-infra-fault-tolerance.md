# Chaos Testing SP2 — Infra Fault Tolerance (AWS FIS) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Inject real AWS faults against **dev** (container/CPU/DDB/backend/Paperclip/Aurora/billing-egress) and **assert recovery** automatically, via CDK-managed FIS experiment templates + a boto3-driven pytest harness.

**Architecture:** A new CDK `fis-stack.ts` defines one `aws_fis.CfnExperimentTemplate` per scenario (each with a CloudWatch stop-condition + least-privilege role). A pytest harness (`apps/backend/tests/chaos/fis/`, marked `fis`, excluded from CI) establishes steady state by driving a real chat through the dev WS gateway, starts the experiment via boto3, polls a recovery signal, and asserts the SLO. Dev-only, on-demand.

**Tech Stack:** AWS CDK (TypeScript, aws-cdk-lib 2.190.0), AWS FIS, boto3, pytest, `websockets` (Python WS client), Clerk (test-user JWT minting).

**Spec:** `docs/superpowers/specs/2026-06-16-chaos-testing-sp2-fis-infra-fault-tolerance-design.md`

**Conventions:**
- All work on branch `chaos-sp2-fis`. Commit per task. **Never push without approval.**
- **NEVER run against prod.** The harness hard-guards on `ENVIRONMENT=="dev"` + dev account id.
- **Phase A** tasks are locally verifiable (`cdk synth`, unit tests). **Phase B** tasks are verified by an **on-demand dev game-day run** (requires `aws sso login --profile isol8-admin` + the FIS stack deployed to dev) — NOT by local pytest or CI.
- Per project memory: targeted tests only during execution; never the full RAM-heavy suite.

---

## File Structure

```
apps/infra/lib/stacks/fis-stack.ts        # CREATE: FisStack — experiment templates + FIS role + stop-conditions
apps/infra/lib/isol8-stage.ts             # MODIFY: instantiate FisStack (dev only)
apps/infra/lib/stacks/observability-stack.ts # MODIFY (Task 5): export the alarm ARNs FIS stop-conditions reference
apps/backend/core/containers/ecs_manager.py   # MODIFY (Task 3): enableExecuteCommand on per-user service (CPU-stress prereq)
apps/backend/tests/chaos/fis/
  __init__.py                             # CREATE
  conftest.py                             # CREATE: dev-only guard fixture + boto3 fis client + config
  _chat_driver.py                         # CREATE: mint test-user JWT + drive a chat over the dev WS gateway
  _recovery.py                            # CREATE: shared poll-until-recovered helpers
  test_s1_container_kill.py               # CREATE (Task 6)
  test_s2_cpu_stress.py                   # CREATE (Task 7)
  test_s5_paperclip_kill.py               # CREATE (Task 8)
  test_s6_aurora_failover.py              # CREATE (Task 9)
  test_s7_backend_paperclip_partition.py  # CREATE (Task 10)
  test_s3_ddb_throttle.py                 # CREATE (Task 11)
  test_s4_backend_kill.py                 # CREATE (Task 12)
  test_s8_billing_egress.py               # CREATE (Task 13)
docs/runbooks/sp2-fis-gameday.md          # CREATE (Task 14): how to run a game-day safely
```

---

## PHASE A — Framework (locally verifiable)

## Task 1: FIS stack scaffold + least-privilege role + first template (container kill)

**Files:**
- Create: `apps/infra/lib/stacks/fis-stack.ts`

- [ ] **Step 1: Write the FisStack with the FIS role + the container-kill template**

`aws_fis.CfnExperimentTemplate` is L1 (aws-cdk-lib/aws-fis). Create `apps/infra/lib/stacks/fis-stack.ts`:
```typescript
import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as fis from "aws-cdk-lib/aws-fis";
import { Construct } from "constructs";

export interface FisStackProps extends cdk.StackProps {
  env: string;                 // "dev" — guarded at the stage level
  clusterArn: string;          // ECS cluster running per-user + backend services
  pageAlarmArns: string[];     // stop-condition alarms from ObservabilityStack
}

export class FisStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: FisStackProps) {
    super(scope, id, props);

    // FIS execution role — least privilege. Start with the ECS-stop-task action
    // surface; later tasks widen it per added template.
    const fisRole = new iam.Role(this, "FisExecutionRole", {
      assumedBy: new iam.ServicePrincipal("fis.amazonaws.com"),
      description: `isol8-${props.env} FIS experiment execution role (dev only)`,
    });
    fisRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ecs:DescribeTasks", "ecs:ListTasks", "ecs:StopTask"],
        resources: ["*"], // scoped by experiment target selection; tighten with cluster ARN condition
        conditions: { StringEquals: { "ecs:cluster": props.clusterArn } },
      }),
    );

    const stopConditions: fis.CfnExperimentTemplate.ExperimentTemplateStopConditionProperty[] =
      props.pageAlarmArns.map((arn) => ({ source: "aws:cloudwatch:alarm", value: arn }));

    // Scenario 1 — kill one per-user container task mid-chat.
    new fis.CfnExperimentTemplate(this, "S1ContainerKill", {
      description: "SP2-S1: stop one per-user OpenClaw task to test gateway reconnect",
      roleArn: fisRole.roleArn,
      stopConditions: stopConditions.length ? stopConditions : [{ source: "none" }],
      tags: { Name: `isol8-${props.env}-fis-s1-container-kill`, "isol8:chaos": "sp2" },
      targets: {
        userTask: {
          resourceType: "aws:ecs:task",
          selectionMode: "COUNT(1)",
          // Resource tags select per-user tasks; the harness passes the exact
          // task via the experiment's target resolution at start time.
          resourceTags: { "isol8:owner": "CHAOS_TEST_USER" },
        },
      },
      actions: {
        stopTask: {
          actionId: "aws:ecs:stop-task",
          targets: { Tasks: "userTask" },
        },
      },
    });
  }
}
```

- [ ] **Step 2: Verify it synthesizes**

Run: `cd apps/infra && npx cdk synth isol8-dev/isol8-dev-fis 2>&1 | head -40`
Expected: synthesizes a `AWS::FIS::ExperimentTemplate` resource with no errors. (If the stage isn't wired yet, this errors with "no stack" — proceed to Task 2 then re-synth.)

- [ ] **Step 3: Commit**

```bash
git add apps/infra/lib/stacks/fis-stack.ts
git commit -m "feat(infra): FIS stack scaffold + container-kill experiment template (dev)"
```

## Task 2: Wire FisStack into the dev stage only

**Files:**
- Modify: `apps/infra/lib/isol8-stage.ts`

- [ ] **Step 1: Instantiate FisStack guarded to dev**

In `apps/infra/lib/isol8-stage.ts`, after the existing stack instantiations, add (mirroring the `new XStack(this, \`isol8-${env}-...\`)` pattern; pull `clusterArn` from the container stack and `pageAlarmArns` from observability — Task 5 exports them):
```typescript
import { FisStack } from "./stacks/fis-stack";
// ... inside the constructor, after observability + container are constructed:
if (env === "dev") {
  new FisStack(this, `isol8-${env}-fis`, {
    env,
    env: props.env, // cdk.StackProps.env (account/region) — rename local to avoid clash; see note
    clusterArn: container.clusterArn,
    pageAlarmArns: observability.pageAlarmArns,
  });
}
```
> NOTE: the local `env` (string) clashes with `StackProps.env` (account/region). Use the existing stage's convention — pass the string as a distinct prop name (`envName`) if the codebase already does so; check how ContainerStack receives it and match. Adjust `FisStackProps` to use `envName` if that's the house style.

- [ ] **Step 2: Verify dev synth includes FIS, prod does not**

Run: `cd apps/infra && npx cdk synth isol8-dev/isol8-dev-fis >/dev/null && echo "dev FIS ok" && (npx cdk synth isol8-prod/isol8-prod-fis 2>&1 | grep -q "No stack" && echo "prod has no FIS (correct)")`
Expected: `dev FIS ok` + `prod has no FIS (correct)`.

- [ ] **Step 3: Commit**

```bash
git add apps/infra/lib/isol8-stage.ts apps/infra/lib/stacks/fis-stack.ts
git commit -m "feat(infra): wire FisStack into dev stage only"
```

## Task 3: Enable ECS Exec on per-user tasks (CPU-stress prerequisite)

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py` (the per-user `create_service` call, ~line 640)

- [ ] **Step 1: Read the create_service call to find the exact kwargs**

Run: `sed -n '630,700p' apps/backend/core/containers/ecs_manager.py`
Identify the `self._ecs.create_service(...)` call and its kwargs dict.

- [ ] **Step 2: Add enableExecuteCommand=True**

In the `create_service` kwargs, add `enableExecuteCommand=True`. Also confirm the per-user task ROLE allows SSM messages — add to the task role (in `container-stack.ts` task role) the managed actions:
```
ssmmessages:CreateControlChannel, ssmmessages:CreateDataChannel,
ssmmessages:OpenControlChannel, ssmmessages:OpenDataChannel
```
(Task-level FIS actions inject via the SSM agent; without this, `aws:ecs:task-cpu-stress` cannot attach.)

- [ ] **Step 3: Unit test the kwarg is set**

Add to an existing ecs_manager test (or a new `tests/unit/containers/test_ecs_exec_enabled.py`): assert the `create_service` mock was called with `enableExecuteCommand=True`. Run that single test:
Run: `cd apps/backend && uv run pytest tests/unit/containers/test_ecs_exec_enabled.py -o addopts="" -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/core/containers/ecs_manager.py apps/infra/lib/stacks/container-stack.ts apps/backend/tests/unit/containers/test_ecs_exec_enabled.py
git commit -m "feat(containers): enableExecuteCommand on per-user tasks (FIS CPU-stress prereq)"
```

## Task 4: Harness conftest — dev-only safety guard + FIS client

**Files:**
- Create: `apps/backend/tests/chaos/fis/__init__.py`, `apps/backend/tests/chaos/fis/conftest.py`

- [ ] **Step 1: Create the package marker** — empty `__init__.py`.

- [ ] **Step 2: Write the dev-only guard + fis client fixtures**

Create `apps/backend/tests/chaos/fis/conftest.py`:
```python
"""SP2 FIS harness fixtures. DEV ONLY — hard guard refuses any other env."""

import os

import boto3
import pytest

DEV_ACCOUNT_ID = "877352799272"  # isol8 AWS account (CLAUDE.md). Dev + prod share it,
# so we ALSO require ENVIRONMENT=dev + an explicit opt-in flag below.


@pytest.fixture(scope="session", autouse=True)
def _dev_only_guard():
    """Refuse to run unless explicitly targeting dev. Three gates:
    ENVIRONMENT=dev, the caller-set CHAOS_FIS_CONFIRM=dev, and the dev account."""
    if os.environ.get("ENVIRONMENT") != "dev":
        pytest.skip("SP2 FIS harness runs only with ENVIRONMENT=dev")
    if os.environ.get("CHAOS_FIS_CONFIRM") != "dev":
        pytest.skip("set CHAOS_FIS_CONFIRM=dev to confirm a dev game-day run")
    ident = boto3.client("sts").get_caller_identity()
    assert ident["Account"] == DEV_ACCOUNT_ID, f"refusing: account {ident['Account']} != dev"


@pytest.fixture(scope="session")
def fis_client():
    return boto3.client("fis", region_name="us-east-1")


@pytest.fixture(scope="session")
def ecs_client():
    return boto3.client("ecs", region_name="us-east-1")
```

- [ ] **Step 3: Verify the guard skips cleanly with no env set**

Run: `cd apps/backend && uv run pytest tests/chaos/fis/ -o addopts="" -q`
Expected: `no tests ran` or all skipped (guard trips) — NO error, NO AWS call.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/tests/chaos/fis/__init__.py apps/backend/tests/chaos/fis/conftest.py
git commit -m "test(chaos): SP2 FIS harness conftest + hard dev-only guard"
```

## Task 5: Export observability alarm ARNs for stop-conditions

**Files:**
- Modify: `apps/infra/lib/stacks/observability-stack.ts`

- [ ] **Step 1: Expose the page-alarm ARNs**

In `observability-stack.ts`, collect the alarms created by `createPageAlarms` into a public readonly array:
```typescript
public readonly pageAlarmArns: string[] = [];
// in createAlarm(), after `const alarm = new cloudwatch.Alarm(...)`:
this.pageAlarmArns.push(alarm.alarmArn);
```
(If `createAlarm` is also used for warn/cost alarms you don't want as stop-conditions, push only from `createPageAlarms`, or filter by a `def.stopCondition === true` flag on `AlarmDef`.)

- [ ] **Step 2: Verify synth still works + ARNs flow to FIS**

Run: `cd apps/infra && npx cdk synth isol8-dev/isol8-dev-fis 2>&1 | grep -A3 stopConditions | head`
Expected: the FIS template's `stopConditions` list references `aws:cloudwatch:alarm` ARNs (not `none`).

- [ ] **Step 3: Commit**

```bash
git add apps/infra/lib/stacks/observability-stack.ts
git commit -m "feat(infra): export page-alarm ARNs for FIS stop-conditions"
```

## Task 5b: Chat driver + recovery helpers

**Files:**
- Create: `apps/backend/tests/chaos/fis/_chat_driver.py`, `apps/backend/tests/chaos/fis/_recovery.py`

- [ ] **Step 1: Write the chat driver**

Mints a Clerk JWT for the dedicated chaos test user and drives one chat turn over the dev WS gateway, returning whether a streamed response completed. Create `apps/backend/tests/chaos/fis/_chat_driver.py`:
```python
"""Drive a real chat against the dev WS gateway as the chaos test user."""

import json
import os

import httpx
import websockets

WS_URL = "wss://ws-dev.isol8.co"
CHAOS_USER_ID = os.environ["CHAOS_FIS_USER_ID"]  # dedicated dev test user (org or user id)


async def mint_test_token() -> str:
    """Mint a session token for the chaos test user via Clerk's API.
    CLERK_SECRET_KEY (dev) must be set. Uses the sign-in-token / session flow."""
    secret = os.environ["CLERK_SECRET_KEY"]
    async with httpx.AsyncClient(base_url="https://api.clerk.com/v1", timeout=30) as c:
        r = await c.post(
            "/sign_in_tokens",
            headers={"Authorization": f"Bearer {secret}"},
            json={"user_id": CHAOS_USER_ID},
        )
        r.raise_for_status()
        return r.json()["token"]


async def drive_one_chat_turn(token: str, agent_id: str, *, timeout_s: float = 60.0) -> bool:
    """Send one message, return True if a 'done' frame arrives before timeout."""
    async with websockets.connect(f"{WS_URL}?token={token}", open_timeout=20) as ws:
        await ws.send(json.dumps({"type": "agent_chat", "agent_id": agent_id, "message": "ping: chaos steady-state check"}))
        import asyncio

        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
            evt = json.loads(raw)
            if evt.get("type") == "done":
                return True
            if evt.get("type") == "error":
                return False
        return False
```
> Verify the exact outbound frame shape against `routers/websocket_chat.py` + `useAgentChat.ts` (`type: "agent_chat"`, `agent_id`, `message`) and the inbound `done`/`error` frames against `connection_pool._transform_agent_event` before relying on it.

- [ ] **Step 2: Write recovery poller**

Create `apps/backend/tests/chaos/fis/_recovery.py`:
```python
"""Poll-until-recovered helpers shared by scenarios."""

import asyncio
import time
from typing import Awaitable, Callable


async def poll_until(predicate: Callable[[], Awaitable[bool]], *, timeout_s: float, interval_s: float = 3.0) -> float:
    """Return seconds-to-true, or raise TimeoutError. predicate is async."""
    start = time.monotonic()
    deadline = start + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return time.monotonic() - start
        await asyncio.sleep(interval_s)
    raise TimeoutError(f"did not recover within {timeout_s}s")


def wait_experiment_terminal(fis_client, experiment_id: str, *, timeout_s: float = 300.0) -> str:
    """Block until the FIS experiment reaches a terminal state; return that state."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = fis_client.get_experiment(id=experiment_id)["experiment"]["state"]["status"]
        if state in ("completed", "stopped", "failed"):
            return state
        time.sleep(5)
    raise TimeoutError(f"experiment {experiment_id} not terminal within {timeout_s}s")
```

- [ ] **Step 3: Verify both import (no AWS/network at import time)**

Run: `cd apps/backend && CHAOS_FIS_USER_ID=x uv run python -c "from tests.chaos.fis import _chat_driver, _recovery; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/tests/chaos/fis/_chat_driver.py apps/backend/tests/chaos/fis/_recovery.py
git commit -m "test(chaos): SP2 chat driver + recovery poll helpers"
```

---

## PHASE B — Scenario harnesses (verified by on-demand dev game-day)

> Every Phase B test starts with `@pytest.mark.chaos` + `@pytest.mark.fis`. Verification is a **manual dev game-day**: `aws sso login --profile isol8-admin`, deploy the FIS stack to dev, set `ENVIRONMENT=dev CHAOS_FIS_CONFIRM=dev CHAOS_FIS_USER_ID=<test user> CLERK_SECRET_KEY=<dev>`, then run the single test. NOT in CI.

## Task 6: Scenario 1 — container kill mid-chat

**Files:** Create `apps/backend/tests/chaos/fis/test_s1_container_kill.py`

- [ ] **Step 1: Write the scenario**

```python
"""S1: kill a per-user container mid-chat → assert gateway reconnect + chat resumes."""

import os

import pytest

from tests.chaos.fis._chat_driver import drive_one_chat_turn, mint_test_token
from tests.chaos.fis._recovery import poll_until

S1_TEMPLATE_TAG = "isol8-dev-fis-s1-container-kill"


@pytest.mark.chaos
@pytest.mark.fis
@pytest.mark.asyncio
async def test_container_kill_recovers(fis_client, _dev_only_guard):
    agent_id = os.environ["CHAOS_FIS_AGENT_ID"]
    token = await mint_test_token()

    # 1. steady state
    assert await drive_one_chat_turn(token, agent_id), "chat not healthy before fault"

    # 2. inject — start the S1 experiment template (resolve id by tag)
    template_id = _resolve_template_id(fis_client, S1_TEMPLATE_TAG)
    exp = fis_client.start_experiment(experimentTemplateId=template_id)
    assert exp["experiment"]["state"]["status"] in ("initiating", "running")

    # 3. assert recovery: a fresh chat turn succeeds within the SLO
    async def _chat_ok():
        try:
            return await drive_one_chat_turn(await mint_test_token(), agent_id, timeout_s=30)
        except Exception:
            return False

    seconds = await poll_until(_chat_ok, timeout_s=120, interval_s=5)
    assert seconds <= 120, f"chat did not recover within SLO (took {seconds:.0f}s)"


def _resolve_template_id(fis_client, name_tag: str) -> str:
    for t in fis_client.list_experiment_templates()["experimentTemplates"]:
        if t.get("tags", {}).get("Name") == name_tag:
            return t["id"]
    raise AssertionError(f"no FIS template tagged Name={name_tag}; deploy the FIS stack to dev")
```

- [ ] **Step 2: Game-day verification (manual, dev)**

Run (after deploying FIS stack to dev + env set):
`cd apps/backend && ENVIRONMENT=dev CHAOS_FIS_CONFIRM=dev CHAOS_FIS_USER_ID=... CHAOS_FIS_AGENT_ID=... uv run pytest tests/chaos/fis/test_s1_container_kill.py -o addopts="" -q`
Expected: PASS (chat recovers ≤120s). If it FAILS → a real resilience finding: the gateway pool didn't re-discover/reconnect. Log it in the findings doc + file a fix.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/tests/chaos/fis/test_s1_container_kill.py
git commit -m "test(chaos): SP2-S1 container-kill recovery scenario"
```

## Task 7: Scenario 2 — CPU-stress crash-loop

**Files:** Add the S2 template to `fis-stack.ts` (action `aws:ecs:task-cpu-stress`, target the per-user task, `duration` ~PT5M); create `test_s2_cpu_stress.py`.

- [ ] **Step 1: Add the S2 template** — in `fis-stack.ts`, a new `CfnExperimentTemplate` with `actionId: "aws:ecs:task-cpu-stress"`, parameters `{ duration: "PT5M", percent: "100" }`, target `aws:ecs:task` COUNT(1) by the chaos owner tag. Widen the FIS role with `ssm:*` messages + `ecs:ExecuteCommand` as required by the cpu-stress action. `cdk synth` to verify.
- [ ] **Step 2: Write the scenario** — steady state → start S2 → assert the container is NOT replaced (task ARN stable, no SIGKILL-restart loop) during stress AND chat recovers after. Assert via `ecs describe_tasks` task count + `stoppedReason` absence.
```python
@pytest.mark.chaos
@pytest.mark.fis
@pytest.mark.asyncio
async def test_cpu_stress_no_healthcheck_killloop(fis_client, ecs_client, _dev_only_guard):
    # ... start S2 template; during the 5min stress, poll ecs describe_tasks for the
    # chaos user's service and assert the running task ARN does NOT change (no SIGKILL
    # restart). After stress lifts, assert drive_one_chat_turn() succeeds.
    ...
```
- [ ] **Step 3: Game-day verification (dev)** — run the single test; a task-ARN change during stress = the crash-loop reproduced = finding (healthcheck still too tight). Document.
- [ ] **Step 4: Commit** — `test(chaos): SP2-S2 CPU-stress crash-loop scenario`.

## Task 8: Scenario 5 — Paperclip Fargate kill

**Files:** Add S5 template (`aws:ecs:stop-task` targeting the Paperclip service task) to `fis-stack.ts` (widen role for the Paperclip cluster/service); create `test_s5_paperclip_kill.py`.

- [ ] **Step 1: Add S5 template** — target `aws:ecs:task` selecting the Paperclip service task (by service/tag); `cdk synth`.
- [ ] **Step 2: Write scenario** — steady state = a `/teams` BFF call + a Friday heartbeat succeed; inject; assert the Teams BFF recovers (a `/teams` agents-list call succeeds again within SLO) after Fargate restarts. Use the backend's authenticated `/teams/*` surface via the chat-driver token.
- [ ] **Step 3: Game-day verification (dev).**
- [ ] **Step 4: Commit** — `test(chaos): SP2-S5 Paperclip Fargate-kill scenario`.

## Task 9: Scenario 6 — Aurora failover

**Files:** Add S6 template (`aws:rds:failover-db-cluster` targeting the dev Paperclip Aurora cluster) to `fis-stack.ts` (role: `rds:FailoverDBCluster`, `rds:DescribeDBClusters`); create `test_s6_aurora_failover.py`.

- [ ] **Step 1: Add S6 template** — target `aws:rds:cluster` by the dev Paperclip cluster ARN; `cdk synth`.
- [ ] **Step 2: Write scenario** — steady state = provisioning + a `/teams` read; inject failover; assert provisioning + `/teams` recover after the writer flips (reconnect to new writer) within SLO. Assert recovery, not zero-disruption (failover is disruptive by design).
- [ ] **Step 3: Game-day verification (dev).**
- [ ] **Step 4: Commit** — `test(chaos): SP2-S6 Aurora-failover scenario`.

## Task 10: Scenario 7 — backend↔Paperclip network partition

**Files:** Add S7 template (`aws:network:disrupt-connectivity` scoped to the backend↔Paperclip path) to `fis-stack.ts`; create `test_s7_backend_paperclip_partition.py`.

- [ ] **Step 1: Add S7 template** — `aws:network:disrupt-connectivity` with a target scoping connectivity from the backend subnets to the Paperclip ALB/Cloud Map; `duration: PT3M`; `cdk synth`. Confirm the action's scope parameter matches the dev VPC topology.
- [ ] **Step 2: Write scenario** — steady state = a Friday heartbeat / `/teams` event flow; inject partition; assert the WS-401/heartbeat relay recovers after the partition heals (the documented heartbeat path). Document if the synthetic-challenge workaround is or isn't needed.
- [ ] **Step 3: Game-day verification (dev).**
- [ ] **Step 4: Commit** — `test(chaos): SP2-S7 backend↔Paperclip partition scenario`.

## Task 11: Scenario 3 — DynamoDB throttle/unavailable

**Files:** Add S3 template (`aws:fis:inject-api-throttle-error` scoped to dynamodb for the backend role, OR route disruption to the dynamodb prefix) to `fis-stack.ts`; create `test_s3_ddb_throttle.py`.

- [ ] **Step 1: Add S3 template** — prefer `aws:fis:inject-api-throttle-error` with `service: dynamodb` + the backend task role as target; `duration: PT3M`; `cdk synth`. (Fallback: `aws:network:route-table-disrupt-connectivity` scope `dynamodb`.)
- [ ] **Step 2: Write scenario** — steady state; inject throttle; during the window assert the backend returns clean errors / retries (no 5xx storm — poll a representative authed endpoint and assert status ∈ {200, 429, 503}, never 500); after, assert full recovery.
- [ ] **Step 3: Game-day verification (dev) — SHARED-INFRA, quiet window only.**
- [ ] **Step 4: Commit** — `test(chaos): SP2-S3 DynamoDB-throttle scenario`.

## Task 12: Scenario 4 — backend kill / gateway drop

**Files:** Add S4 template (`aws:ecs:stop-task` targeting the backend service task) to `fis-stack.ts`; create `test_s4_backend_kill.py`.

- [ ] **Step 1: Add S4 template** — target the backend service task COUNT(1); `cdk synth`.
- [ ] **Step 2: Write scenario** — steady state; kill backend task; assert ECS restarts it, ALB target becomes healthy, and a fresh chat/`/health` succeeds within the recovery SLO. Measure recovery seconds.
- [ ] **Step 3: Game-day verification (dev) — SHARED-INFRA (full backend outage), quiet window only.**
- [ ] **Step 4: Commit** — `test(chaos): SP2-S4 backend-kill recovery scenario`.

## Task 13: Scenario 8 — billing provider unreachable

**Files:** Add S8 template (`aws:network:disrupt-connectivity` egress to Stripe/Metronome endpoints from backend subnets) to `fis-stack.ts`; create `test_s8_billing_egress.py`.

- [ ] **Step 1: Add S8 template** — disrupt egress to the Stripe/Metronome endpoints (by prefix list or generic egress for the billing path); `duration: PT3M`; `cdk synth`. Scope tightly so only the billing egress is affected.
- [ ] **Step 2: Write scenario** — steady state; inject egress block; assert the provision gate / billing surface returns a **clean blocked state** (structured "blocked" response, not a 500/wedge) while the provider is unreachable; recover when egress restored. Asserts graceful degradation only — never touches billing state.
- [ ] **Step 3: Game-day verification (dev) — SHARED-INFRA, quiet window only.**
- [ ] **Step 4: Commit** — `test(chaos): SP2-S8 billing-provider-unreachable scenario`.

## Task 14: Game-day runbook + findings doc

**Files:** Create `docs/runbooks/sp2-fis-gameday.md`; create `docs/superpowers/research/2026-06-16-chaos-sp2-findings.md`.

- [ ] **Step 1: Write the runbook** — prerequisites (`aws sso login`, deploy FIS stack to dev, dedicated chaos test user + agent, env vars), the quiet-window discipline for shared-infra scenarios (S3/S4/S6/S7/S8), how to abort (FIS stop-experiment + stop-conditions), and the per-scenario run commands. Explicitly: never prod.
- [ ] **Step 2: Create the findings doc skeleton** — a table of (scenario, run date, recovery seconds vs SLO, pass/fail, finding). Populated on each game-day.
- [ ] **Step 3: Commit** — `docs(chaos): SP2 FIS game-day runbook + findings doc`.

---

## Final verification

- [ ] **Phase A is locally green:** `cd apps/infra && npx cdk synth isol8-dev/isol8-dev-fis` succeeds with all 8 templates; `cd apps/backend && uv run pytest tests/chaos/fis/ -o addopts="" -q` skips cleanly (guard) with no AWS calls; the Task 3 ECS-exec unit test passes; `uv run ruff check tests/chaos/fis/`.
- [ ] **Phase B is exercised on dev** at least once per scenario (game-day), results recorded in the findings doc with recovery-seconds vs SLO. Any SLO miss = a resilience finding with a filed fix.
- [ ] **Prod safety re-confirmed:** `npx cdk synth isol8-prod/isol8-prod-fis` shows no FIS stack; the harness guard skips outside dev.
