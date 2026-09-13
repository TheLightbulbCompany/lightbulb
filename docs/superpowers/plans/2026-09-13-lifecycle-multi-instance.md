# Lifecycle Without a Privileged Instance — Implementation Plan (sub-project C)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Shipped, Task 7 step 1, then a whole-branch PR review round.** Tasks 1–6 are DONE
> (commits `179b13d98` P12, `d8740710e` `run.completed` pin, `c48d610a1` `try_claim_pass`,
> `d54881b00` passes behind admission, `74c7d6084` `connection_lock` deleted, `75e7857f9`
> admin idempotency; SHAs as landed on `arch/lifecycle-multi-instance` after its rebase onto
> `main`). Two implementation-phase review rounds produced follow-up fix commits that
> supersede a couple of this plan's illustrative code snippets — see the "Review round" notes
> in Task 3 and before the Pre-flight seam table. A THIRD round, an Opus whole-branch review
> of the opened PR (#2602), found Task 1's own shipped design still had two problems this
> plan's Task 1 section (below) does not reflect: the `instance` value belongs on a
> **structured log field**, not a metric **dimension** (a new CloudWatch identity per task per
> deploy — the same class `_DENIED_DIMENSIONS` already refuses for `container_id`; the code
> was fixed, not just documented), and the P12 threshold needed **8, not 7**, to still catch
> one dead instance at prod's N = 4 autoscaling ceiling (one dead loop of four reads 7.5).
> Task 1's prose below is LEFT AS ORIGINALLY WRITTEN AND REVIEWED — it describes what Task 1
> shipped as of the first review round, not the corrected final state; read it alongside the
> spec's own "review round" corrections in §8, which are current.

**Goal:** Make the backend's periodic lifecycle work cheap and honest at `desiredCount >= 2` — one pass admission per window instead of N duplicate scans, cadence on a durable row instead of a process clock, the two in-process mutual-exclusion maps replaced by mechanisms that already work across processes, and a P12 liveness alarm that can still see a dead instance when there are two.

**Architecture:** `container_repo.try_claim_pass(name, interval_s)` — ONE conditional write on a reserved-key row of the `containers` table, failing closed — admits at most one instance into each periodic pass per window. The four `run_scheduled_worker` passes plus `sweep_trial_idle_once` and `sweep_runs_once` enter behind it; their bodies and their per-row claims are untouched (the admission is an efficiency device, never the correctness boundary). `core/services/credentials/connection_lock.py` is deleted and both call sites take the existing cross-process `owner_lifecycle_guard(owner_id)`. `core/services/idempotency.py` is deleted and its decorator is re-homed onto the existing `webhook-event-dedup` conditional claim, answering `409` instead of replaying a cached body. `_running_count_gauge_loop` stays deliberately unadmitted (it is the liveness canary) and gains an `instance` dimension; P12 becomes instance-count-independent by dividing its sample count by the target group's `HealthyHostCount`.

**Tech Stack:** Python 3.12 / FastAPI, DynamoDB conditional writes (`boto3` resource API via `core.dynamodb.run_in_thread`), pytest + `pytest-asyncio`, AWS CDK v2 (`aws-cdk-lib/aws-cloudwatch` metric math), Jest for the infra tests.

**Spec:** `.superpowers/sdd/2026-09-13-lifecycle-step-functions/spec.md` (committed at the end of this plan as `docs/superpowers/specs/2026-09-13-lifecycle-multi-instance-design.md` — renamed at commit time, Task 7 step 1, along with this plan; see that file's corrections banner). Controller rulings C1–C6 live in `progress.md` beside it. C1 is IMPLEMENTED, C2 is NOT TAKEN (no state machine; the update-apply heartbeat custody stays exactly as it is), C3/C4/C5 are implemented, C6 is D's.

## Global Constraints

- No backward compatibility, no feature flags, fix-forward. One cutover per environment.
- Call it **pass admission**, never a *lease*, in code, comments, tests and commit messages. It admits a scan; it holds custody of nothing.
- `try_claim_pass` **fails closed** — a DynamoDB fault returns `False` and the tick is skipped. This is the deliberate inverse of `try_claim_daily_system_notice` (`container_repo.py:587`), which fails open. Assert the difference explicitly so nobody "fixes" it.
- Every per-row claim inside every pass (`try_claim_idle_stop`, `messaging_repo.end_run`, `update_repo.claim_stale_apply`, `apply_update`'s own claim) stays untouched. If pass admission is wrong, stale or unreachable, the worst outcome is a missed or duplicated *scan* — which is what every instance does today.
- `openclaw_worker.run` (the SQS consumer) and `_running_count_gauge_loop` (the liveness canary) are the two deliberate exceptions: both keep running on every instance. `_recover_stale_task_definition_refreshes` is a THIRD exception — it is C2's custody recovery, and admitting only one instance per window would lengthen its recovery tail for no gain.
- Metrics never carry owner/user/container ids as dimensions (`core/observability/metrics.py:35` `_DENIED_DIMENSIONS`). The one new dimension value, the ECS task id, is bounded by the running task count and stops billing when a task stops.
- C must touch NO file `arch/machine-relay` touches, except `apps/backend/main.py` and `apps/infra/lib/stacks/observability-stack.ts` (see the collision note in Task 1 and the seam table). Never push, merge, deploy or touch AWS without the owner's explicit approval.

---

## File structure

- `apps/backend/core/repositories/container_repo.py` — `try_claim_pass()` (new, ~35 lines) beside `try_claim_daily_system_notice` (`:587`); `scan_all` (`:85`) gains a one-line filter that hides the reserved key from every fleet consumer at once.
- `apps/backend/core/services/update_service.py` — `run_scheduled_worker` (`:1015`) loses `last_purge_pass_at`/`last_rollup_pass_at` (`:1026`, `:1027`) and its four passes enter behind admission.
- `apps/backend/core/services/durable_activity.py` — `run_trial_idle_sweeper` (`:81`) admits before `sweep_trial_idle_once` (`:33`).
- `apps/backend/core/services/run_ledger.py` — `run_ledger_sweeper` (`:46`) admits before `sweep_runs_once` (`:19`).
- `apps/backend/routers/connections.py` — import (`:30`) and both call sites (`:788`, `:1033`) move to `owner_lifecycle_guard`.
- **Deleted:** `apps/backend/core/services/credentials/connection_lock.py`, `apps/backend/core/services/idempotency.py`.
- `apps/backend/core/services/webhook_dedup.py` — gains `idempotency()` (the re-homed decorator, ~25 lines) next to `record_event_or_skip` (`:713`).
- `apps/backend/routers/admin.py` — one import line (`:48`). The twelve `@idempotency()` decorations (`:271, :290, :306, :334, :377, :412, :506, :555, :591, :609, :624, :640`) are NOT edited.
- `apps/backend/main.py` — `_running_count_gauge_loop` (`:251`) gains the `instance` dimension.
- `apps/infra/lib/stacks/observability-stack.ts` — P12 (`:854`–`:905`) becomes a metric-math ratio.
- `apps/infra/test/observability-stack.test.ts` — emitter registry (`:193`) and the P12 assertion (`:726`).
- `docs/superpowers/specs/2026-09-13-lifecycle-multi-instance-design.md` (new, the spec with its corrections).

---

### Task 1: P12 sees a dead instance among two (C5, gates D's prod flip)

Lands **first and separately** — C5 is the precondition for D's `desiredCount` flip, and it is independently deployable. P12 today alarms on `SampleCount(gateway.running.count) < 5` over ten minutes against `{env, service}`. One instance emits ten samples per window; at N=2 it reads twenty and one dead loop reads ten — arithmetically indistinguishable from a healthy single instance.

**A per-instance ALARM is not expressible and this task does not attempt one.** A CloudWatch alarm binds a fixed `dimensionsMap` at synth time, ECS task ids are unknown then and change on every deploy, and `SEARCH` is rejected outright on metric alarms (the stack already carries that scar — `core/observability/metrics.py:60-66`). The implementable form of "a dead instance is visible" is to divide the fleet sample count by the live instance count, which `AWS/ApplicationELB HealthyHostCount` already publishes for `props.targetGroup` (W28 at `observability-stack.ts:1751` already binds that target group). The `instance` dimension is still added to the gauge — it is what makes the failure *diagnosable* on a dashboard or in Logs Insights once the ratio pages.

**Files:**
- Modify: `apps/backend/main.py:251` (`_running_count_gauge_loop`)
- Modify: `apps/infra/lib/stacks/observability-stack.ts:854` (P12 block, through `:905`)
- Modify: `apps/infra/test/observability-stack.test.ts:193` (registry entry), `:726` (P12 assertion)
- Test: `apps/backend/tests/unit/test_main_lifespan.py:124` (`test_running_count_gauge_loop_emits_running_count`)

**Interfaces:**
- Consumes: `core.observability.metrics.gauge(name: str, value: float, dimensions: dict[str, str] | None) -> None` (`metrics.py:159`); `os.environ["HOSTNAME"]` — the Fargate task id in ECS, already relied on with exactly this comment at `core/services/provisioning_workflow.py:67-68`.
- Produces: `gateway.running.count` emitted with `{"instance": <task id or "local">}`. `put_metric` (`metrics.py:116-124`) publishes the full set `[env, service, instance]` AND, because `instance` is not in `_ROLLUP_DIMENSIONS` (`metrics.py:81`) and the full set differs from `_ROLLUP_KEYS`, the `[env, service]` rollup — so the existing P12 identity keeps receiving exactly the data it does today.
- Produces (infra): alarm `lightbulb-{env}-P12-backend-liveness`, logical id pinned `ReaperHeartbeatAlarm31FA055D`, now `metrics:` (math) rather than `metric:`.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/test_main_lifespan.py — replace the body of
# test_running_count_gauge_loop_emits_running_count (currently :124)
@pytest.mark.asyncio
async def test_running_count_gauge_loop_tags_the_emitting_instance(monkeypatch):
    """P12 cannot tell a dead instance from a halved rate without this dimension."""
    import main
    monkeypatch.setenv("HOSTNAME", "task-abc123")
    seen: list[tuple[str, float, dict | None]] = []
    monkeypatch.setattr(
        "core.observability.metrics.gauge",
        lambda name, value, dimensions=None: seen.append((name, value, dimensions)),
    )
    monkeypatch.setattr(
        main.container_repo, "get_by_status", AsyncMock(return_value=[{"owner_id": "o1"}])
    )
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await main._running_count_gauge_loop()
    assert seen == [("gateway.running.count", 1, {"instance": "task-abc123"})]


@pytest.mark.asyncio
async def test_running_count_gauge_loop_falls_back_off_ecs(monkeypatch):
    """No HOSTNAME (local/pytest) must not crash the canary."""
    import main
    monkeypatch.delenv("HOSTNAME", raising=False)
    seen = []
    monkeypatch.setattr(
        "core.observability.metrics.gauge",
        lambda name, value, dimensions=None: seen.append(dimensions),
    )
    monkeypatch.setattr(main.container_repo, "get_by_status", AsyncMock(return_value=[]))
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await main._running_count_gauge_loop()
    assert seen == [{"instance": "local"}]
```

```ts
// apps/infra/test/observability-stack.test.ts — REPLACE the test at :726
test("P12 divides the heartbeat by the live instance count", () => {
  template.hasResourceProperties("AWS::CloudWatch::Alarm", {
    AlarmName: "lightbulb-dev-P12-backend-liveness",
    ComparisonOperator: "LessThanThreshold",
    Threshold: 7,
    TreatMissingData: "breaching",
    Metrics: Match.arrayWith([
      Match.objectLike({ Id: "beatsPerInstance", Expression: "beats / hosts" }),
      Match.objectLike({
        Id: "beats",
        MetricStat: Match.objectLike({
          Stat: "SampleCount",
          Metric: Match.objectLike({
            Namespace: "LightbulbAI",
            MetricName: "gateway.running.count",
          }),
        }),
      }),
      Match.objectLike({
        Id: "hosts",
        MetricStat: Match.objectLike({
          Stat: "Minimum",
          Metric: Match.objectLike({
            Namespace: "AWS/ApplicationELB",
            MetricName: "HealthyHostCount",
          }),
        }),
      }),
    ]),
  });
});
```

- [ ] **Step 2: Tag the gauge**
  - In `main.py`, inside `_running_count_gauge_loop` (`:251`), read the instance once before the loop: `instance = os.getenv("HOSTNAME") or "local"` (confirm `os` is already imported before adding it).
  - Change the emit to `gauge("gateway.running.count", len(rows), {"instance": instance})`.
  - Extend the docstring: this loop stays UNADMITTED on purpose — it is the liveness canary, and a canary admitted once per window only proves that *some* instance is alive.
- [ ] **Step 3: Make P12 instance-count-independent**
  - In `observability-stack.ts`, keep `reaperDims` (`:863`) and `heartbeatMetric` (`:865`) exactly as they are — the `[env, service]` rollup is still what the numerator reads.
  - Add the denominator beside it:
    `new cloudwatch.Metric({ namespace: "AWS/ApplicationELB", metricName: "HealthyHostCount", statistic: "Minimum", period: cdk.Duration.minutes(10), dimensionsMap: { LoadBalancer: props.alb.loadBalancerFullName, TargetGroup: props.targetGroup.targetGroupFullName } })`.
    `Minimum` (not `Average`) is load-bearing: a rolling deploy runs at `maxHealthyPercent: 200`, so `Average` would count the transient extra tasks in the denominator while their sample windows are only partly filled, and the ratio would dip into a false page.
  - Replace `metric: heartbeatMetric` on the alarm (`:879`) with `metric: new cloudwatch.MathExpression({ expression: "beats / hosts", usingMetrics: { beats: heartbeatMetric, hosts: healthyHosts }, period: cdk.Duration.minutes(10) })` and set `threshold: 7`.
  - Leave `evaluationPeriods: 1`, `comparisonOperator: LESS_THAN_THRESHOLD`, `treatMissingData: BREACHING` and `overrideLogicalId("ReaperHeartbeatAlarm31FA055D")` (`:903`) untouched. Changing the alarm's properties does not change its logical id; changing the logical id would delete the live page-tier alarm (the comment at `:889-902` explains why).
  - Update the alarm description: "PAGE: at least one backend instance has stopped emitting its 60 s heartbeat. Samples per healthy ALB target have fallen below 7 (healthy is ~10)."
  - Add a `ponytail:` comment on the threshold naming the knob: `// ponytail: 7 is the calibrated middle — healthy reads ~10/instance, one dead loop of two reads ~5. Retune here, not by changing the expression, if deploy churn or a period change moves the healthy baseline.`
- [ ] **Step 4: Register the new emitter shape**
  - `apps/infra/test/observability-stack.test.ts:193`: `"gateway.running.count": [[]],` becomes `"gateway.running.count": [["instance"]],` with a comment pointing at `main.py:_running_count_gauge_loop`. The contract test (`:583`) short-circuits alarms whose `extraDims.length === 0` as "always watching the universal rollup" (`:597`), which is exactly what P12 binds.
- [ ] **Step 5: Run the tests**

```bash
cd apps/backend && uv run pytest tests/unit/test_main_lifespan.py -v
cd apps/infra && npx jest test/observability-stack.test.ts
```
Expected: all pass. The infra suite must still report at least 65 alarms (`observability-stack.test.ts:328`) — the alarm count is unchanged, only P12's metric shape.

- [ ] **Step 6: Commit**

```
feat(observability)!: P12 measures heartbeats per live instance

gateway.running.count carries the emitting ECS task id, and P12 divides its
fleet sample count by the target group's HealthyHostCount. At desiredCount 2 a
single dead metrics loop halved the rate and the alarm could not see it; the
ratio is instance-count-independent, so it pages on one dead loop of two.

Not a per-instance alarm: an alarm binds its dimensions at synth time, task ids
are not known then, and SEARCH is rejected on metric alarms. The instance
dimension is for diagnosis; the ratio is the alarm.
```

> **Collision note (D):** this is the one C task that shares files with `arch/machine-relay` — `apps/backend/main.py` (D adds a router import at `:44` and a mount at `:779`; C edits `:251`, no textual overlap) and `apps/infra/lib/stacks/observability-stack.ts` (D edits W78; C edits P12 at `:854-905`, ~900 lines apart). Both merge cleanly today. Because C5 *gates* D's prod flip, this task ships as its own PR merged **before** D, not folded into C's main PR.

---

### Task 2: `run.completed` emits once per run — prove it, change nothing (C5)

The spec (§8) says `run_completed` should "dedupe on the run-ledger row … emitting the metric only on a non-`None` `end_run` result is the whole fix". **The code does not match that description and needs no change.** Verified on this tree: `run.completed` has exactly one emitter (`core/services/run_analytics.py:158`) and exactly two callers — `core/services/operator_proxy.py:101`, which `arch/machine-relay` DELETES, and `core/services/messaging.py:1923` inside `handle_usage_report`, which is already gated by the durable conditional claim `messaging_repo.claim_usage_receipt` (`messaging_repo.py:1597`, `attribute_not_exists(sk)`). Separately, `messaging.run.ended` is already emitted only inside `if row is not None` after `end_run` (`messaging.py:1855-1857`, `run_ledger.py:29-34`). So after D lands there is no cross-instance double-count path, and `_seen_run_ids` (`run_analytics.py:72`) stays exactly where §3 "Accepted at N >= 2" put it.

What is missing is the *evidence*, since C5 gates a prod flip. This task adds the regression test that fails if someone ever removes the durable gate, and corrects the one stale comment.

**Files:**
- Modify: `apps/backend/core/services/messaging.py:1920-1923` (the comment above the `schedule_run_completed` call)
- Test: `apps/backend/tests/unit/services/test_usage_report.py`

**Interfaces:**
- Consumes: `messaging_repo.claim_usage_receipt(owner_id, run_id) -> bool`; `run_analytics.schedule_run_completed(*, owner_id, parsed, payload, phase) -> asyncio.Task | None`.
- Produces: nothing new. Behaviour is unchanged by design.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/services/test_usage_report.py
@pytest.mark.asyncio
async def test_usage_report_emits_run_completed_once_across_instances(monkeypatch):
    """The durable receipt, not the per-process _seen_run_ids cache, is what keeps
    run.completed single-emit at desiredCount >= 2. Replay must not re-emit."""
    calls = []
    monkeypatch.setattr(
        "core.services.messaging.run_analytics.schedule_run_completed",
        lambda **kw: calls.append(kw["payload"]["runId"]),
    )
    # First writer wins; the SECOND call models the other instance.
    claims = iter([True, False])
    monkeypatch.setattr(
        "core.repositories.messaging_repo.claim_usage_receipt",
        AsyncMock(side_effect=lambda *a, **k: next(claims)),
    )
    body = UsageReportRequest(session_key="cron:o1", run_id="r1", success=True, usage=_zero_usage())
    first = await messaging.handle_usage_report("o1", body)
    second = await messaging.handle_usage_report("o1", body)
    assert (first.status, second.status) == ("applied", "replayed")
    assert calls == ["r1"]
```

- [ ] **Step 2: Correct the comment, not the code**
  - Above `run_analytics.schedule_run_completed(` (`messaging.py:1923`) state plainly that `claim_usage_receipt` above it is what makes this single-emit across instances, and that `run_analytics._seen_run_ids` is a per-process accelerator only.
- [ ] **Step 3: Run the tests**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_usage_report.py tests/unit/services/test_run_analytics.py -v
```
Expected: all pass, no production code changed.

- [ ] **Step 4: Commit**

```
test(messaging): pin run.completed to the durable usage receipt

C5 asked for run.completed to be deduped on the run ledger. It already is, one
layer up: claim_usage_receipt is a conditional PutItem and is the only gate the
surviving emitter passes through (the other caller, operator_proxy, goes away
with the machine relay). This test fails if that gate is ever removed.
```

---

### Task 3: `try_claim_pass` — pass admission (C1)

**Files:**
- Modify: `apps/backend/core/repositories/container_repo.py` — add `try_claim_pass` after `try_claim_daily_system_notice` (`:587-620`); add the reserved-key filter to `scan_all` (`:85-103`)
- Test: `apps/backend/tests/unit/repositories/test_container_repo_pass_admission.py` (new)

**Interfaces:**
- Consumes: `core.dynamodb.run_in_thread`, `get_table("containers")` (PK `owner_id`, string, no sort key — `database-stack.ts:119-120`), `botocore.exceptions.ClientError`.
- Produces:
  ```python
  PASS_KEY_PREFIX = "__pass__#"

  async def try_claim_pass(name: str, *, interval_s: int, now: int | None = None) -> bool
  ```
  Returns `True` when this caller owns the window, `False` when another instance already has it **or** when DynamoDB faulted (fail closed).

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/repositories/test_container_repo_pass_admission.py
import pytest
from botocore.exceptions import ClientError
from unittest.mock import MagicMock, patch
from core.repositories import container_repo


def _table(side_effect=None):
    t = MagicMock()
    t.update_item = MagicMock(side_effect=side_effect)
    return t


def _conditional_failure():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


@pytest.mark.asyncio
async def test_first_claimant_wins_the_window():
    with patch.object(container_repo, "_get_table", return_value=_table()):
        assert await container_repo.try_claim_pass("paperclip_purge", interval_s=86_400) is True


@pytest.mark.asyncio
async def test_second_claimant_in_the_same_window_loses():
    with patch.object(container_repo, "_get_table", return_value=_table(_conditional_failure())):
        assert await container_repo.try_claim_pass("paperclip_purge", interval_s=86_400) is False


@pytest.mark.asyncio
async def test_claim_targets_a_reserved_key_that_cannot_collide_with_an_owner():
    table = _table()
    with patch.object(container_repo, "_get_table", return_value=table):
        await container_repo.try_claim_pass("run_ledger", interval_s=60, now=1_700_000_000)
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"owner_id": "__pass__#run_ledger"}
    # cutoff = now - interval_s; the window, not process uptime, decides.
    assert kwargs["ExpressionAttributeValues"][":cutoff"] == 1_700_000_000 - 60
    assert kwargs["ExpressionAttributeValues"][":now"] == 1_700_000_000


@pytest.mark.asyncio
async def test_a_dynamodb_fault_fails_CLOSED():
    """The deliberate inverse of try_claim_daily_system_notice, which fails OPEN.
    A fleet-wide fail-open costs N x every scan exactly when DynamoDB is unhappy."""
    boom = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
    with patch.object(container_repo, "_get_table", return_value=_table(boom)):
        assert await container_repo.try_claim_pass("owner_day_rollup", interval_s=3600) is False


@pytest.mark.asyncio
async def test_scan_all_hides_the_reserved_pass_rows_from_fleet_consumers():
    """update_service.schedule_task_definition_refresh_for_fleet queues an update for
    EVERY row scan_all returns; a pass row must never reach it."""
    table = MagicMock()
    table.scan = MagicMock(return_value={"Items": [{"owner_id": "user_1"}]})
    with patch.object(container_repo, "_get_table", return_value=table):
        rows = await container_repo.scan_all()
    assert rows == [{"owner_id": "user_1"}]
    kwargs = table.scan.call_args.kwargs
    assert "begins_with" in kwargs["FilterExpression"]
    assert container_repo.PASS_KEY_PREFIX in kwargs["ExpressionAttributeValues"].values()
```

- [ ] **Step 2: Implement `try_claim_pass`**
  - Add after `container_repo.py:620`:

```python
PASS_KEY_PREFIX = "__pass__#"


async def try_claim_pass(name: str, *, interval_s: int, now: int | None = None) -> bool:
    """Admit at most one instance into periodic pass ``name`` per ``interval_s`` window.

    ONE conditional write on a fixed-key row: set ``last_run_at`` only when it is
    absent or at/older than ``now - interval_s`` (``<=``, not ``<`` -- with a
    strict less-than, a pass whose interval equals its caller's tick, e.g. the
    60 s worker passes and sweepers, would never see ``elapsed == interval_s``
    admitted and would only fire every other tick). Modelled on
    ``try_claim_daily_system_notice`` and adds no new pattern.

    This is PASS ADMISSION, not a lease. It admits a *scan*; it holds custody of
    nothing. Every per-row claim inside every pass stays the correctness
    boundary, so a wrong, stale or unreachable row degrades to exactly what the
    code did before this existed: a duplicate scan.

    Cadence lives on the row, not in a process frame, so ``interval_s`` -- not
    process uptime -- decides a daily purge and a deploy no longer forces one.

    FAILS CLOSED, unlike ``try_claim_daily_system_notice``: ANY fault (a
    conditional refusal, a ``ClientError``, or a ``BotoCoreError`` -- endpoint/
    connect/read timeouts, ``NoCredentialsError``, none of which carry an
    ``Error`` code so ``ClientError`` alone would miss them) skips this tick
    rather than admitting every instance. A skipped 60 s sweep costs a minute
    of latency on a trial park or a swept run; a fleet-wide fail-open costs
    N x every scan in the product exactly when DynamoDB is already unhappy.
    ``tests/unit/repositories/test_container_repo_pass_admission.py``
    asserts the difference so nobody "fixes" it.

    ponytail: one reserved-key row per pass name, all on the containers table.
    Fine at ~6 writes/min fleet-wide; give it its own table only if that stops
    being true.
    """
    stamp = int(time.time()) if now is None else int(now)
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": f"{PASS_KEY_PREFIX}{name}"},
            UpdateExpression="SET last_run_at = :now",
            ConditionExpression="attribute_not_exists(last_run_at) OR last_run_at <= :cutoff",
            ExpressionAttributeValues={":now": stamp, ":cutoff": stamp - int(interval_s)},
        )
        return True
    except Exception:
        return False  # conditional refusal AND any fault both fail closed -- see docstring
```

> **Review round (corrected here, Task 7 step 1).** The snippet above is the code as it
> shipped, not as this task originally drafted it. The Task 3/4 review (OPUS) found the
> draft's `<` admitted a same-interval pass only every OTHER tick (~120 s at N=1, ~82 s at
> N=2 instead of 60 s) and its `except ClientError` missed `BotoCoreError`. Both were fixed
> in one commit, `fix(lifecycle): pass admission admits at exactly interval_s and fails
> closed on every boto error` (`4a97aadec` on this branch post-rebase), alongside a new
> moto-backed boundary test (`t+59` refused, `t+60` admitted) and two test-suite stubbing
> fixes it surfaced (`test_update_service_graceful_stop.py` now stubs
> `_recover_stale_task_definition_refreshes`; `tests/chaos/api/conftest.py` now stubs
> `run_ledger_sweeper` alongside the other two background loops).

- [ ] **Step 3: Hide the reserved rows from every fleet consumer at once**
  - In `scan_all` (`container_repo.py:85`), add to the per-page `kwargs`:
    `FilterExpression="NOT begins_with(owner_id, :pass_prefix)"` and `ExpressionAttributeValues={":pass_prefix": PASS_KEY_PREFIX}`.
  - One guard in the shared function instead of one per caller: this covers `update_service.py:145` (`schedule_task_definition_refresh_for_fleet`, which otherwise queues a task-definition refresh for a pass row at `:147`), `ops/ecs_ops.py:144`, `scripts/bedrock_gate_break_glass.py:66`, `scripts/ensure_litestream_s3_policies.py:44` and `scripts/reapply_openclaw_config.py:141`.
  - Note in the docstring that `get_by_status` (`:60`) needs no filter — a pass row carries no `status`, so it is absent from the sparse `status-index` GSI.
- [ ] **Step 4: Run the tests**

```bash
cd apps/backend && uv run pytest tests/unit/repositories/test_container_repo_pass_admission.py tests/unit/repositories/ tests/unit/core/services/ops/test_ecs_ops.py -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```
feat(lifecycle): pass admission on a durable row

container_repo.try_claim_pass admits one instance per window into a periodic
pass, with one conditional write on a reserved-key row. It fails CLOSED, the
inverse of try_claim_daily_system_notice: a fleet-wide fail-open would cost N x
every scan exactly when DynamoDB is unhappy.

It is admission for a scan, not a lease: the per-row claims inside each pass
remain the correctness boundary, so a wrong or unreachable row degrades to
today's duplicate scan. scan_all filters the reserved keys so no fleet consumer
ever sees one.
```

---

### Task 4: The periodic passes enter behind admission (C1)

**Files:**
- Modify: `apps/backend/core/services/update_service.py:1015` (`run_scheduled_worker`; delete `:1026`, `:1027`, and the `time.monotonic()` cadence blocks at `:1056-1077`)
- Modify: `apps/backend/core/services/durable_activity.py:81` (`run_trial_idle_sweeper`)
- Modify: `apps/backend/core/services/run_ledger.py:46` (`run_ledger_sweeper`)
- Test: `apps/backend/tests/unit/services/test_update_service_paperclip_passes.py` (rewrite the cadence tests at `:405`, `:409`, `:476`), `apps/backend/tests/unit/services/test_pass_admission_wiring.py` (new)

**Interfaces:**
- Consumes: `container_repo.try_claim_pass(name, *, interval_s)` from Task 3; `core.observability.metrics.put_metric`.
- Produces: six admission points with these exact names and intervals —
  `"due_updates"` (`_WORKER_INTERVAL_S`, `update_service.py:62`), `"paperclip_retry"` (`_WORKER_INTERVAL_S`), `"paperclip_purge"` (`_PURGE_PASS_INTERVAL_SECONDS`, `:56`), `"owner_day_rollup"` (`_ROLLUP_PASS_INTERVAL_SECONDS`, `:59`), `"trial_idle"` (`durable_activity._SWEEP_SECONDS`, `:16`), `"run_ledger"` (`run_ledger._SWEEP_SECONDS`, `:16`).
- Produces: metric `lifecycle.pass.claimed`, dimensions `{"pass": <name>, "outcome": "won" | "lost"}`. `outcome` is in `_ROLLUP_DIMENSIONS` (`metrics.py:81`), so `{env, service, outcome}` publishes automatically and a future alarm can bind it with no emitter change.
- **Deliberately NOT admitted:** `put_metric("update.scheduled_worker.heartbeat")` (`:1033`, the worker's own liveness) and `_recover_stale_task_definition_refreshes()` (`:950`, C2's custody recovery — admitting it would lengthen its recovery tail for no gain). Both stay per-instance.

Skipped: a CloudWatch alarm on the `lifecycle.pass.claimed` won-count. Add one when a wedged admission row actually happens — the worst consequence is a missed daily Paperclip purge, and the metric already publishes an alarm-bindable `{env, service, outcome}` identity for free.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_pass_admission_wiring.py
import asyncio
import inspect
import pytest
from unittest.mock import AsyncMock
from core.services import durable_activity, run_ledger, update_service


@pytest.mark.asyncio
async def test_worker_runs_no_pass_body_when_admission_refuses(monkeypatch):
    """The whole point: at N=2 the loser scans nothing."""
    ran = []
    monkeypatch.setattr(update_service.container_repo, "try_claim_pass", AsyncMock(return_value=False))
    for fn in ("_paperclip_provision_retry_pass", "_paperclip_purge_pass", "_rollup_pass"):
        monkeypatch.setattr(update_service, fn, AsyncMock(side_effect=lambda _f=fn: ran.append(_f)))
    monkeypatch.setattr(update_service.update_repo, "get_due_scheduled",
                        AsyncMock(side_effect=AssertionError("scanned while not admitted")))
    monkeypatch.setattr(update_service, "_recover_stale_task_definition_refreshes", AsyncMock())
    task = asyncio.create_task(update_service.run_scheduled_worker())
    await asyncio.sleep(0)
    update_service.request_worker_stop()
    await task
    assert ran == []


@pytest.mark.asyncio
async def test_worker_runs_each_pass_once_when_admitted(monkeypatch):
    ran = []
    monkeypatch.setattr(update_service.container_repo, "try_claim_pass", AsyncMock(return_value=True))
    monkeypatch.setattr(update_service.update_repo, "get_due_scheduled", AsyncMock(return_value=[]))
    for fn in ("_paperclip_provision_retry_pass", "_paperclip_purge_pass", "_rollup_pass"):
        monkeypatch.setattr(update_service, fn, AsyncMock(side_effect=lambda _f=fn: ran.append(_f)))
    monkeypatch.setattr(update_service, "_recover_stale_task_definition_refreshes", AsyncMock())
    task = asyncio.create_task(update_service.run_scheduled_worker())
    await asyncio.sleep(0)
    update_service.request_worker_stop()
    await task
    assert ran == ["_paperclip_provision_retry_pass", "_paperclip_purge_pass", "_rollup_pass"]


@pytest.mark.asyncio
async def test_purge_and_rollup_ask_for_their_documented_windows(monkeypatch):
    claims = []
    monkeypatch.setattr(
        update_service.container_repo, "try_claim_pass",
        AsyncMock(side_effect=lambda name, *, interval_s: claims.append((name, interval_s)) or False),
    )
    monkeypatch.setattr(update_service, "_recover_stale_task_definition_refreshes", AsyncMock())
    task = asyncio.create_task(update_service.run_scheduled_worker())
    await asyncio.sleep(0)
    update_service.request_worker_stop()
    await task
    assert ("paperclip_purge", update_service._PURGE_PASS_INTERVAL_SECONDS) in claims
    assert ("owner_day_rollup", update_service._ROLLUP_PASS_INTERVAL_SECONDS) in claims
    # Cadence belongs to the ROW now: no monotonic sentinel may survive in the worker.
    assert "monotonic" not in inspect.getsource(update_service.run_scheduled_worker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mod, sweeper_name, once_name",
    [
        (durable_activity, "run_trial_idle_sweeper", "sweep_trial_idle_once"),
        (run_ledger, "run_ledger_sweeper", "sweep_runs_once"),
    ],
)
async def test_sweepers_skip_their_scan_when_admission_refuses(monkeypatch, mod, sweeper_name, once_name):
    monkeypatch.setattr(mod.container_repo, "try_claim_pass", AsyncMock(return_value=False))
    monkeypatch.setattr(mod, once_name, AsyncMock(side_effect=AssertionError("scanned while not admitted")))
    stop = asyncio.Event()
    task = asyncio.create_task(getattr(mod, sweeper_name)(stop))
    await asyncio.sleep(0)
    stop.set()
    await task


@pytest.mark.asyncio
async def test_admission_emits_won_and_lost(monkeypatch):
    seen = []
    monkeypatch.setattr("core.services.run_ledger.put_metric",
                        lambda n, **kw: seen.append((n, kw.get("dimensions"))))
    monkeypatch.setattr(run_ledger.container_repo, "try_claim_pass", AsyncMock(return_value=False))
    stop = asyncio.Event()
    task = asyncio.create_task(run_ledger.run_ledger_sweeper(stop))
    await asyncio.sleep(0)
    stop.set()
    await task
    assert ("lifecycle.pass.claimed", {"pass": "run_ledger", "outcome": "lost"}) in seen
```

  Also rewrite `tests/unit/services/test_update_service_paperclip_passes.py:409` (`test_run_scheduled_worker_skips_purge_within_24h`) and `:476` (`test_run_scheduled_worker_rollup_pass_hourly_cadence`) — both currently drive `time.monotonic` through a fake clock (`:437-443`). Replace with admission-driven equivalents (stub `try_claim_pass` `True` then `False`), and delete the `last_purge_pass_at=0` assertion at `:405`.

- [ ] **Step 2: Add the shared admission helper once**
  - In `update_service.py`, next to the worker — it is where the metric lives, and the two sweepers import it:

```python
async def admitted(name: str, *, interval_s: int) -> bool:
    """Pass admission plus its metric. See container_repo.try_claim_pass."""
    won = await container_repo.try_claim_pass(name, interval_s=interval_s)
    put_metric("lifecycle.pass.claimed", dimensions={"pass": name, "outcome": "won" if won else "lost"})
    return won
```

- [ ] **Step 3: Wire the four worker passes**
  - Delete `last_purge_pass_at` / `last_rollup_pass_at` (`:1026-1027`), the `now = time.monotonic()` line (`:1061`) and both cadence `if`s (`:1062`, `:1071`). Keep `import time` (`:24`) — `:953` still uses it.
  - Guard the due-update block (`:1037-1047`) with `if await admitted("due_updates", interval_s=_WORKER_INTERVAL_S):` so the `get_due_scheduled` scan moves inside it. `apply_update`'s own claim is untouched.
  - `if await admitted("paperclip_retry", interval_s=_WORKER_INTERVAL_S): await _paperclip_provision_retry_pass()` — keep the existing `try/except` isolation around each body.
  - `if await admitted("paperclip_purge", interval_s=_PURGE_PASS_INTERVAL_SECONDS): await _paperclip_purge_pass()`.
  - `if await admitted("owner_day_rollup", interval_s=_ROLLUP_PASS_INTERVAL_SECONDS): await _rollup_pass()`.
  - Update the `run_scheduled_worker` docstring (`:1016-1021`): cadence now comes from the admission row, so a deploy no longer forces an immediate purge.
- [ ] **Step 4: Wire the two sweepers**
  - `durable_activity.py:81`: `if await admitted("trial_idle", interval_s=_SWEEP_SECONDS): await sweep_trial_idle_once()`. Keep the existing `except` → `put_metric("gateway.trial_scale_to_zero", dimensions={"trigger": "sweep_error"})`. `container_repo` is already imported (`:11`).
  - `run_ledger.py:46`: `if await admitted("run_ledger", interval_s=_SWEEP_SECONDS): await sweep_runs_once()`. Add the `container_repo` import. One-line comment: `end_run`'s conditional (`messaging_repo.py:1685`) remains the boundary — admission only stops the duplicate GSI query.
- [ ] **Step 5: Run the tests**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_pass_admission_wiring.py tests/unit/services/test_update_service_paperclip_passes.py tests/unit/services/test_update_service.py tests/unit/services/test_update_service_graceful_stop.py tests/unit/services/test_run_ledger.py tests/unit/services/test_durable_activity.py -v
```
Expected: all pass. `tests/chaos/api/conftest.py:47-50` already stubs all three loops out, so the chaos suite is unaffected.

- [ ] **Step 6: Commit**

```
feat(lifecycle)!: the periodic passes take pass admission

The four run_scheduled_worker passes and the two 60 s sweepers now enter only
on the instance that wins try_claim_pass for that window. At desiredCount 2
each full-table scan happened twice a minute; it now happens once.

The daily purge and hourly rollup lose their time.monotonic() process globals:
cadence lives on the admission row, so "once per day" is once per day fleet-wide
instead of once per day per instance, and a deploy no longer resets the sentinel
and forces an immediate purge.

Not admitted, deliberately: the worker heartbeat and the task-definition stale
recovery (custody, not a scan) stay per-instance, as do the SQS consumer and the
liveness gauge.
```

---

### Task 5: `connection_lock` deleted; both call sites take `owner_lifecycle_guard` (C3)

Supersedes D's withdrawn Task 4b (the per-`(owner, provider)` DynamoDB lease). `core/services/owner_lifecycle.py:61` already serializes across processes, is already re-entrant for the same owner on the same task (`:73-79`), and its docstring already claims this exact job.

Verified safe on this tree: nothing in the two write sections spawns a child task or calls an `@serialize_owner_lifecycle` `ecs_manager` method. `apply_connection` (`core/services/credentials/apply_connection.py:49`) only `run_in_thread`s a materializer; a materializer that drives `config_patcher` via `asyncio.run` takes `get_file_lock` (`efs_file_lock.py:45-57`, a `threading.Lock` keyed on the **absolute .lock path**) against the owner's `openclaw.json` lock (`config_patcher.py:174`, `:1351`, `:1415`), while the guard holds `.lightbulb-owner-lifecycle-locks/<sha>.lock` (`owner_lifecycle.py:29-33`) — different keys, no deadlock. Zero tests reference `connection_lock`.

**Files:**
- Modify: `apps/backend/routers/connections.py:30` (import), `:788` (DELETE teardown), `:1033` (OAuth callback write section)
- Delete: `apps/backend/core/services/credentials/connection_lock.py`
- Test: `apps/backend/tests/unit/routers/test_connections_owner_guard.py` (new)

**Interfaces:**
- Consumes: `core.services.owner_lifecycle.owner_lifecycle_guard(owner_id: str) -> AsyncIterator[None]`.
- Produces: nothing new. Granularity coarsens from per-`(owner, provider)` to per-**owner** — deliberate (C3): a disconnect racing a reprovision that re-materializes credentials is the same bug class the per-provider lock was too narrow to catch.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/routers/test_connections_owner_guard.py
@pytest.mark.asyncio
async def test_disconnect_takes_the_owner_guard_around_the_teardown(monkeypatch):
    """A disconnect landing mid-connect must not be undone by the callback's writes."""
    order = []

    @asynccontextmanager
    async def tracking_guard(owner_id):
        order.append(("acquire", owner_id))
        yield
        order.append(("release", owner_id))

    monkeypatch.setattr("routers.connections.owner_lifecycle_guard", tracking_guard)
    monkeypatch.setattr("routers.connections._disconnect_teardown",
                        AsyncMock(side_effect=lambda *a: order.append(("teardown", a[0]))))
    ...  # drive the DELETE route; assert order == [acquire, teardown, release] for one owner


@pytest.mark.asyncio
async def test_the_guard_is_taken_after_the_token_exchange_never_around_it(monkeypatch):
    """Holding an EFS file lock across a provider round-trip would be a new problem."""
    events = []
    ...  # stub oauth_dance.exchange to append "exchange"; guard to append "acquire"
    assert events.index("exchange") < events.index("acquire")


@pytest.mark.asyncio
async def test_a_nested_serialize_owner_lifecycle_call_does_not_deadlock():
    """The write section may reach an @serialize_owner_lifecycle ecs_manager method."""
    async with owner_lifecycle_guard("org_1"):
        async with owner_lifecycle_guard("org_1"):
            pass


def test_connection_lock_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        import core.services.credentials.connection_lock  # noqa: F401
```

- [ ] **Step 2: Swap both call sites**
  - `connections.py:30`: drop `connection_lock` from the `core.services.credentials` import tuple; add `from core.services.owner_lifecycle import owner_lifecycle_guard`.
  - `:788`: `async with connection_lock.connection_lock(owner_id, provider):` → `async with owner_lifecycle_guard(owner_id):`. Update the comment above it (`:785-787`) — the lock is now per-owner and cross-process, so a disconnect also serializes against a concurrent provision or resize.
  - `:1033`: same substitution (drop the `prov` argument). Keep the placement comment at `:1030-1032`; it is still exactly right — taken **after** the exchange network call, never around it.
- [ ] **Step 3: Delete the module**
  - `git rm apps/backend/core/services/credentials/connection_lock.py`.
- [ ] **Step 4: Run the tests**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_connections_owner_guard.py tests/unit/routers/ tests/unit/services/test_owner_lifecycle.py -v
grep -rn "connection_lock" apps/backend   # expect: no matches
```
Expected: all pass, grep empty.

- [ ] **Step 5: Commit**

```
fix(credentials)!: serialize connect/disconnect across instances

connection_lock was an in-process asyncio.Lock whose own docstring named the
dependency -- "The backend runs single-instance ... When multi-instance lands,
this becomes a distributed lock." At desiredCount 2 a DELETE /connections/<p>
landing mid-connect on the other instance could be undone by the callback
finishing its writes, resurrecting a revoked live credential behind a
disconnected UI. That is a security boundary, not a cosmetic race.

owner_lifecycle_guard already serializes across processes, is already
re-entrant for the same owner on the same task, and its docstring already
claims this job, so the fix deletes a module instead of rewriting one. The lock
is now per-owner rather than per-(owner, provider); the coarsening is
deliberate. Supersedes the machine-relay plan's withdrawn DynamoDB-lease task.
```

---

### Task 6: Admin idempotency becomes a durable claim and a 409 (C4)

`core/services/idempotency.py` is a module-level 60 s `dict` behind twelve admin write handlers; at N=2 two clicks on two tasks both execute. A durable replacement that preserved the "replay the cached body" contract would have to persist response bodies; refusing is the honest answer (C4).

**Verified, and it removes the spec's one frontend change:** the admin UI mints a **fresh `randomUUID()` per request** (`apps/frontend/src/app/admin/_lib/api.ts:328`), so no client ever sends the same key twice and the decorator has never deduped anything in production. Making it answer 409 is invisible to the UI. No frontend change is needed, and spec §7's "the one frontend change C carries" does not apply. Also verified: `core/services/idempotency.py` has **no test file** — spec §11's "delete `tests/unit/services/test_idempotency.py`" names a file that does not exist.

**Files:**
- Modify: `apps/backend/core/services/webhook_dedup.py` — add `idempotency()` after `record_event_or_skip` (`:713-751`)
- Modify: `apps/backend/routers/admin.py:48` (the import line ONLY — the twelve decorations at `:271, :290, :306, :334, :377, :412, :506, :555, :591, :609, :624, :640` keep their exact spelling)
- Delete: `apps/backend/core/services/idempotency.py`
- Test: `apps/backend/tests/unit/services/test_admin_idempotency_claim.py` (new)

**Interfaces:**
- Consumes: `webhook_dedup.record_event_or_skip(event_id: str, *, source: str, ttl_seconds: int | None) -> WebhookDedupResult` (`:713`), `WebhookDedupResult` (`:53`), the shared `lightbulb-{env}-webhook-event-dedup` table (already TTL'd; already used by `marketplace_purchases`, `ses_inbound`, `billing`, `apple_iap`, `metronome_webhook`, `paperclip_events`, `slack_app`, `webhooks`).
- Produces: `def idempotency(*, header: str = "Idempotency-Key", ttl_s: int = 60) -> Callable` — same name, same signature, same decorator shape, so the twelve call sites stay byte-identical.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_admin_idempotency_claim.py
import pytest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from core.services.webhook_dedup import WebhookDedupResult, idempotency


class _Req:
    def __init__(self, key=None):
        self.headers = {"Idempotency-Key": key} if key else {}


@pytest.mark.asyncio
async def test_same_key_executes_once_and_then_409s():
    calls = []

    @idempotency()
    async def handler(*, request):
        calls.append(1)
        return {"ok": True}

    results = iter([WebhookDedupResult.RECORDED, WebhookDedupResult.ALREADY_SEEN])
    with patch("core.services.webhook_dedup.record_event_or_skip",
               AsyncMock(side_effect=lambda *a, **k: next(results))):
        assert await handler(request=_Req("k1")) == {"ok": True}
        with pytest.raises(HTTPException) as exc:
            await handler(request=_Req("k1"))
    assert exc.value.status_code == 409
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_header_bypasses_the_claim_entirely():
    """Unchanged contract: an unkeyed request is not deduped and never touches DDB."""
    calls = []

    @idempotency()
    async def handler(*, request):
        calls.append(1)

    with patch("core.services.webhook_dedup.record_event_or_skip", AsyncMock(side_effect=AssertionError)):
        await handler(request=_Req())
        await handler(request=_Req())
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_two_handlers_with_one_key_both_execute():
    """The claim is namespaced per handler, as the in-memory cache key was."""
    seen = []
    with patch("core.services.webhook_dedup.record_event_or_skip",
               AsyncMock(side_effect=lambda eid, **k: seen.append(eid) or WebhookDedupResult.RECORDED)):
        @idempotency()
        async def a(*, request): ...
        @idempotency()
        async def b(*, request): ...
        await a(request=_Req("same"))
        await b(request=_Req("same"))
    assert len(set(seen)) == 2
    assert all(e.startswith("admin:") and e.endswith(":same") for e in seen)


@pytest.mark.asyncio
async def test_a_dedup_table_fault_fails_the_request_CLOSED():
    """An admin mutation that cannot be deduped must not run."""

    @idempotency()
    async def handler(*, request):
        raise AssertionError("must not run")

    with patch("core.services.webhook_dedup.record_event_or_skip", AsyncMock(side_effect=RuntimeError("ddb"))):
        with pytest.raises(RuntimeError):
            await handler(request=_Req("k"))


def test_the_in_memory_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        import core.services.idempotency  # noqa: F401
```

- [ ] **Step 2: Re-home the decorator**
  - Append to `core/services/webhook_dedup.py` (after `record_event_or_skip` ends at `:751`):

```python
def idempotency(*, header: str = "Idempotency-Key", ttl_s: int = 60) -> Callable:
    """Refuse a repeated ``Idempotency-Key`` on an admin write handler with 409.

    Replaces the in-memory TTL cache that used to live in
    ``core/services/idempotency.py``: a module-level dict is per-instance, so at
    desiredCount >= 2 two clicks landing on two tasks BOTH executed. The repo's
    own ``container_repo.try_claim_owner_deploy`` docstring made this argument
    long before anyone acted on it.

    **Contract change.** A repeat key used to re-serve the first response; it is
    now refused. Preserving replay means persisting response bodies, and the
    twelve handlers behind this are container / billing / fleet mutations whose
    services are individually claim- or condition-guarded -- the value was always
    "don't run it twice", never "give me the same body twice".

    Fails CLOSED: a dedup-table fault raises and the request fails. An admin
    mutation that cannot be deduped should not run. Requests with no header
    bypass entirely, as before.
    """

    def decorator(handler: Callable) -> Callable:
        handler_id = f"{handler.__module__}.{handler.__qualname__}"

        @functools.wraps(handler)
        async def wrapped(*args, **kwargs):
            request = kwargs.get("request")
            key = request.headers.get(header) if request is not None and hasattr(request, "headers") else None
            if not key:
                return await handler(*args, **kwargs)
            seen = await record_event_or_skip(f"admin:{handler_id}:{key}", source="admin", ttl_seconds=ttl_s)
            if seen is WebhookDedupResult.ALREADY_SEEN:
                raise HTTPException(status_code=409, detail="duplicate Idempotency-Key")
            return await handler(*args, **kwargs)

        return wrapped

    return decorator
```
  - Add `import functools` and `from fastapi import HTTPException` to the module's imports if absent.
- [ ] **Step 3: Repoint admin.py and delete the old module**
  - `routers/admin.py:48`: `from core.services.idempotency import idempotency` → `from core.services.webhook_dedup import idempotency`. Nothing else in `admin.py` changes.
  - `git rm apps/backend/core/services/idempotency.py`.
- [ ] **Step 4: Run the tests**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_admin_idempotency_claim.py tests/unit/services/test_webhook_dedup.py tests/unit/routers/ -v
grep -rn "services.idempotency" apps/backend   # expect: no matches
```
Expected: all pass, grep empty.

- [ ] **Step 5: Commit**

```
fix(admin)!: dedupe admin writes on the shared claim table, not in memory

The @idempotency decorator cached responses in a module-level dict, so two
clicks landing on two backend tasks both executed. It now takes a conditional
PutItem on lightbulb-{env}-webhook-event-dedup, keyed admin:<handler>:<key>,
and answers 409 on a duplicate.

Contract change by design (C4): a repeat key is refused rather than replayed.
No frontend change -- apps/frontend/src/app/admin/_lib/api.ts mints a fresh
randomUUID per request, so no client has ever sent a repeated key and the
in-memory cache never deduped anything in production.

The decorator moves into webhook_dedup next to the primitive it now uses, so
the twelve decorations in routers/admin.py are untouched; only the import moves.
```

---

### Task 7: Cutover — rebase, full suite, PR, dev at `desiredCount: 2`, prod

**Files:**
- Create: `docs/superpowers/specs/2026-09-13-lifecycle-multi-instance-design.md` *(renamed from the `-step-functions-` spelling below at commit time to match this plan's own filename)* (the spec, with the corrections below folded in)
- Modify: `apps/backend/AGENTS.md` (only if it names `connection_lock`, `idempotency` or the monotonic pass cadence — grep before editing)

**Interfaces:** none — documentation and release.

- [ ] **Step 1: Correct the spec before committing it as the design doc.** Five claims do not survive contact with the code and must be fixed in the committed copy, not silently left:
  - §8's "the ECS task id from the metadata endpoint, **already read elsewhere in the observability package**" — it is not. `core/observability/` reads no metadata endpoint; the precedent is `HOSTNAME` at `core/services/provisioning_workflow.py:67-68`. Also record that a per-instance ALARM is not expressible and that P12 uses the `HealthyHostCount` ratio (Task 1).
  - §8's "`run_completed` dedupes on the run-ledger row … emitting the metric only on a non-`None` `end_run` result is the whole fix" — `run.completed` is never emitted off `end_run`. Replace with the verified facts from Task 2.
  - §11's "Delete `tests/unit/services/test_idempotency.py`" — no such file exists; the module has no tests at all.
  - §12 criterion 2: `grep -rn "asyncio.Lock" apps/backend/core/services` returns five modules beyond §3's accepted list — `live_events.py:93`, `paperclip_session_cookie.py:297`, `seller_display.py:40`, `curator.py:38`, `paperclip_admin_session.py:67`. All are per-process caches or single-flight singletons that are correct at N >= 2. Extend §3's list to name them so the criterion is satisfiable as written.
  - §10.1's branch base ("off `arch/machine-relay`") — the controller's ruling is `arch/lifecycle-sfn` off `arch/stateless-backend`. C is stacked on B and runs in parallel with D; this task rebases onto `main` after both.
- [ ] **Step 2: Rebase and run everything**

```bash
git fetch origin
git rebase origin/main            # after B and D have merged, whichever order
cd apps/backend && uv run pytest tests/ -q
cd ../infra && npx jest && npx cdk synth >/dev/null   # rewrites .github/workflows/deploy.yml if app.ts moved
cd ../.. && git status --short     # deploy.yml MUST be committed in the same PR if it changed
```
Expected: full backend suite green, infra Jest green, no uncommitted `deploy.yml` drift.

- [ ] **Step 3: Open the PR** — title `feat(lifecycle)!: lifecycle without a privileged instance`. Body carries: the five spec corrections; the note that Task 1 shipped as its own PR ahead of D because C5 gates D's prod flip; and the irony worth keeping — `container_repo.try_claim_owner_deploy`'s docstring ("the backend runs several ECS tasks behind an ALB … so the in-memory `services/idempotency` cache cannot fence this") was the repo's own argument for Task 6's deletion, written long before anyone acted on it.
- [ ] **Step 4: Dev deploy, then the proof at `desiredCount: 2`** (spec §10.4 / §11 / §12):
  - `lifecycle.pass.claimed` shows exactly one `outcome=won` per window per pass, with `lost` counts matching N-1.
  - A fleet task-definition refresh drains 100 % with the N=2 backend (`_recover_stale_task_definition_refreshes` stays unadmitted, so recovery latency is unchanged).
  - A connect and a disconnect for the same owner issued concurrently against the two tasks leave the credential in the **disconnected** state.
  - Stopping one of the two dev instances puts P12 into ALARM (§12 criterion 4).
  - Two admin POSTs with the SAME `Idempotency-Key` (curl, not the UI — the UI never repeats one): one execution, one 409.
  - `grep -rn "asyncio.Lock" apps/backend/core/services` returns only guards named in the corrected §3 (§12 criterion 2); `connection_lock.py` and `idempotency.py` do not exist (§12 criterion 3).
- [ ] **Step 5: Prod promotion** once the dev proof passes, with the owner's explicit approval. Check the queue is clear first — `gh run list --repo TheLightbulbCompany/lightbulb --workflow deploy.yml --limit 10 --json status --jq '[.[]|select(.status!="completed")]|length'` must read `0` — then `gh workflow run deploy.yml --ref main`.
- [ ] **Step 6: Commit**

```
docs(lifecycle): commit the C design doc with its code corrections

Five spec claims did not survive contact with the code and are corrected in the
committed copy: the observability package reads no ECS metadata endpoint
(HOSTNAME is the precedent and a per-instance alarm is not expressible),
run.completed is not emitted off end_run and is already fenced by
claim_usage_receipt, there is no test_idempotency.py to delete, §12's
asyncio.Lock criterion now names every guard that legitimately survives at
N >= 2, and the branch base follows the controller's ruling.
```

> **As shipped, this differs in three small ways from the draft above.** The PR title is
> `feat(backend)!: lifecycle without a privileged instance (sub-project C)` (the `!:` and
> scope match the repo's convention for the other three sub-project PRs; the plan's draft
> title omitted the scope). The spec and plan filenames use `-multi-instance-` throughout
> rather than `-step-functions-`, matching `progress.md`'s own header correction ("was 'as
> Step Functions'"). Both docs land in one commit, `docs(lifecycle): spec and plan for the
> multi-instance lifecycle work`, rather than the plan's drafted `docs(lifecycle): commit
> the C design doc with its code corrections` (this plan is committed alongside the spec,
> so one commit message covers both files).
>
> **Review round (corrected here, Task 7 step 1).** Beyond Task 3/4's `<=`/`except
> Exception` fix noted above, the OPUS review of Tasks 5/6 (APPROVED WITH MINORS, four
> Importants) produced a second follow-up commit, `fix(lifecycle): review round — busy
> redirect on guard timeout, off-loop admin claim, fault logging` (`c82f0f02c` on this
> branch post-rebase): (1) `routers/connections.py` — a guard-acquire timeout on the OAuth
> callback used to surface as a raw 500 after the provider state was already consumed; it
> now returns the existing busy response on both the callback and the DELETE route; (2)
> `webhook_dedup.record_event_or_skip`'s `PutItem`, now reached from the request-handling
> event loop via the admin idempotency claim, is routed through `run_in_thread` like the
> sibling `PutItem` in `_claim_active_top_up` — the same event-loop-blocking class as the
> 2026-09-12 EFS-tree-walk freeze; (3) the decorator's docstring now says plainly that a
> raising handler keeps its claim (releasing on failure would reopen the double-click race
> the decorator exists to close); (4) `try_claim_pass`'s callers used to emit `won`/`lost`
> from the boolean alone, making a persistent DynamoDB fault indistinguishable from ordinary
> contention — the fault path now emits `lifecycle.pass.claimed` `outcome=fault` directly
> from `try_claim_pass` itself (reflected in spec.md §8/§9 above).

---

## Pre-flight seam table

Produces → consumes, within C and against `arch/stateless-backend` (B, this tree) and `arch/machine-relay` (D).

| # | Producer | Consumer | Seam | Status | Evidence |
|---|---|---|---|---|---|
| 1 | T3 `container_repo.try_claim_pass(name, *, interval_s, now=None) -> bool` | T4 `admitted()` in `update_service`, `durable_activity`, `run_ledger` | signature + fail-closed contract | **consistent** | new symbol; `grep -rn "try_claim_pass" apps/backend` returns nothing on this tree |
| 2 | T3 `PASS_KEY_PREFIX` reserved rows on `containers` | `scan_all` consumers: `update_service.py:145`, `ops/ecs_ops.py:144`, `scripts/{bedrock_gate_break_glass,ensure_litestream_s3_policies,reapply_openclaw_config}.py` | a sentinel row reaching `queue_task_definition_refresh_for_owner` | **consistent after T3 step 3** | `update_service.py:147` loops every `scan_all` row with no filter; the one-line `FilterExpression` in `scan_all` (`container_repo.py:85`) covers all five callers at once |
| 3 | T3 reserved rows | `container_repo.get_by_status("running")` (`:60`, used by `main.py:257`) | pass row appearing in the running gauge | **consistent, no change needed** | `status-index` is a GSI on `status` (`database-stack.ts:122-124`); a pass row carries no `status`, so it is absent from the sparse index |
| 4 | T4 `lifecycle.pass.claimed {pass, outcome}` | `metrics.put_metric` rollups | does an alarm-bindable identity publish | **consistent** | `outcome` is in `_ROLLUP_DIMENSIONS` (`metrics.py:81`), so `[env, service, outcome]` is emitted automatically |
| 5 | T1 `gauge("gateway.running.count", n, {"instance": …})` | P12's `dimensionsMap {env, service}` (`observability-stack.ts:863`) | does the alarm's identity still receive data | **consistent** | `put_metric` appends `_ROLLUP_KEYS` whenever the full set differs (`metrics.py:123-124`); `instance` is not in `_ROLLUP_DIMENSIONS`, so no third set is added |
| 6 | T1 emitter shape | `EMITTED_DIMENSION_SETS` contract test (`observability-stack.test.ts:173`, `:583`) | B's registration requirement | **consistent after T1 step 4** | entry at `:193` moves `[[]]` → `[["instance"]]`; the test short-circuits `extraDims.length === 0` at `:597`, which is what P12 binds |
| 7 | T1 P12 metric-math alarm | `isol8MetricBindings` walker (`observability-stack.test.ts:287`) | does the walker see math-expression alarms | **consistent** | the walker already descends `props.Metrics[].MetricStat.Metric` at `:309-311` |
| 8 | T1 `props.targetGroup` denominator | `ObservabilityStackProps` (`observability-stack.ts:56`) | is the target group already a prop | **consistent** | W28 already binds `props.targetGroup.targetGroupFullName` at `:1746`/`:1760`; wired in `lightbulb-stage.ts:313` |
| 9 | T5 `owner_lifecycle_guard` at `connections.py:788`/`:1033` | `apply_connection` → `run_in_thread(materializer)` → `asyncio.run(config_patcher…)` | EFS-lock deadlock | **consistent** | `get_file_lock` keys on the absolute `.lock` path (`efs_file_lock.py:45-57`); guard path is `.lightbulb-owner-lifecycle-locks/<sha>.lock` (`owner_lifecycle.py:29-33`), config_patcher uses the owner's openclaw.json lock (`:174`, `:1351`, `:1415`) |
| 10 | T5 module deletion | any importer | stranded import | **consistent** | `grep -rn "connection_lock"` returns only `routers/connections.py:30/788/1033` and the module itself; zero test references |
| 11 | T6 `webhook_dedup.idempotency()` | the 12 decorations in `routers/admin.py` | decorator shape | **consistent** | same name, same kwargs, same `request` kwarg contract; only `admin.py:48` changes |
| 12 | T6 409 contract | `apps/frontend/src/app/admin/_lib/api.ts:328` | does the UI need a 409 branch | **consistent — the spec's frontend change is unnecessary** | the UI mints `randomUUID()` per request, so a repeated key never reaches the backend |
| 13 | T2 `run.completed` | `EMITTED_DIMENSION_SETS["run.completed"]` + W67 | does the metric survive D deleting `operator_proxy.py` | **consistent** | the surviving caller `messaging.py:1923` is `handle_usage_report`, documented as the path for "cron, heartbeat, **Paperclip**" runs, so W67's `source=paperclip` signal is unaffected |
| 14 | C vs D — `apps/backend/main.py` | both branches | **file-level collision, hunks disjoint** | **consistent** | `git diff arch/stateless-backend arch/machine-relay -- apps/backend/main.py` shows only `:44` (router import) and `:779` (mount); C touches `:251` |
| 15 | C vs D — `apps/infra/lib/stacks/observability-stack.ts` | both branches | **file-level collision, hunks ~900 lines apart** | **consistent, but ORDERED** | D edits W78; C edits P12 (`:854-905`). C5 gates D's prod flip, so T1 must merge FIRST as its own PR |
| 16 | C vs D — every other C file | D's declared set (`connection_service.py`, `machine_auth.py`, `relay_hooks.py`, `routers/gateway_relay.py`, `routers/websocket_chat.py`, `desktop_pair*.py`, `paperclip_openclaw_pairing.py`, `core/config.py`, `service-stack.ts`, `api-stack.ts`; deletes `node_proxy.py`, `operator_proxy.py`, `node_connection.py`, `operator_rpc.py`) | overlap | **no collision** | C touches `container_repo.py`, `update_service.py`, `durable_activity.py`, `run_ledger.py`, `routers/connections.py`, `routers/admin.py`, `webhook_dedup.py`, `messaging.py`, `observability-stack.test.ts` — none in D's set. `routers/connections.py` (C) and `core/services/connection_service.py` (D) are different files |
| 17 | C vs B | `arch/stateless-backend` @ `673c23b66` | C is built on B | **consistent** | every `path:line` in this plan was read on this detached checkout of `arch/stateless-backend` |
| 18 | Spec §8 "metadata endpoint already read in the observability package" | `core/observability/` | — | **INCONSISTENT** | no metadata read exists there (`ls core/observability/` → `e2e_correlation, logging, metrics, middleware, product_analytics`); precedent is `provisioning_workflow.py:67-68`. Corrected in T1 + T7 step 1 |
| 19 | Spec §8 "`run_completed` … emit only on a non-None `end_run`" | `run_analytics.py:158` / `messaging.py:1923` | — | **INCONSISTENT** | `run.completed` is never emitted off `end_run`; the surviving caller is already fenced by `claim_usage_receipt` (`messaging_repo.py:1597`). Corrected in T2 + T7 step 1 |
| 20 | Spec §11 "delete `tests/unit/services/test_idempotency.py`" | the tests tree | — | **INCONSISTENT** | the file does not exist; `core/services/idempotency.py` has no tests at all |
| 21 | Spec §12 criterion 2 | `grep -rn "asyncio.Lock" apps/backend/core/services` | — | **INCONSISTENT (doc-only)** | five modules beyond §3's list return hits (`live_events.py:93`, `paperclip_session_cookie.py:297`, `seller_display.py:40`, `curator.py:38`, `paperclip_admin_session.py:67`); all correct at N >= 2. Corrected in T7 step 1 |
| 22 | Spec §10.1 "branch `arch/lifecycle` off `arch/machine-relay`" | the controller ruling in `progress.md` (`arch/lifecycle-sfn` off `arch/stateless-backend`) | branch base | **INCONSISTENT — ruling wins** | C is stacked on B and runs in parallel with D; T7 rebases onto `main` after both |
| 23 | Spec §8 "P12's alarm moves to per-instance evaluation" | CloudWatch alarm semantics | is a per-instance alarm buildable | **INCONSISTENT — deviation, implemented as a ratio** | an alarm binds `dimensionsMap` at synth time, ECS task ids change every deploy, and `SEARCH` is rejected on metric alarms (`metrics.py:60-66`). T1 implements the instance-count-independent `HealthyHostCount` ratio instead, and still adds the `instance` dimension for diagnosis |
