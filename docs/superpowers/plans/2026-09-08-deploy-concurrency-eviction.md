# Deploy concurrency: pending prod promotions are silently evicted

Status: **shipped** (`queue: max`). Delete this doc once it has ridden a promotion.

## 1. The failure

`.github/workflows/deploy.yml` (generated from `apps/infra/lib/app.ts`):

```yaml
on:
  push: { branches: [main] }
  workflow_dispatch: {}
concurrency:
  group: lightbulb-deploy
  cancel-in-progress: false
```

`cancel-in-progress: false` protects a run that is already `in_progress`. It does
**not** protect a run that is still *pending*. GitHub's documented default is
`queue: single` — "at most one job or workflow run can be `pending` in the
concurrency group" — and "any existing `pending` job or workflow in the same
concurrency group will be canceled and the new queued job or workflow will take
its place."

Prod ships only on `workflow_dispatch`. The pipeline is **runner-bound**: 67 jobs
against roughly **two** concurrent job slots on the self-hosted `isol8-wsl`
runner, 10–34 minutes of execution per run, and the group is effectively
saturated (measured runs start within seconds of the previous one finishing). So
a promotion routinely sits pending for minutes, and any merge to `main` inside
that window fires a push run that evicts it. Nobody is told.

### Evidence, 2026-09-07/08

`gh run list --workflow=deploy.yml`, with job counts:

| run | event | created | outcome | jobs run |
|---|---|---|---|---|
| 34173158922 | workflow_dispatch | 00:23:33 | cancelled | **0** |
| 34177495429 | workflow_dispatch | 01:41:32 | cancelled | **0** |
| 34177543989 | push | 01:42:27 | cancelled | **0** |
| 34177614173 | workflow_dispatch | 01:43:43 | cancelled | **0** |
| 34176328884 | push | 01:20:42 | cancelled | 67 |

**A cancelled run that executed ZERO jobs is the eviction signature.** Three
promotions of a live prod defect fix died that way in ninety minutes. The one
cancelled run that *did* execute jobs was cancelled by a human — `cancel-in-progress:
false` did its job there.

Two things make this recur rather than get noticed and fixed:

- **The cost is externalised.** The merger's run is the one that survives; the
  promoter's is the one that dies. Nothing in either UI says a run was evicted —
  it just reads `cancelled`.
- **`queued` is ambiguous.** A run at `status: queued` may be waiting on the
  concurrency group *or* on a runner, and `gh run list` cannot tell them apart.
  The only reliable discriminator is the job count, after the fact.

## 2. The idea under evaluation, and why it is rejected

> Give `workflow_dispatch` its own concurrency group so a promotion cannot be
> evicted by a merge.

**Rejected.** A prod dispatch deploys **dev first** — every prod job `needs:` the
full dev stage, deliberately, so a promotion cannot skip dev. Split the groups and
a push run and a promotion can deploy the *same dev CloudFormation stacks* at the
same time.

### Does that wedge CloudFormation?

Mostly no, and this is worth stating precisely because it is the question that
decides the idea:

- The deploy jobs use `aws-actions/aws-cloudformation-github-deploy@v1`, i.e.
  `CreateChangeSet` → `ExecuteChangeSet`.
- A stack executes one operation at a time. A second `CreateChangeSet`/
  `ExecuteChangeSet` against a stack in `UPDATE_IN_PROGRESS` is rejected with a
  `ValidationError` / `InvalidChangeSetStatus`. Executing a change set also
  obsoletes the stack's other change sets, so a stale one cannot be executed
  afterwards.
- `UPDATE_ROLLBACK_FAILED` comes from a rollback that cannot restore a resource,
  not from a rejected concurrent update. **The concurrent-update case fails
  cleanly — a red build, not a wedged stack.**

So CFN's own guard rails hold. The reasons to reject split groups are the ones
*above* CloudFormation:

1. **Whole-run atomicity is lost.** Today one run completes before the next
   starts, so the account always converges on one commit. Interleaved runs mean a
   stack from the older commit can land *after* a stack from the newer one —
   deploying an old template over a new one. That is a silent regression, not a
   red build.
2. **Half-applied deploys.** A run whose 4th stack is rejected leaves stacks 1–3
   updated and 5–13 never attempted. Infra then matches no commit. This repo has
   already been burned by the shape of this (#1817: a dev stack rolled back on
   every deploy and prod was silently skipped for hours).
3. **Runner starvation.** Two runs sharing ~2 job slots each halve, so both runs
   roughly double in wall-clock — which *widens* the pending window the fix was
   meant to close.

## 3. Does job-level concurrency rescue it? No.

The composite proposal — workflow-level group keyed by event so a dispatch is
never evicted by a push, plus a job-level group shared across both on the jobs
that mutate shared infrastructure — fails on all three of its possible shapes:

- **One shared job-level group for all 25 deploy jobs.** Serialises those jobs
  *within a single run* too, destroying the parallelism the stack DAG already
  expresses. Every deploy gets much slower.
- **Per-stack job-level groups** (`lightbulb-cfn-<stack>`). Keeps intra-run
  parallelism, but this is precisely failure (1) above, made systematic: run A
  and run B interleave per stack, and the `needs:` DAG only orders stacks *within*
  a run. Old-over-new template ordering becomes the normal case rather than a
  race.
- **Any of the above with the default `queue: single`.** The eviction simply
  moves down a level: a *pending job* is evicted by the next arrival in the same
  job-level group. The dispatch run then has a cancelled job mid-DAG, every
  downstream job skips, and prod never deploys — the same silent loss, now
  reported as a partially-green run, which is strictly harder to diagnose than a
  0-job cancellation.

Job-level concurrency does not compose here. It moves the eviction or it trades a
clean failure for a silent one.

## 4. The fix that does work: `queue: max`

GitHub shipped a larger-queue option on 2026-05-07 ("GitHub Actions concurrency
groups now allow larger queues"):

- `queue: single` (default) — at most one pending run per group.
- `queue: max` — **up to 100** pending runs, drained **FIFO** by the time each
  started waiting on the group. Beyond 100, additional runs are cancelled.
- `queue: max` with `cancel-in-progress: true` is a **workflow validation error**.
  `false` (what the pipeline renders) is explicitly supported.

```yaml
concurrency:
  group: lightbulb-deploy
  cancel-in-progress: false
  queue: max
```

**The group does not change.** One run at a time still touches CloudFormation, so
none of §2 or §3 applies. All that changes is what happens to the runs waiting:
they wait instead of dying.

### Validated empirically before shipping

The blast radius of an unrecognised key is total — an invalid workflow file blocks
every dev and prod deploy — and `deploy.yml` is only validated when it runs, i.e.
post-merge. So it was validated on a throwaway `push`-triggered workflow on this
branch, on `ubuntu-latest` (no contention with the starved self-hosted box), with a
control:

| probe | result |
|---|---|
| `concurrency: {group, cancel-in-progress: false, queue: max}` | run **34178856622 — success** |
| same, plus a bogus `qeueu: bogus` key | run **34178885403 — failure, "workflow file issue"** |

The control proves the schema is strict about unknown keys under `concurrency`,
so the first probe's success proves `queue` is recognised on this repo, this plan
and this visibility. Both probe commits were then dropped from the branch.

### Tradeoff, deliberately accepted

Eviction was also doing something useful: **coalescing**. A burst of merges used
to collapse into one or two dev deploys, because a later run deploys the same
`main` HEAD anyway. With `queue: max` every merge gets its own run. Tonight's
burst (5 pushes + 3 dispatches between 01:20 and 01:55) would have drained in
~110 minutes at ~14 min/run instead of executing two or three runs.

Nothing is *lost* — dev still converges on HEAD, and the queue is FIFO and
visible — it costs self-hosted runner time and queue depth. That is the right
trade against silently losing a promotion: determinism over speed.

The upgrade path, if the backlog ever bites: have `Build-Synth` detect that its
`github.sha` is no longer `main`'s tip and skip the rest of the run, restoring
coalescing *inside* `queue: max`. It is deliberately not done here — it needs an
`if:` on all ~66 jobs (Build-Synth and `Assets-*` have no `jobSettings` hook, so
it would be a JsonPatch per job name) and it introduces a new way for a deploy to
silently not happen. Do it only if measurement says the queue is a real problem.

## 5. Alternatives considered

| option | verdict |
|---|---|
| **Leave it; coordinate by hand** (freeze merges around a promotion) | This *is* the status quo, and it is the thing that failed four times in one night. It relies on the person who bears no cost remembering to pause. |
| **Re-dispatch-on-eviction watcher** | Detects a 0-job cancellation and re-fires. Treats the symptom, adds a moving part that can itself loop, and cannot help while merges keep arriving — it just re-enters the same race. |
| **Make the promotion not deploy dev first** | Would make dispatch runs short and let groups split safely by environment (dev and prod stacks are disjoint; shared assets are content-addressed and idempotent). But "prod jobs `needs:` the full dev stage" is a deliberate safety property — prod only ships behind a dev deploy that just succeeded on the same commit. Removing it is a much larger change with a real safety regression, and it is orthogonal to eviction. |
| **Scheduled promotion window** | Reduces collisions statistically, does not remove them, and adds latency to every fix. |
| **GitHub Environments with required reviewers / wait timer** | Not available: Enterprise-only for private repos, and the API returns 422 on this org's Team plan. Already tried and closed (isol8 #2016). |

## 6. Recommendation

Ship `queue: max` on the existing `lightbulb-deploy` group. One generated line;
no change to the group, the DAG, the promotion gate, or which stacks a run
touches.

**Confidence: high.** The semantics are documented, the key was validated against
this repo's own validator with a negative control, the diff to `deploy.yml` is a
single line, the full `apps/infra` suite (330 tests) passes, and the failure mode
being removed is proved from run records rather than inferred.

Two facts to carry forward: a promotion no longer needs a merge freeze, and
**re-dispatching a promotion no longer jumps the queue** — it appends another run
behind the first. Re-dispatch only when the first run has actually failed.
