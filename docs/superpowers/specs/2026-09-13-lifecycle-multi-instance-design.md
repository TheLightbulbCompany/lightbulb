# Lifecycle without a privileged instance — design (sub-project C)

Fourth of four durable-architecture sub-projects (A async turn contract, B stateless
backend, C lifecycle, D machine relay off the API Gateway WebSocket door). C is the last
one: A took room turns off the gateway socket, B deleted the connection pool, D moves the
machine relays onto the `bv-` door. What is left holding the backend to one instance is
not a socket — it is **time**: five `asyncio` loops started in `main.py`'s lifespan, two
in-process mutual-exclusion maps, and a handful of per-process monotonic timers that
decide when a daily or hourly pass runs.

Read against `arch/stateless-backend` @ `673c23b66` (post-B, pre-D-merge). Sources: A spec
§10, B spec §3 "Untouched until C" and §13, D's `preflight-report.md` §"parked items" and
its three unlisted N=2 hazards.

> **Corrected at commit time (Task 7 step 1).** Five claims in the original draft did not
> survive contact with the code and are fixed in place below rather than left standing:
> the §4 admission window's boundary condition, §8's metadata-endpoint and `run_completed`
> claims, §11's reference to a test file that never existed, §12's `asyncio.Lock` criterion
> (five more correct guards found on the tree), and §10.1's branch base. Each correction is
> marked inline.

---

## 1. Problem

The backend's lifecycle work is correct today because exactly one process runs it. Four
distinct mechanisms carry that assumption:

**(a) Loops, not schedules.** `main.py`'s lifespan starts five background tasks —
`run_scheduled_worker` (60 s), `openclaw_worker.run` (SQS, correctly per-instance by
design), `run_trial_idle_sweeper` (60 s), `run_ledger_sweeper` (60 s) and
`_running_count_gauge_loop` (60 s). Every one of them runs on *every* instance. At
`desiredCount: 2` each full-table scan happens twice a minute instead of once. The work
that follows is claim-guarded (`try_claim_idle_stop`, `messaging_repo.end_run`,
`update_repo.claim_stale_apply`), so the outcome is right; the cost is not.

**(b) Cadence stored in a process global.** `run_scheduled_worker` decides when to run its
daily Paperclip purge and its hourly owner-day rollup from `last_purge_pass_at` /
`last_rollup_pass_at` — `time.monotonic()` values in the worker's own frame. Two instances
hold two independent clocks, so "once per day" becomes "once per day per instance", and
every deploy resets both to the `0.0` sentinel that forces an immediate run. The rollup
pass documents itself as idempotent under "the transient ×2 worker during deploys"; the
purge pass makes no such claim and calls `PaperclipProvisioning.purge` on due rows.

**(c) In-process mutual exclusion.** `core/services/credentials/connection_lock.py` is a
per-`(owner, provider)` `asyncio.Lock` serializing the OAuth callback's write section
against `DELETE /connections/<provider>`. Its own docstring names the dependency: *"The
backend runs single-instance … When multi-instance lands, this becomes a distributed
lock."* A disconnect landing mid-connect on the other instance resurrects a revoked live
credential behind a disconnected UI. That is a security boundary, not a cosmetic race.
`core/services/idempotency.py` is the same class one rung down: a module-level 60 s
`dict` behind twelve admin write handlers.

**(d) Custody by heartbeat.** A task-definition refresh holds `applying` state for as long
as its ECS waiter runs, kept alive by `_heartbeat_apply_claim` every 30 s, and reclaimed by
`_recover_stale_task_definition_refreshes` after three missed beats — at most three rows per
worker tick. This is a hand-rolled version of exactly what `ProvisionStateMachine` already
does for provisioning, and it is the one place in C where a state machine is the
straightforwardly better tool rather than a bigger one.

None of this blocks `desiredCount: 2` on its own — B proved the dev flip and D flips prod.
C is what makes N ≥ 2 *cheap and honest* rather than merely survivable, and it closes the
one item in the set that is a real defect (c).

---

## 2. Decisions for the owner

**C1 — Scope: do NOT build a state machine per loop.** The sub-project is named "lifecycle
as Step Functions", and the honest reading of the code is that only one job has the shape
Step Functions is for: the update apply (§5). The periodic passes need a *scheduler*, not a
*custodian* — they are cheap scans whose follow-on work is already conditional-write
guarded. Proposal: give the passes a durable singleton lease (§4, ~30 lines, no new infra)
and leave the state machine for the apply. **Recommended: yes.** The alternative — one
EventBridge Scheduler rule per pass firing a Step Functions execution that `RunTask`s the
backend image, mirroring `ProvisionStateMachine`'s `runProvisioner` — costs a Fargate cold
start per 60-second tick and buys nothing the lease does not.

**C2 — Update-apply custody: move it to a state machine, or keep the heartbeat?** The
claim + heartbeat + stale-recovery path (§1d) is correct at N ≥ 2 today: the claim is
conditional, takeover is fenced, and a fleet roll of 9 owners drained cleanly on dev on
2026-09-13. A state machine would shorten recovery from "up to 90 s stale + one worker
tick, three rows per tick" to "one Wait state", and would delete ~100 lines of hand-rolled
custody (`_heartbeat_apply_claim`, `_recover_stale_task_definition_refreshes`,
`_APPLY_HEARTBEAT_S`, `_APPLY_STALE_S`, `_RECOVERY_MAX_PER_TICK`). It buys **recovery
latency, not correctness**. **Recommended: not now** — do C1's lease first, re-measure a
prod fleet roll at N = 2, and take C2 only if the recovery tail actually shows up. This is
the decision most likely to be overruled on the strength of the sub-project's name; if the
owner wants it, it is Task 6 and it is modelled line-for-line on `ProvisionStateMachine`.

**C3 — `connection_lock` becomes `owner_lifecycle_guard`, and the module is deleted.**
D's plan proposed a **DynamoDB lease for `connection_lock`** — its Amendment 7 / new Task
4b: a short-TTL `mutation_lock_until` attribute plus a fencing token on the existing
`integrations` row keyed `(owner_id, provider)`, conditional acquire, released in a
`finally`, ~25 lines, both call sites keeping `async with`. **C supersedes that proposal**:
`core/services/owner_lifecycle.py:owner_lifecycle_guard(owner_id)` already exists, already
serializes across processes (an EFS `O_EXCL` guard plus a process-local file lock), is
already re-entrant for the same owner on the same task, and its docstring already claims
this exact job — *"Serialize owner-scoped ECS and credential mutations across workers."*
Wrapping the two call sites in it deletes `connection_lock.py` outright instead of
rewriting it, and adds no new attribute, no new lease clock, and no second fencing-token
discipline to keep correct. **D's ledger records the withdrawal of Task 4b**; C owns the
change. The cost is coarser granularity — a connect now serializes against a provision or
resize for the same owner, where before it only serialized against the same provider — and
that is a *feature*: a disconnect racing a reprovision that re-materializes credentials is
the same bug class the per-provider lock was too narrow to catch. **Recommended: yes.**

**C4 — Admin idempotency becomes a 409, not a replayed body.** `idempotency.py` caches the
handler's *response* and returns it verbatim on a repeat key. A durable replacement that
preserves that contract has to persist response bodies; one that does not can only refuse.
Proposal: refuse — a conditional claim on the shared `webhook-event-dedup` table, `409` on
the duplicate (§7). Two rapid admin clicks then get one execution and one honest error
instead of one execution and a silently re-served stale body. **Recommended: yes.**

**C5 — P12 and `run_completed` at N ≥ 2** (parked by B, unaddressed by D). P12
(backend-liveness) alarms on `SampleCount < 5` over five minutes against
`gateway.running.count`, which one instance emits once a minute. At N = 2 it reads 10 and
cannot distinguish "one instance died" from "the survivor's rate halved". `run_completed`
(feeding W67) may double-count once two instances can pick up work for the same owner — the
`emit_metric` flag deleted with the pool's usage path existed for exactly this. Proposal:
add a per-instance dimension to the gauge and make the alarm instance-count-independent
(§8); confirm `run_completed` is already deduped on the durable usage receipt, which is
already the single gate its surviving emitter passes through. **Recommended: yes, and it
gates D's prod `desiredCount` flip** — an alarm that cannot see a dead instance is worse
than no alarm when there are two.

**C6 — Prod autoscaling policy.** B's success bar was `desiredCount: 2` proven on dev; D
flips prod to 2–4. C changes nothing about the policy itself, but C5 is its precondition.
**Recommended: land C5 before D's flip, then leave the 2–4 range as D specified.**

---

## 3. Scope and boundaries

### Removed

| Deleted | Why it can go |
|---|---|
| `core/services/credentials/connection_lock.py` (the module, `_locks`, and the `connection_lock()` accessor) | §6 — both call sites move to `owner_lifecycle_guard`, which already does this across processes. Supersedes D's Task 4b DDB lease (C3). |
| **`core/services/idempotency.py`** (the module, `_cache`, `_purge_expired`, `reset_cache`, the `idempotency()` decorator) **and the twelve `@idempotency()` decorations in `routers/admin.py`** — lines 271, 290, 306, 334, 377, 412, 506, 555, 591, 609, 624, 640 | §7 — replaced by a **409 dedup-table claim** on the shared `webhook-event-dedup` table (`webhook_dedup.record_event_or_skip`, a conditional `PutItem` that already exists and is already used by four routers). The in-memory cache is per-instance and cannot fence two clicks landing on two tasks. |
| `_heartbeat_apply_claim`, `_recover_stale_task_definition_refreshes`, `_APPLY_HEARTBEAT_S`, `_APPLY_STALE_S`, `_RECOVERY_MAX_PER_TICK` in `update_service.py`, and `update_repo.claim_stale_apply` / `list_stale_applying` | **Only if C2 is taken.** Otherwise these stay exactly as they are. |
| `last_purge_pass_at` / `last_rollup_pass_at` process globals in `run_scheduled_worker` | §4 — the cadence moves onto the lease row, so it survives a deploy instead of resetting to the `0.0` sentinel. |

### Re-homed

| Today | After C |
|---|---|
| `run_scheduled_worker`'s due-update pass, Paperclip retry pass, Paperclip purge pass, owner-day rollup pass — each running on every instance every tick | the same passes, each entered only by the instance that wins `try_claim_pass(name, interval_s)` for that tick (§4). Bodies unchanged. |
| `sweep_trial_idle_once` (`durable_activity.py`), `sweep_runs_once` (`run_ledger.py`) | same, behind the same lease. The per-row claims inside (`try_claim_idle_stop`, `end_run`'s conditional) stay — the lease removes duplicate *scans*, it is not the correctness boundary and must not become one. |
| `routers/connections.py:788` (`_disconnect_teardown`) and `:1033` (the OAuth callback write section) | `async with owner_lifecycle_guard(owner_id):` — same placement, same width, still taken **after** the provider network call, never around it. |
| the twelve `@idempotency()` admin handlers | a `Depends` (or a thin decorator of the same shape) that claims `admin:{handler_id}:{key}` and raises `409` when the claim is refused. |
| `_running_count_gauge_loop`'s `gauge("gateway.running.count", …)` | same gauge, plus an `instance` dimension (§8). Behind the lease it would emit once per minute total — which is what P12 wants — but the *liveness* reading needs one sample per instance, so this loop deliberately stays unleased and gains a dimension instead. |

### Kept, deliberately

- **`openclaw_worker.run`** — the SQS consumer runs on every instance by design (B §3). Not
  a pass, not leased, not touched.
- **`try_claim_provision`, `claim_attempt`, `is_provision_claim_stale`,
  `ProvisionStateMachine` and `dispatch_provision`'s start-before-spawn invariant** — already
  durable, already cross-instance, already the precedent C argues from. The wall-clock
  stale-lease heuristic in `is_provision_claim_stale` stays; it is the only recovery for a
  backend that dies between `try_claim_provision` and `_start`.
- **`try_claim_owner_deploy` / `extend_owner_deploy_claim` / `release_owner_deploy_claim`**
  — named in B's "Untouched until C", inspected, and left alone. It is already a fenced
  conditional write on the `containers` row, already owner-wide, already documents that "the
  backend runs several ECS tasks behind an ALB … so the in-memory `services/idempotency`
  cache cannot fence this". Its `DEPLOY_CLAIM_STALE_S > PAIR_WORST_CASE_S` relationship is
  asserted by a test. Nothing about N ≥ 2 changes it. **Note the irony worth keeping in the
  PR description:** this docstring is the repo's own argument for §3's `idempotency.py`
  deletion, written long before anyone acted on it.
- **`try_claim_daily_system_notice`, `try_consume_daily_email_send`, `try_claim_idle_stop`,
  `idle_stop_claim_current`** — the existing conditional-write idiom on the `containers`
  row. §4's lease is modelled on `try_claim_daily_system_notice` and adds no new pattern.
- **`owner_lifecycle_guard`'s EFS-file-lock implementation.** It is not pretty (a shared
  filesystem as a lock manager), but it is already load-bearing for nine `ecs_manager`
  methods and it already works across processes. Replacing it with DynamoDB is a separate,
  bigger change and is not C.

### Accepted at N ≥ 2 — documented, not fixed

- **`_resume_wake_resize_transitions`** (`main.py`, boot): both instances re-kick
  `_await_running_transition` for the same *unclaimed* provisioning rows. The poller is
  idempotent and converges (its own docstring: "a poller finding an already-healthy
  container transitions it on its first iteration and exits"), and SFN-owned rows are
  already excluded. Cost is a duplicate describe loop per affected owner per deploy.
- **`user_provision._org_provision_locks`**: a per-org `asyncio.Lock` that is explicitly
  documented as a process-level accelerator in front of a strongly-consistent cross-instance
  guard. Not a defect; leave it.
- **`onboarding_brief._draining`**: a re-entrance set whose retry budget is *persisted*, so
  a second instance draining the same owner spends attempts against the same durable budget.
  Degrades to redundant work.
- **Five more per-process `asyncio.Lock` guards, all correct at N ≥ 2** *(correction, Task 7
  step 1 — `grep -rn "asyncio.Lock" apps/backend/core/services` returns these beyond the
  entries above)*: `live_events.py:93` (`_signing_lock`, guarding a per-process
  live-events-signing-key cache), `paperclip_session_cookie.py:297` (`_locks`, a per-`user_id`
  single-flight guard for minting a Paperclip session cookie), `seller_display.py:40`
  (`_lock`, guarding a module-level seller display-name cache), `curator.py:38` (`_lock`,
  same shape for curated-listing state), `paperclip_admin_session.py:67` (`_lock`,
  single-flight for the bootstrap-admin session mint). Each is a per-process cache or
  single-flight accelerator in front of an idempotent or strongly-consistent operation, not
  custody of shared state — the same class as `user_provision._org_provision_locks` above.
- Everything B already listed: `teams_event_broker` clients, `_typing_last` / `_presence_last`,
  `run_analytics._seen_run_ids` / `_run_actor`, `product_analytics._daily_dedup_seen`,
  `wake_policy._suppressed_emitted`, `routers/container.py._browser_restore_inflight`, and
  the TTL read caches.

---

## 4. The singleton pass lease

One primitive, `container_repo`-shaped, on a single fixed-key row rather than the per-owner
`containers` row (the passes are fleet-scoped, not owner-scoped):

```python
async def try_claim_pass(name: str, *, interval_s: int, now: int | None = None) -> bool:
    """Admit at most one instance into pass `name` per `interval_s` window.

    One conditional write: SET last_run_at = :now WHERE attribute_not_exists(last_run_at)
    OR last_run_at <= :cutoff, with :cutoff = now - interval_s. `<=`, not `<` — with a
    strict less-than, a pass whose interval equals its caller's tick (all four
    run_scheduled_worker passes and both sweepers are 60 s) would never see
    elapsed == interval_s admitted and would only fire every OTHER tick. Returns False on
    ConditionalCheckFailedException (somebody else has this tick).

    FAILS CLOSED, unlike try_claim_daily_system_notice: a DynamoDB fault skips this tick
    rather than admitting every instance. A skipped 60-second sweep costs one minute of
    latency on a trial park or a swept run; a fleet-wide fail-open costs N× every scan in
    the product exactly when DynamoDB is already unhappy.
    """
```

*(Correction, Task 7 step 1: the condition above is `<=`. A strict `<` — as an earlier draft
of this primitive had it — admits only at `elapsed > interval_s`, which for a 60 s pass on a
60 s tick means real cadence of ~120 s at N = 1 and ~82 s at N = 2 instead of 60 s. Caught in
Task 3/4 review, fixed before merge.)*

Cadence moves from the process frame onto that row, so `interval_s` — not process uptime —
decides a daily purge, and a deploy no longer forces one. The worker loop keeps its 60 s
tick and its existing per-pass `try/except` isolation; each pass body simply becomes:

```python
if await container_repo.try_claim_pass("paperclip_purge", interval_s=86_400):
    await _paperclip_purge_pass()
```

The trial-idle and run-ledger sweepers move under the same call with `interval_s=60`.

**The lease is an efficiency device, not a correctness boundary.** Every per-row claim
inside every pass stays exactly as it is. If the lease row is wrong, stale, or unreachable,
the worst outcome is a missed or duplicated *scan* — and a duplicated scan is precisely
what the code does today on every instance, so the failure mode is the status quo.

`ponytail: one fixed-key row, so all passes contend on one partition. Fine at ~4 writes/min
fleet-wide; shard the key by pass name if that ever stops being true.`

---

## 5. Update apply custody (C2, conditional)

Taken only if the owner overrules C2's recommendation. The shape, for completeness:

`UpdateApplyStateMachine`, modelled on `ProvisionStateMachine` in `service-stack.ts`:
`InitWatch → RunApply` (ECS `RunTask`, `RUN_JOB`, the backend image with
`command: ["python", "-m", "scripts.apply_update", owner_id, update_id]`) → watch the
`pending-updates` row on a 60 s `Wait` → `Succeed` on `applied` / `failed` / row-gone, retry
the ECS leg on `error` or after a tick backstop. Exit-code ABI identical to
`scripts/provision_runner.py` (10 deterministic, 20 superseded). `apply_update` gains a
`PROVISION_ATTEMPT_TOKEN`-equivalent so a superseded straggler is fenced at its next
checkpoint, exactly as `claim_attempt` fences provisioning.

Then §3's "Removed, only if C2" row applies: the heartbeat task, the stale-recovery pass and
their three constants go, and `update_repo` loses `claim_stale_apply` / `list_stale_applying`.

---

## 6. Per-owner mutual exclusion

```python
# routers/connections.py:788  (DELETE /connections/{provider})
async with owner_lifecycle_guard(owner_id):
    await _disconnect_teardown(owner_id, provider, p)

# routers/connections.py:1033 (OAuth callback, after the token exchange)
async with owner_lifecycle_guard(owner_id):
    ...
```

Then delete `core/services/credentials/connection_lock.py` and its import.

Three things that make this a drop-in rather than a rewrite:

1. **Re-entrance.** The guard admits a nested acquire for the same owner on the same
   asyncio task, so a write section that reaches an `@serialize_owner_lifecycle`
   `ecs_manager` method does not deadlock. It raises on a nested acquire for a *different*
   owner — which these paths never do.
2. **A child task does not inherit ownership.** Anything the write section spawns with
   `asyncio.create_task` will wait for its parent, which is the behaviour we want here.
3. **Placement is unchanged.** The guard is taken after the provider network call and
   around the write section only, exactly where the `asyncio.Lock` sat. Holding an EFS lock
   across a provider round-trip would be a new problem.

The one behavioural change worth stating plainly: the lock is now per-**owner**, not
per-(owner, provider). Connecting Slack while disconnecting GitHub now serializes. Both are
second-scale write sections; the coarsening is deliberate (C3).

---

## 7. Admin idempotency

Replace the decorator body, not its shape:

```python
key = request.headers.get("Idempotency-Key")
if key:
    claim = f"admin:{handler_id}:{key}"
    seen = await webhook_dedup.record_event_or_skip(claim, source="admin", ttl_seconds=60)
    if seen is WebhookDedupResult.ALREADY_SEEN:
        raise HTTPException(status_code=409, detail="duplicate Idempotency-Key")
return await handler(*args, **kwargs)
```

Requests with no header bypass it, as today. `record_event_or_skip` is an existing
conditional `PutItem` on `lightbulb-{env}-webhook-event-dedup`, already TTL'd, already used
by `marketplace_purchases`, `ses_inbound`, `billing` and `apple_iap`; it takes a
`ttl_seconds` override, so the 60 s window is expressed without a new table, a new repo, or
a new expiry mechanism.

**This is a contract change, and it is the point of C4.** Today a repeat key re-serves the
first response; after C it is refused. The twelve handlers behind it are admin container,
billing and fleet mutations whose underlying services are individually claim- or
condition-guarded, so the value of the decorator was always "don't run it twice", never
"give me the same body twice". The admin UI must treat 409 as "already done" rather than as
a failure — that is the one frontend change C carries.

---

## 8. Observability at N ≥ 2

*(Both bullets below are corrections, Task 7 step 1 — the original draft attributed a
metadata-endpoint reader to `core/observability/` that does not exist there and described
`run_completed`'s dedup mechanism inaccurately.)*

- `gauge("gateway.running.count", …)` gains an `instance` dimension: the ECS task id read
  from `os.environ["HOSTNAME"]` — Fargate sets `HOSTNAME` to the task id, and this is the
  same source already relied on with exactly this comment at
  `core/services/provisioning_workflow.py:67-68`. `core/observability/` (`e2e_correlation.py`,
  `logging.py`, `metrics.py`, `middleware.py`, `product_analytics.py`) reads no metadata
  endpoint and never has — there is no precedent there to point to.
  A **per-instance CloudWatch alarm is not expressible**: an alarm binds a fixed
  `dimensionsMap` at synth time, ECS task ids are unknown then and change on every deploy,
  and `SEARCH` is rejected outright on metric alarms (`core/observability/metrics.py:60-66`
  carries this same scar). P12 instead becomes **instance-count-independent**: a
  `cloudwatch.MathExpression` divides the fleet `SampleCount(gateway.running.count)` over
  `{env, service}` by `Minimum(AWS/ApplicationELB HealthyHostCount)` on the backend's own
  target group — `Minimum`, not `Average`, because a rolling deploy runs at
  `maxHealthyPercent: 200` and `Average` would dilute the ratio with the transient extra
  tasks' partially-filled sample windows. Healthy reads ~10 samples per instance per 10-minute
  window; the alarm fires below 7, so one dead loop of two (~5) pages while a halved rate at
  N = 1 does not apply here (there is nothing to divide by at N = 1 changing). The `instance`
  dimension is for diagnosis on a dashboard or in Logs Insights, not for the alarm's own
  binding. `_running_count_gauge_loop` stays unleased for exactly this reason — it is the
  liveness canary, and a leased canary proves only that *some* instance is alive.
- `run_completed` is **already** single-emit at N ≥ 2, and needs no code change. Its only
  surviving emitter is `run_analytics.schedule_run_completed` (`run_analytics.py:158`),
  called from exactly one caller, `core/services/messaging.py`'s `handle_usage_report`
  (the path documented for "cron, heartbeat, Paperclip" runs — the room-turn contract emits
  usage elsewhere). `handle_usage_report` is gated by `messaging_repo.claim_usage_receipt`
  — a conditional `PutItem` that admits a given `(owner_id, run_id)` at most once
  fleet-wide — *before* it schedules the metric, so a replay short-circuits to
  `status: "replayed"` and never re-emits. It is **not** emitted off `end_run`: the ledger's
  own terminal-state gate (`messaging_repo.end_run`'s conditional) feeds a different metric,
  `messaging.run.ended`, on a separate call path. `run_analytics._seen_run_ids` is a
  per-process accelerator only and is not what prevents a cross-instance double-emit — the
  durable receipt is. Task 2 adds the regression test that pins this and corrects the one
  stale comment that used to claim otherwise.
- One new metric, `lifecycle.pass.claimed`, dimensioned by pass name and `won`/`lost`/`fault`
  (`fault` added during review so a persistent DynamoDB error is distinguishable from
  ordinary contention on the same tick). A pass whose `won` count drops to zero fleet-wide is
  a wedged lease row and is the only new failure mode C introduces.

---

## 9. Failure handling

| Failure | Behaviour |
|---|---|
| Lease row unreachable | `try_claim_pass` fails closed; the tick is skipped; `lifecycle.pass.claimed` shows `outcome=fault`, not `won` or `lost`. Recovered on the next tick. |
| Lease row wedged (a write lands with a far-future `last_run_at`) | Every instance skips that pass indefinitely. Detected by the `won == 0` condition above; repaired by deleting the attribute. This is the price of the lease and is stated here so it is not a surprise. |
| Instance dies mid-pass | The lease is not renewed and not released; the next window admits another instance. Passes are scans whose follow-on work is individually claimed, so a half-finished scan loses nothing but the remainder of that tick. |
| EFS unavailable (§6) | `owner_lifecycle_guard` blocks on the file lock, so connect/disconnect blocks. Same exposure nine `ecs_manager` methods already have; not new, and not worth a second mechanism to avoid. A guard timeout surfaces as the existing "busy" response on both routes, not a raw 500. |
| Dedup table unreachable (§7) | `record_event_or_skip` raises; the admin request fails. Fail-closed is right here — an admin mutation that cannot be deduped should not run. |

---

## 10. Rollout

No backward compatibility, no feature flags — the standing ruling. One cutover per
environment.

1. *(Correction, Task 7 step 1 — the original draft said "Branch `arch/lifecycle` off
   `arch/machine-relay`", i.e. stacked on D. The controller's ruling, and what actually
   shipped, is the opposite stacking.)* Branch `arch/lifecycle-multi-instance` off
   `arch/stateless-backend` (C stacks on B; C runs in **parallel** with D, not stacked on
   it — the two touch no common file except `main.py` and `observability-stack.ts`, both
   disjoint hunks per the pre-flight seam table in `plan.md`). C's own PR can merge
   independently of D; D's prod `desiredCount` flip waits on C5. Rebase onto `main` at
   cutover, after both B and D have merged.
2. Land §8 (P12 dimension + `run_completed` regression pin) **first and separately** — it
   gates D's prod `desiredCount` flip (C5) and is independently deployable.
3. Land §4, §6, §7 together; they touch disjoint files and share one test pass.
4. Dev deploy, then the proof: `desiredCount: 2` on dev with (a) `lifecycle.pass.claimed`
   showing exactly one winner per window per pass, (b) a fleet task-definition refresh
   draining with the N = 2 backend, (c) a connect/disconnect pair issued concurrently against
   the two tasks leaving the credential in the disconnected state.
5. Prod promotion once the dev proof passes — authorized per the 2026-09-13 goal reset.

---

## 11. Testing

- `try_claim_pass`: two concurrent claimants in the same window, one winner; a claimant in
  the next window wins; a claimant exactly `interval_s` after the first wins (the `<=`
  boundary — a regression test for the Task 3/4 review finding); a DynamoDB fault returns
  `False` (fail closed, the inverse of `try_claim_daily_system_notice` — assert the
  difference explicitly so nobody "fixes" it).
- Worker loop: with the lease stubbed to refuse, no pass body runs and the loop still ticks;
  with it stubbed to admit, each pass runs exactly once.
- `owner_lifecycle_guard` on the connections routes: a disconnect and a callback for the
  same owner serialize; the nested `@serialize_owner_lifecycle` call inside the write section
  does not deadlock; a second owner is not blocked.
- Admin idempotency: same key twice → one execution, second is 409; no key → two executions;
  two different handlers with the same key → both execute.
- `run_completed` emits once when `end_run` returns a row and not at all when it returns
  `None`.
- *(Correction, Task 7 step 1 — the original draft said "Delete
  `tests/unit/services/test_idempotency.py` with the module; move nothing." No such file
  ever existed: `core/services/idempotency.py` had no dedicated test file at all.)* The
  replacement, `webhook_dedup.idempotency()`, is covered by the new
  `tests/unit/services/test_admin_idempotency_claim.py`; there is nothing to delete.

---

## 12. Success criteria

1. `main.py` starts no loop whose *work* is duplicated at N ≥ 2 (the SQS consumer and the
   liveness gauge are the two deliberate exceptions, both documented in §3).
2. `grep -rn "asyncio.Lock\|defaultdict(asyncio.Lock)" apps/backend/core/services` returns
   only guards listed in §3 "Accepted at N ≥ 2" *(corrected, Task 7 step 1: that list now
   includes the five additional guards found on the tree — `live_events.py`,
   `paperclip_session_cookie.py`, `seller_display.py`, `curator.py`,
   `paperclip_admin_session.py` — so this criterion is satisfiable as written)*.
3. `core/services/credentials/connection_lock.py` and `core/services/idempotency.py` do not
   exist.
4. P12 fires when one of two dev instances is stopped.
5. A dev fleet task-definition refresh drains 100 % with `desiredCount: 2`.

---

## 13. Out of scope

The update-apply state machine unless C2 is overruled (§5); `try_claim_owner_deploy` and the
provisioning claims (§3 "Kept"); replacing `owner_lifecycle_guard`'s EFS implementation with
DynamoDB; the authored workspace on EFS (working-trees spec R1); autoscaling policy beyond
C5's precondition; the `tests/chaos/fis` drills deleted with B's chat driver — rewriting them
on the HTTP contract is a separate owner decision.
