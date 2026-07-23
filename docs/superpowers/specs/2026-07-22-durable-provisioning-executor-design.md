# Durable Provisioning Executor — Step Functions Design

**Decision: AWS Step Functions (Standard) orchestrating a one-shot ECS RunTask
"provisioner" that runs the existing backend provisioning code.** No Lambdas on
the happy path, no code rewrite — the state machine replaces the *execution
environment*, not the provisioning logic.

Status: DESIGN APPROVED PENDING HUMAN GO ON THE PLAN. Companion plan:
`docs/superpowers/plans/2026-07-22-durable-provisioning-executor.md`.

---

## 1. The problem (one root cause)

Container provisioning is a minutes-long, multi-step, side-effecting job
(IAM/AIP mint → ECS service create/update → EFS config write → start → up to
~12 min health wait) executed as **fire-and-forget asyncio tasks inside the
stateless FastAPI web process**, triggered by clients polling
`GET /container/status`. The process it runs in is deployed, scaled, and
Spot-killed independently of the jobs it happens to be running.

Everything below exists to compensate for that one fact:

| Compensating mechanism | Where | What it compensates for |
|---|---|---|
| Provision lease + fencing token (`try_claim_provision`, `update_container_leased`, `ProvisionSuperseded`) | `container_repo.py:130-274` | No managed single-flight; concurrent polls double-dispatch (the prod AIP-split incident) |
| Stale-lease heuristic (`PROVISION_CLAIM_STALE_S=300`, `is_provision_claim_stale`) | `container_repo.py:19,311` | A SIGKILL'd worker can't say "I died"; wall-clock inference is the only detector |
| ECS liveness probe guarding the heuristic (`has_live_service`) | `ecs_manager.py:1741` | The heuristic false-positives during the legitimate ~12-min boot window |
| Poll-driven error auto-retry + stale-recovery dispatch branch | `container.py:277-352` | No managed retry; the client's poll is the only recurring event |
| `release_provision_to_error` self-heal | `container_repo.py:277` | Failed runs must hand the row back to the poll loop |
| Unmanaged `_await_running_transition` poller (no timeout, orphaned by every deploy) | `ecs_manager.py:2325` | No durable step execution; the provisioning→running transition needs *something* watching, and `resolve_running_container` on the next poll re-heals when the poller dies |
| On-boot reconciler `_resume_provisioning_transitions` | `main.py:258` (run in lifespan at `main.py:411`) | Re-attaches the orphaned transition pollers after every deploy — a startup sweep that exists only because the pollers die with the process |
| `maybe_resume_stuck_conversion` | `container.py:270` | Same smell on the conversion path — its fire-and-forget task dies and only the poll resurrects it |

Confirmed prod incident class: `asyncio.create_task` with no done-callback →
a raise is an invisible "Task exception was never retrieved" → no container,
infinite spinner, no error. PR-1 (`fix/provisioning-honest-failure-state`)
makes the *reporting* honest; this design fixes the *executor*.

## 2. Why Step Functions (and why not the alternatives)

Two alternatives were evaluated and rejected:

**B — provision-jobs DDB table worked by `run_scheduled_worker`.** Cheapest
option (~1/4 the effort, zero new infra; the worker loop and `pending-updates`
pattern already exist — see `_paperclip_provision_retry_pass`). It fixes
swallowed failures (durable job row) and improves dead-worker recovery (any
backend task picks up the job). **Rejected because it leaves execution inside
the deploy blast radius**: every backend deploy still SIGKILLs in-flight
provisions mid-step, and — decisively — the stale-lease heuristic must
*survive* in B (a SIGKILL'd worker still can't mark its job dead; another
worker must infer it from a heartbeat clock). B relocates the smell; it does
not delete it. It also adds up to 60s pickup latency (worker tick) where today
dispatch is immediate.

**C — status quo + PR-1.** PR-1 is necessary regardless (honest reporting)
but changes nothing about execution: provisions still die with deploys, retry
is still client-poll-driven, the lease/staleness/fencing machinery all stays.
C is the "do nothing structural" baseline, not a fix.

**A — Step Functions** is the only option that removes provisioning from the
deployable process entirely, which is the only way the wall-clock liveness
heuristic actually gets *deleted* rather than relocated: SFN's execution state
is authoritative ("is this provision alive?" becomes `DescribeExecution`, not
clock math), retries/timeouts/backoff are declarative, and every provision
becomes a browsable execution history — the durable job record and the
observability story in one primitive. The known caveat — Isol8 provisions
long-lived per-user ECS *services*, so `ecs:runTask.sync` can't natively run
the *user container* — is answered by pointing `runTask.sync` at the
**provisioning job itself**, which is a textbook one-shot task.

Honest costs, stated up front: this is the **first Step Functions state
machine in the codebase** (new CDK surface, new IAM, LocalStack-Pro SFN
emulation for local dev — Pro is already required and emulates SFN); a
dual-run window where both executors exist; and SFN does **not** fix the
Paperclip-seed 422/orphan class (§7).

## 3. Architecture

```
GET /container/status (unchanged trigger)
   └─ dispatch_provision(owner_id)
        A. Row has a stamped (execution_name, claim_id) and status is
           retryable → StartExecution(stamped name, stamped input):
             • execution still RUNNING → idempotent success, SAME ARN
               returned, no new execution (StartExecution is idempotent for
               STANDARD workflows on identical name+input) — this is the
               cross-poll/cross-instance single-flight, for free
             • execution CLOSED → 400 ExecutionAlreadyExists → fall to B
        B. Fresh dispatch: try_claim_provision(owner_id) (unchanged atomic
           gate) + stamp execution_name = provision-{owner}-{claim_id[:8]}
           in the same conditional write → StartExecution(new name)
           (a crash between claim and start is healed by any later poll
           re-entering at A with the stamped name)
             ▼
   Step Functions: isol8-{env}-provision  (Standard, JSONata)
   ┌────────────────────────────────────────────────────────────┐
   │ RunProvisioner  (ecs:runTask.sync, TimeoutSeconds 1800,     │
   │                  provisioner task-def)                      │
   │   └─ one-shot Fargate task, BACKEND IMAGE, command =        │
   │      python -m scripts.provision_runner --owner … --claim … │
   │      • resolve_provider_choice → provision_user_container   │
   │        (same code, same claim_id fencing)                   │
   │      • waits for running+healthy SYNCHRONOUSLY              │
   │        (_await_running_transition logic, now bounded)       │
   │      • writes the same containers-row states → the PR-1     │
   │        /container/status contract is emitted verbatim       │
   │      • exit 0 ok · 10 deterministic-fail · 11 transient ·   │
   │        20 superseded (deliberate door took the lease)       │
   │   exit 0 → Succeed                                          │
   │ Catch States.TaskFailed / States.Timeout / AmazonECS.*      │
   │   (a non-zero essential-container exit FAILS the .sync      │
   │    state with States.TaskFailed; the Cause JSON carries     │
   │    Containers[].ExitCode + StoppedReason — verified §11)    │
   │   → Classify (Choice, JSONata $parse of Cause):             │
   │     exit 20            → Succeed (write nothing — the row   │
   │                          belongs to the superseding run)    │
   │     exit 10            → MarkFailed  (native DDB UpdateItem:│
   │                          status=error, retryable=false,     │
   │                          claim-conditioned) → Fail          │
   │     exit 11 / SIGKILL / launch failure / States.Timeout,    │
   │       attempt < 3      → MarkRetrying (native DDB UpdateItem│
   │                          substatus=error_retrying,          │
   │                          claim-conditioned) → Wait(30s·2ⁿ)  │
   │                          → RunProvisioner                   │
   │       attempt ≥ 3      → MarkFailed (retryable=true) → Fail │
   └────────────────────────────────────────────────────────────┘
```

If the JSONata `$parse`-the-Cause Choice proves brittle in practice, the
fallback is the AWS-blessed pattern (a tiny classify Lambda in the Catch
route — see the `aws-samples/retryable-ecs-run-task-step-functions`
reference); prefer the Lambda-free shape first.

In-machine retries interact with the poll safely: while the machine retries,
the row shows `error` + `error_retrying` (honest per PR-1) and any poll's
dispatch lands on branch A → idempotent no-op against the running execution.
The machine's `MarkFailed`/`MarkRetrying` DDB writes are
**claim-conditioned** (`provision_claim_id = input.claim_id`), preserving the
existing "never clobber a superseding run's row" rule even from SFN's side.

Key properties:

- **The worker is outside the deploy blast radius.** The provisioner task is
  launched from a pinned task-definition revision; backend deploys do not touch
  it. A Spot/infra kill of the provisioner is *seen by SFN* (the `.sync` task
  fails) and retried by policy — no heuristic, no orphan.
- **Zero Lambdas in v1.** Status writes on the SIGKILL path use SFN's native
  DynamoDB `UpdateItem` integration (`MarkFailed`/`MarkRetrying` states) —
  these are the backstop for "runner died before it could write its own
  terminal state"; on clean failures the runner has already written the row
  and the SFN write is an idempotent overwrite of the same values.
- **Same code, same contract.** The runner imports and calls
  `provision_user_container(claim_id=…)` unchanged. Every row transition the
  PR-1 contract reads (`status=error` never masked, `error_retrying`
  substatus, `retryable` cause, `last_error`) is produced by the same lines of
  Python that produce it today. Clients cannot tell the executor changed —
  which is the point: this is a producer swap behind a stable interface.
- **Crash-safe dispatch.** The claim write and the execution *name* land in one
  conditional `UpdateItem`; `StartExecution` is idempotent by (name, input)
  for Standard workflows — same name+input against a RUNNING execution
  returns the same ARN; against a CLOSED one it returns
  `ExecutionAlreadyExists` (400), which is the dispatcher's signal to mint a
  fresh claim (branch B). A backend crash between claim and start is healed
  by any later poll re-entering branch A. Execution names embed the claim_id
  so the 90-day name-reservation window never collides across attempts, and
  names use only `[0-9A-Za-z-_]` (SFN naming constraint; Clerk owner_ids —
  `user_…`/`org_…` — are already in-alphabet).
- **Health wait becomes bounded.** `_await_running_transition` today loops
  forever with no timeout; inside the runner the same logic runs under the
  state's 30-minute per-attempt timeout, and `States.Timeout` routes to the
  transient path. The circuit-breaker `rolloutState=FAILED` detection maps to
  exit 10.

### Provisioner task definition (codebase-verified specifics)

A **separate lean task-def family** (`isol8-{env}-provisioner`) defined in
**service-stack.ts** next to the backend task-def, because everything it needs
already lives there — no new cross-stack exports (a hard-won constraint in
this repo):

- **Same ECR image** as the backend; **same task role** (`this.taskRole` —
  the provisioning path uses its EcsManagement/EcsTaskDefinition/PassRole/
  Secrets/EFS-access-point/KMS/CloudMap/S3 statements plus the
  `BackendOwnerBedrockManage` managed policy) and **same execution role**
  (the `secrets:` block below needs its SecretsManager+KMS grants).
- **Same env + secrets block.** `core.config.Settings` is populated purely
  from env vars (no boot-time Secrets Manager fetch — `config.py` has zero
  boto3), so the provisioner must receive the backend's env verbatim.
  Implementation: factor the backend container-def's env/secrets maps into a
  shared builder used by both container definitions, so they cannot drift.
  Notables the runner actually consumes: `ECS_*`, `EFS_*`, `CLOUD_MAP_*`,
  `DYNAMODB_TABLE_PREFIX`, `PAPERCLIP_INTERNAL_URL`
  (`http://paperclip.{namespace}:3100` — `write_user_configs` builds its own
  httpx client on it at `ecs_manager.py:2573`), `ENCRYPTION_KEY` (required
  by the chatgpt_oauth pre-stage — `oauth_service.get_decrypted_tokens`;
  BYOK notably does NOT need it, it flows as a `valueFrom` secret ARN),
  `PAPERCLIP_SERVICE_TOKEN_KEY`, bedrock-gate/AIP settings.
- **Same security group** (`serviceSg`) — **mandatory, not convenience**: the
  per-user container SG's ingress rule (`ContainerFromServiceIngress`,
  `service-stack.ts:197-204`) allows all-TCP *only from `serviceSg`*, and the
  runner's health wait TCP-probes the container on port 18789
  (`GATEWAY_PORT`, `ecs_manager.py:40`, probe at `:1477`). Paperclip:3100 is
  VPC-CIDR-open (`paperclip-stack.ts:191-195`) so any SG would do for that
  half, but 18789 would silently break in a fresh SG.
- **Same EFS volume + `/mnt/efs` mount** (`write_user_configs` writes the
  owner's workspace/config on EFS).
- **No ALB wiring, no container healthCheck** (a batch job must not be killed
  for not serving HTTP). Sized 1 vCPU / 2 GB dev-parity. Image tag pinned by
  the same deploy that pins the backend's — runner code moves in lockstep
  with backend deploys.

### The runner and the detached-poller problem

`provision_user_container` internally fires **detached**
`asyncio.create_task(self._await_running_transition(…))` pollers
(`ecs_manager.py:1062` inside `start_user_service`, plus `:2132/:2194/:2218`
in the restart/redeploy/starting branches). In the backend process those
survive the request; in a one-shot runner they would die at process exit and
strand the row at `provisioning` — exactly the failure the on-boot
reconciler exists to repair. The runner therefore:

1. calls `provision_user_container(claim_id=…)`;
2. then **awaits `_await_running_transition(owner_id)` in the foreground**
   until the row leaves `provisioning` (the loop's own exit conditions:
   running+healthy → it writes `running`; rollout FAILED → it writes
   `error`; external state change → returns). Duplicate detached pollers
   racing it are benign (all writes are condition-guarded, and they exit as
   soon as status leaves `provisioning`); `asyncio.run` cancels any
   stragglers at exit, which the poller handles (`CancelledError` re-raise).
3. maps the final row state to the exit code: `running` → 0; `error` with a
   deterministic cause (bad config, 4xx, unknown provider) → 10; `error`
   from rollout-FAILED / ECS 5xx / throttle / Spot → 11 (a tripped circuit
   breaker can be capacity as easily as image, and attempts are bounded, so
   transient is the safe classification); `ProvisionSuperseded` → 20.

### Triggering model (unchanged, simplified)

`GET /container/status` remains the canonical trigger. What changes inside it:

- **no-row / error(retryable) / stopped-wake branches** call
  `dispatch_provision` instead of `asyncio.create_task(background_provision)`.
- **Transient retry moves in-machine**: while an execution is retrying, the row
  says `error` + `error_retrying` (written by runner/SFN), and the poll branch
  does *not* re-dispatch (the claim write refuses anyway — status is not
  claimable mid-execution because the lease is fresh… see §5 for what replaces
  the staleness clause). After the machine exhausts retries and lands terminal
  `error` + `retryable=true`, the next poll may dispatch a fresh execution —
  same PR-1 semantics, but short transients now retry in seconds-to-minutes
  without depending on a client keeping a tab open.
- **`error` + `retryable=false`**: never re-dispatched, exactly as PR-1
  specifies.
- Wake of a parked/stopped container rides the same machine (it is the same
  `background_provision` path today — scenario "desired=0 → scale up").

`POST /container/provision` and `/retry` currently run the whole provision
synchronously inside the HTTP request — a third unmanaged execution site
(dies with the request worker). They migrate to `dispatch_provision` + return
the existing pollable `{"status": "provisioning"}` shape (v2).

## 4. Doors and supersede semantics (call-site-verified)

The door taxonomy is better than the folk model. **Everything that goes
through `background_provision` rides the state machine automatically in v1**,
because v1 swaps `background_provision`'s internals for `dispatch_provision`.
That set (verified call sites): the status-poll auto-retry
(`container.py:350`), `POST /container/wake` AND the silent chat-send wake
(`container_wake.py:83`, `:128`), first provision via `ensure_user_ready`
(`user_provision.py:211` — including its two indirect entry wrappers,
`_autoprovision` in `paperclip_proxy.py:1054` and `_provision_after_gate` in
`teams/agents.py:380`), provider change (`billing_service.py:1005`), and
admin reprovision + the Stripe trial→paid conversion's final dispatch (both
via `reprovision_for_user` → `ecs_manager.py:1297`; the conversion's
drain-wait pre-steps stay in-process and out of scope, but its terminal
provision becomes durable for free).

Only **three synchronous doors** call `provision_user_container` directly
with a lease takeover: `POST /provision` (`container.py:573`), `POST /retry`
(`container.py:627`), and `POST /recover` (three branches,
`container_recover.py:143/177/218`). The dev-only `POST /debug/provision`
bypasses all of this by calling the ECS primitives directly and stays as-is.

- **v1 (everything via background_provision):** the three synchronous doors
  keep the direct path. Interop is exactly today's fencing — the runner holds
  a claim_id and its checkpoints raise `ProvisionSuperseded` when a door
  takes over; the runner exits 20 (superseded) and the machine ends without
  touching the row — same "write nothing over the winner" rule as today.
- **v2:** doors become `StopExecution`(any running execution for owner) → wait
  for the execution to reach a terminal state (bounded `DescribeExecution`
  poll) → `takeover`-style claim → `StartExecution`. Important verified
  caveat: on execution stop, Step Functions' cancellation of the in-flight
  `.sync` ECS task is documented **best-effort** (§11) — so the door either
  keeps a minimal fence (the runner's claim-conditioned checkpoints, exactly
  today's `ProvisionSuperseded` mechanism) or issues its own `ecs:StopTask`
  on the runner task as a double-tap. Decide in the v2 PR; until then the
  fence stays, which also means the §5 fencing deletion is contingent on
  choosing the double-tap. Doors are rare and human-triggered; seconds of
  stop-wait are acceptable.
- **Out of scope (v3 candidate):** the trial→paid conversion's 90s-drain
  reprovision + `maybe_resume_stuck_conversion` is the same smell and a
  natural future rider (drain-wait as a state), but it has extra EFS-WAL
  single-writer semantics and is deliberately not in this program.

## 5. What gets deleted (end-state, after v2)

- `PROVISION_CLAIM_STALE_S`, `is_provision_claim_stale`, and the stale clause
  inside `try_claim_provision` — liveness is `DescribeExecution` /
  the machine's own Catch, never clock math. The claim shrinks to a pure
  status-transition gate (still needed: it is the cross-instance
  double-dispatch fence for *starting* executions, and DDB conditional writes
  remain the right primitive for that).
- `has_live_service` — exists solely to guard the heuristic.
- The status poll's stale-recovery branch and its dispatch plumbing in
  `container.py`.
- `background_provision`'s claim/release/error bookkeeping (the function
  becomes `dispatch_provision`, ~15 lines).
- The detached `_await_running_transition` poller and every
  `asyncio.create_task` of it (folded synchronously into the runner), and
  with it the on-boot `_resume_provisioning_transitions` reconciler in
  `main.py` — its only job is re-attaching those orphaned pollers after a
  deploy.
- (v2, contingent) `takeover_provision` + `update_container_leased`
  checkpoints + `ProvisionSuperseded` — supersede becomes
  StopExecution-then-start, but because `.sync` cancellation is best-effort
  (§4, §11), this deletion only happens if the v2 door adds the explicit
  `ecs:StopTask` double-tap; otherwise the fence stays as the last line
  against a zombie runner task.
- The "provisions orphaned by deploy" failure class, and with it the need for
  clients to babysit retries.

What it adds: one CDK state machine + one lean task-def + `provision_runner.py`
(thin CLI over existing code) + `dispatch_provision` + alarms. Net LOC is
likely near-zero; net *concept count* drops (no leases-with-staleness, no
fencing after v2, no unmanaged pollers).

## 6. Failure & observability story

- Every provision = one execution: browsable step-by-step history with inputs,
  outputs, exit codes, timing — the durable job record that today simply does
  not exist. The prod incident ("Paperclip 422 swallowed → infinite spinner")
  class of *container* failures becomes impossible to hide: an execution
  either Succeeded, Failed (with cause), or is visibly Running.
- Alarms (observability-stack): `ExecutionsFailed`, `ExecutionsTimedOut`,
  execution duration p99. These complement (not replace) the existing
  `container.provision {status}` EMF metrics, which the runner keeps emitting
  from the same code paths — the W1 provision-error-rate alarm keeps working
  through the migration untouched.
- SIGKILL of the runner: SFN sees the task fail → `MarkRetrying`/`MarkFailed`
  native-DDB writes keep the row honest even though the dying process wrote
  nothing. This is the exact scenario the stale-lease heuristic existed for,
  handled without a clock.

## 7. What this does NOT fix (scope honesty)

- **The Paperclip seed** (422/duplicate-email orphans, CEO-less companies).
  It stays a synchronous backend call in `ensure_user_ready`; PR-1 owns its
  honesty (`teams_status` degraded signal) and its bugs are cross-service
  state problems no orchestrator fixes. Explicitly out.
- The org create-claim machinery in `user_provision.py` (different resource,
  different race) — untouched.
- Gateway-pool per-task blindspot (#1364) — already fixed, unrelated.
- Conversion drain (§4, v3 candidate).

## 8. Cost

- SFN Standard: $25/M state transitions. ~10–15 transitions per provision
  (more with retries). At even 50k dispatches/month (signups + error retries +
  parked-trial wakes) → **≈ $15–20/month**. If wake volume ever makes this
  material, wakes can drop to a scale-up-only fast path — not a v1 concern.
- Provisioner Fargate task: 0.5 vCPU/1 GB × 3–15 min ≈ **$0.002–0.01 per
  provision** — noise.
- Engineering: the real cost. Estimate **3 PRs / roughly 1.5–2 weeks**
  (infra + runner/dispatch behind a flag, doors migration + deletion pass,
  bake time between them). Not a weekend patch; priced accordingly.

## 9. Migration / dual-run

Flag: `PROVISION_EXECUTOR = "inline" | "sfn"` (settings, per-env).

1. **PR-A (infra + runner, dark):** CDK state machine + provisioner task-def
   in **service-stack.ts** (greenfield SFN — no existing constructs in the
   repo; `aws-cdk-lib` already bundles the modules) + `provision_runner.py` +
   `dispatch_provision` behind the flag (default `inline` everywhere).
   LocalStage deploys ServiceStack, so the construct must synth under
   `cdklocal`; local keeps `inline` regardless (§11 LocalStack caveat).
   PR-1's contract tests become the shared conformance suite both executors
   must pass.

   **Flip prerequisites — ALL must hold before step 2:**
   1. ✅ SATISFIED (2026-07-23): PR #1385 (honest-failure-state) merged, this
      branch rebased on it, classifier consolidated into
      `classify_provision_error`. Without #1385's retryable gate in the
      router, `MarkFailedDeterministic`'s `retryable=false` is inert and
      deterministic failures re-dispatch forever — each cycle now costing a
      Fargate launch + a Failed execution, not just a swallowed asyncio task.
   2. The provision alarms' actions wired to the page topic
      (`alertPageTopicArn` already exists in service-stack); alarms with no
      action are dashboards, not pages.
   3. The step-2 validation scenarios below run green, including the two
      added for this design: transient-failure-with-tab-open and the raw
      provisioner-command smoke.
2. **Dev flip:** `sfn` in dev. Validate: fresh signup, error-retry (inject a
   transient), deterministic failure (bad config), deploy-during-provision
   (the headline scenario — kill/deploy the backend mid-provision and watch
   the execution complete anyway), parked-trial wake, deliberate door
   supersede (door wins, runner exits superseded), transient failure with
   the owner's tab open polling (~3s polls against a row reading
   error/error_retrying — exercises the Branch-A error-row dedupe: exactly
   ONE execution must exist afterwards), and a raw provisioner-command smoke
   (`aws ecs run-task` with the provisioner task-def + the exact command
   override the dispatcher emits, or equivalent — proves the `uv run python
   -m scripts.provision_runner` transport outside the state machine).
3. **Prod flip** after a multi-day dev soak. Rollback = flip the flag back —
   the inline path stays intact until the deletion pass, and both paths speak
   the same lease protocol so they can coexist across the flip instant.
4. **PR-B (doors, v2):** migrate the three synchronous doors — `/provision`,
   `/retry`, `/recover` — to StopExecution-then-dispatch (admin,
   provider-change, wake, and the conversion's final dispatch already ride
   v1 via `background_provision`).
5. **PR-C (deletion pass):** remove the flag, the inline executor, the
   staleness/fencing machinery per §5. Delete this doc's plan per repo policy.

## 10. Strongest argument against this design

Option B delivers the two user-visible fixes (no swallowed failures, recovery
after worker death) for a quarter of the effort with zero new infrastructure,
and provisions are rare enough that execution history is a nice-to-have. If
the team's appetite for new AWS surface area is low this quarter, B is the
defensible cheap answer — and nothing in B forecloses doing SFN later.

It is still the wrong call here, for one structural reason: **B keeps the
minutes-long job inside the process we redeploy many times a day.** Isol8's
backend deploy cadence is the *primary* killer of in-flight provisions (every
deploy cycles the only task at 200%/100%), so an executor that survives deploys
isn't gold-plating — it targets the dominant failure source. B would keep
interrupting provisions and keep the wall-clock heuristic that PR-1's own plan
calls "the smell that necessitates the liveness check." Deleting that smell is
the stated goal of this rearchitecture; only A does it.

## 11. Verified AWS behavior (sources checked 2026-07-22)

Load-bearing claims verified against AWS docs / AWS sample code — do not
re-litigate without re-checking the source:

- **`ecs:runTask.sync` fails the Task state on non-zero essential-container
  exit** with `States.TaskFailed`; exit 0 succeeds the state. The error
  `Cause` is a JSON string carrying the task object (`Containers[].ExitCode`,
  `StoppedReason`, `StopCode`). Corroborated by the state-machine shape of
  `aws-samples/retryable-ecs-run-task-step-functions` (RUN_JOB integration +
  `addCatch` routing to an exit-code classifier; **no** post-success Choice).
  Launch-level failures (non-empty `Failures` in the RunTask response, e.g.
  capacity) fail the state as `AmazonECS.Unknown`
  ([connect-ecs](https://docs.aws.amazon.com/step-functions/latest/dg/connect-ecs.html)).
- **`StartExecution` idempotency (Standard):** same name + same input as a
  RUNNING execution → succeeds, returns the original ARN. Closed execution or
  different input → `400 ExecutionAlreadyExists`. Names reserved 90 days after
  close; max 80 chars, `[0-9A-Za-z-_]` recommended for CW Logs
  ([API_StartExecution](https://docs.aws.amazon.com/step-functions/latest/apireference/API_StartExecution.html)).
- **`.sync` abort is best-effort cancellation:** when an execution is stopped,
  "Step Functions will make a best-effort attempt to cancel the task" — not a
  guarantee (IAM gaps / outages can leave the job running)
  ([connect-to-resource](https://docs.aws.amazon.com/step-functions/latest/dg/connect-to-resource.html)).
  Drives the v2 double-tap decision (§4).
- **Native DynamoDB integration** supports `GetItem`/`PutItem`/`UpdateItem`/
  `DeleteItem` request-response with full API parameter shape (Update/
  Condition expressions), so `MarkFailed`/`MarkRetrying` need no Lambda.
  256 KiB task I/O cap — irrelevant at our payload sizes
  ([connect-ddb](https://docs.aws.amazon.com/step-functions/latest/dg/connect-ddb.html)).
- **State-machine role IAM for `.sync` ECS:** `ecs:RunTask` on the task-def
  (revision-qualified or `*` for latest), `ecs:StopTask` + `ecs:DescribeTasks`
  (`*`, only SFN-started tasks are stoppable), the EventBridge managed rule
  (`events:PutTargets/PutRule/DescribeRule` on
  `rule/StepFunctionsGetEventsForECSTaskRule`), and `iam:PassRole` for the
  provisioner's task role AND execution role
  ([ecs-iam](https://docs.aws.amazon.com/step-functions/latest/dg/connect-ecs.html)).
- **`Overrides.ContainerOverrides[].Command`** accepts values from state input
  (JSONata/JSONPath), which is how `--owner`/`--claim` reach the runner
  (connect-ecs, "Passing Data to an Amazon ECS Task").
- **CDK deploy mid-execution replaces the provisioner task-def revision.** An
  in-flight execution is pinned (at StartExecution) to the definition that
  references the old revision, which CloudFormation deregisters on update —
  so that execution's RETRY attempts fail RunTask and exhaust via the
  States.ALL catch (`MarkFailedExhausted`, retryable=true), healing through
  a fresh execution on the next poll against the new revision. The
  already-running provisioner task itself survives the deploy (RunTask tasks
  are not part of any service rollout); only not-yet-started retry attempts
  are affected. Accepted: deploys are frequent but mid-retry windows are
  seconds wide, and the failure self-heals through the normal poll path.

Not yet verified (flagged for PR-A, not assumed): LocalStack's SFN provider
coverage of `ecs:runTask.sync` — the local stage keeps
`PROVISION_EXECUTOR=inline` until proven, so local dev never depends on it.
