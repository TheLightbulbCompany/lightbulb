# What happens after "Let's do it": Paperclip and Hermes mechanics for Ideas

Date: 2026-10-15. Read-only code research. Nothing was run.

This report goes deeper than section 5 of `2026-10-15-hermes-capabilities-for-execution.md` and does not repeat it.

## Sources and how to read the citations

- **LB** = Lightbulb at `origin/main` 521ea1cc7 (#4165). The local `main` checkout is older (1553085fd) and has no `ideas.py`. Paths are relative to the repo root.
- **PC** = the Paperclip fork at commit c6f5a4d2e. `paperclip-version.json` names this as `upstreamCommit`. The fork is **not vendored** in this repo; the repo only pins the image by digest. I read the local checkouts `~/Desktop/paperclip-fork-rebase.nosync` and `~/Desktop/paperclip-fork.nosync`, which are both at c6f5a4d2e. The deployed image is built from the fork tag, so it can differ from this source. I did not inspect the image. PC paths are relative to the checkout.
- `H` = `PC server/src/services/heartbeat.ts` (29k lines). `R` = `PC server/src/services/recovery/service.ts`.
- A line number with `~` is approximate (within a few lines).

## 0. Short answer

- An idea of size `task` creates a **backlog** task. Backlog never wakes anyone. The only wake is the person's DM message.
- The agent then moves the task to `in_progress` from inside the DM turn. That move **does** wake Paperclip, but the bridge folds that wake into the live DM turn.
- An idea of size `project` creates an `in_progress` project. Projects never wake anyone. Work continues only if the DM turn files tasks as `todo`. The skill tells the agent to file them as `backlog` by default.
- After the first DM turn, the work moves forward through four mechanisms, and nothing else:
  - Paperclip's 30-second stranded-issue reconciler, for `todo`/`in_progress`/`in_review` tasks.
  - Recovery and continuation wakes, which have small attempt caps and then set the task to `blocked`.
  - Timer heartbeats: the CEO every 8 h, 12 h or 24 h; specialists every 24 h, and only when they have `todo`/`in_progress` work.
  - Comments, mentions and child or blocker completion.
- Each agent can have up to 20 Paperclip runs at once. One owner's microVM runs at most 4 turns at once, across all agents.

## 1. Wake path

### 1.1 Text diagram

```
 IDEA START (LB apps/backend/core/services/ideas.py _start)
   |-- size=project -> Paperclip POST project {status:in_progress, leadAgentId}   (no wake: projects never wake)
   |-- size=task    -> Paperclip POST issue {status:backlog, assigneeAgentId}     (no wake: backlog is skipped)
   '-- create_human_message(DM, "> idea ... Let's do it! Tracked in Work: [..](link)", via="ideas")
            |
            v
 DM WAKE (room path)
   messaging._dispatch_targets: a DM addresses its agent -> eventKind user_request
   -> FIFO SQS, one group per (owner, room, agent)          LB messaging_repo.room_fifo_ids
   -> openclaw_work consumer: steer a live run, else create run row
      POST runtime /plugins/lightbulb-channel/inbound/<agentId>
   -> bridge run_turn -> per-(agent, conversation) lock -> microVM slot (max 4) -> Hermes turn

 ISSUE WAKE (Paperclip path)
   issue create/PATCH/comment (PC server/src/routes/issues.ts)
   -> queueIssueAssignmentWakeup / addWakeup -> heartbeat.enqueueWakeup
      (gates: backlog, wakeOnDemand, budget, daily caps, throttle, issue execution lock)
   -> agent_wakeup_requests + heartbeat_runs(queued)
   -> startNextQueuedRunForAgent (<= maxConcurrentRuns, default 20)
   -> adapter hermes_gateway (PC packages/adapters/hermes/src/gateway/server/execute.ts)
      POST {PUBLIC_API_BASE}/api/v1/internal/paperclip/hermes/<owner>/v1/runs
         headers: Bearer hook_token, Idempotency-Key=<runId>, X-Hermes-Session-Key
         body: {input: rendered wake prompt, instructions, session_id}
   -> LB routers/messaging.py create_paperclip_hermes_run (25 s budget, 503 when cold)
   -> runtime_transport -> bridge server.py POST /internal/paperclip/v1/runs -> 202
   -> bridge turn.run_paperclip_turn (thread) -> _route_paperclip_run
        - issue bound to a room conversation of THIS agent?
            live room run -> steer it, hold the Paperclip run open until the room run ends
            no live run   -> run a turn in that room conversation (no room post)
        - else -> own session "paperclip:company:C:agent:A:issue:I"
   <- adapter polls GET .../v1/runs/<id> every 10 s until completed/failed/cancelled
   -> Paperclip finalizes the run, auto-posts the final text as an issue comment,
      then runs disposition/handoff/recovery checks
   -> plugin isol8-notifications -> LB /webhooks/paperclip-events (push only)
```

### 1.2 Hop by hop, with evidence

1. **Paperclip event to wake request.**
   - On create, `PC server/src/routes/issues.ts:12013` calls `queueIssueAssignmentWakeup` with `reason:"issue_assigned"`.
   - The helper returns early for backlog: `if (!input.issue.assigneeAgentId || input.issue.status === "backlog") return;` (`PC server/src/services/issue-assignment-wakeup.ts:43`).
   - The wake carries `source:"assignment"`, `payload.issueId`, `contextSnapshot.issueId` and `contextSnapshot.source` (lines 45-76).
2. **Adapter config.** Every agent uses `hermes_gateway`. The config is built in `LB apps/backend/core/services/paperclip_adapter_config.py:55-62`:
   - `apiBaseUrl` = `.../api/v1/internal/paperclip/hermes/{owner_id}`
   - `sessionKeyStrategy: "issue"`
   - `timeoutSec: 3600` (line 25)
   - `eventReconnectMs: 30000` (line 34)
   - `pollIntervalMs: 10000` (line 35)
3. **Adapter request.**
   - The adapter sends `POST {apiBaseUrl}/v1/runs` (`PC .../gateway/server/execute.ts:~862-884`).
   - Headers are built at `execute.ts:248-262`: Bearer, `Idempotency-Key: runId`, `X-Hermes-Session-Key`.
   - The body is built at `execute.ts:323-340`.
   - The session key is `paperclip:company:{co}:agent:{ag}:issue:{issueId}` (`execute.ts:151-166`).
4. **Backend facade.** `LB apps/backend/routers/messaging.py`:
   - Routes are at 1479 (create), 1512 (poll) and 1527 (stop).
   - Identity is parsed at 1548-1581. Company, agent and issue ownership are checked on create at 1584-1627.
   - The runtime budget is `PAPERCLIP_HERMES_TIMEOUT_S = 25.0` (1641). The reason is that API Gateway caps at 30 s and a cold microVM takes 54-57 s, so the first call to a parked workspace fails with a retryable 503 (1630-1640).
   - A 404 from the bridge becomes `{"status":"failed","error":"runtime_lost"}` (1680-1698).
5. **Bridge accept.** `LB apps/infra/hermes/bridge/server.py:93-126`:
   - `reserve_turn()` returns 503 `recycling` during a recycle hold (lines 98-99; hold TTL 120 s, `turn.py:60`).
   - It registers the run idempotently (`turn.register_paperclip_run`, `turn.py:719-749`), starts a thread on `turn.run_paperclip_turn`, and answers 202.
6. **Routing inside the bridge.** `LB apps/infra/hermes/bridge/turn.py:4057-4090` (`_route_paperclip_run`):
   - It looks up `<profile>/lightbulb-issue-bindings/<issueId>.json` (`issue_conversation`, 3948-3968; TTL 14 days, 3928).
   - If the agent has a live room run on that conversation, it steers the wake text into that run (4076-4083). `_await_room_run` then holds the Paperclip run open until the room run ends, and reports "completed" with the text "Handled in the room conversation that owns this issue" (4022-4041).
   - If the agent is still a room member, it runs a turn in that conversation (4084-4087).
   - Otherwise it runs in the issue's own session (4090).
7. **Turn.** `_run_native_paperclip` (`turn.py:3770-3854`):
   - It sets `PAPERCLIP_API_URL`, `PAPERCLIP_AGENT_ID`, `PAPERCLIP_COMPANY_ID`, `PAPERCLIP_RUN_ID` and `PAPERCLIP_TASK_ID` in the shell (3788-3795).
   - It takes the conversation lock and a slot (3811).
   - Outcomes:
     - `completed` if there is a reply.
     - `failed` with `empty_response`, `model_unavailable` or `INTERRUPTED`.
     - `cancelled` on abort (3842-3854).
8. **Result back to Paperclip.**
   - `finish_paperclip_run` writes a durable S3 record first, then updates memory (`turn.py:804-838`). Output is capped at 256 KB (801).
   - A 30 s heartbeat refreshes a "running" record, which counts as stale after 90 s (758-759, 786-791). That way a split-session microVM can answer polls.
   - The adapter polls until a terminal status. Its terminal set is at `execute.ts:82-90`.

### 1.3 Which statuses wake

Issue creates and PATCHes:

| Event | Wakes the assignee? | Evidence |
|---|---|---|
| Create, assigned, status `backlog` | **No** | `issue-assignment-wakeup.ts:43` |
| Create, assigned, any other status (incl. no status: the server default is `todo` when assigned) | Yes, `issue_assigned` | `routes/issues.ts:12013`; `LB apps/infra/hermes/skills-src/paperclip/references/api-reference.md` "status on create" table |
| Sub-issue create, assigned, not backlog | Yes, `issue_assigned`, `issue.child_create` | `routes/issues.ts:12252` |
| Reassign while not backlog | Yes, `issue_assigned` | `routes/issues.ts:14537-14546` |
| `backlog` -> any non-backlog status (when `status` is in the body) | Yes, `issue_status_changed` | `routes/issues.ts:14434-14437`, `14577-14590` |
| `blocked`->`todo`, `done`/`cancelled`->`todo`, user moves `in_review`->`todo` | Yes | `routes/issues.ts:14438-14446`, 13909-13912 |
| `todo`->`in_progress`, ->`in_review`, ->`blocked`, ->`done` | No | same block: no generic status wake |
| Project create with `leadAgentId` | **No** (projects have no wake code) | `PC server/src/routes/projects.ts:233`; no `wakeup` in `services/projects.ts` |

Comments and graph events:

| Event | Wakes? | Evidence |
|---|---|---|
| Comment by a human or another agent on an open issue (**including backlog**) | Yes, `issue_commented` | `PC server/src/services/issue-comment-wakeup.ts:18-27` |
| The assignee's own comment | No (unless an explicit resume) | same, lines 18-23 |
| @mention in a comment | Yes, `issue_comment_mentioned` | `routes/issues.ts:~14688-14715` |
| All children done/cancelled | Wakes the parent, if the parent is assigned and not backlog/done/cancelled | `PC services/issues.ts:9307-9360` |
| All blockers done | Wakes the dependent, if it is assigned and not backlog | `PC services/issues.ts:9243-9305` |

### 1.4 What the agent sees

- **DM wake.**
  - It is a normal room turn on the room's own Hermes transcript, with up to 50 messages of recent history (`LB apps/backend/core/services/openclaw_work.py:44`; `turn.py:3563-3595`).
  - The user message is the person's message. For an idea that is `start_text` (`ideas.py:435-443`):

    ```
    > **<title>**
    > <why>

    Let's do it!
    Tracked in Work: [<title>](<FRONTEND_URL>/chat?panel=teams-project-detail&detailId=<projectId>)
    ```

    For a task, the link uses `panel=teams-issue-detail` (`ideas.py:420-423`). For a quick idea, there is no link line.
  - The turn has **no** `PAPERCLIP_RUN_ID` and no checkout (`LB apps/infra/hermes/skills-src/paperclip/SKILL.md:31`).
- **Issue wake (own session).** The `input` is the adapter's rendered prompt (`execute.ts:266-321`):

  ```
  You are <name>, an AI agent employee in a Paperclip-managed company.
  Paperclip runtime identity: Agent ID / Company ID / Run ID / Paperclip API URL
  Execution contract: take concrete action ... leave durable progress and update the issue to a clear final disposition ...
  ## Paperclip Wake Payload  (reason, issue identifier + title, pending comments ...)
  <task markdown>
  Structured wake payload JSON
  ```

  - The wake prompt comes from `PC packages/adapter-utils/src/server-utils.ts:2180`.
  - A recovery wake swaps the execution contract for a "recover, do not redo" block (2237-2253).
  - The bridge adds the agent's `AGENTS.md` and a repo rule to the system prompt (`turn.py:3821`).
- **Issue wake bound to a room conversation.**
  - If a run is live, it is steered in with the prefix `[Paperclip woke you for issue X, which this conversation is already working on ...]` (`turn.py:3938-3941`).
  - If no run is live, a turn runs on the room transcript with the prefix `[Paperclip woke you for issue X ... Nothing you write in this turn is posted to the room: record progress on the issue itself.]` (3935-3937).
- **Timer wake.**
  - The context has no issue: `{source:"scheduler", reason:"interval_elapsed"}` (`H:29158-29163`). The session starts fresh (`H:5673-5679`).
  - The agent reads `$HERMES_HOME/HEARTBEAT.md` (`LB apps/infra/hermes/skills-src/paperclip/SKILL.md:40`). The checklists are seeded from `LB apps/backend/core/services/heartbeat_checklist.py`.

## 2. Concurrency

| Limit | Value | Scope | Evidence |
|---|---|---|---|
| Paperclip `maxConcurrentRuns` | default 20, clamp 1..50 | per agent | `PC packages/shared/src/constants.ts:77`; `H:639-641`; enforced only in `startNextQueuedRunForAgent` `H:19424-19450` |
| Company or global run cap in Paperclip | **not found in repo** (none in code) | | agent report on `H` |
| Issue execution lock | one live run per issue (`executionRunId`) | per issue | `H:17567-17604`, `H:26246-26249` |
| Agent start lock | serializes claim+dispatch; 30 s stale timeout; in memory | per agent | `PC server/src/services/agent-start-lock.ts:3,32-48` |
| Bridge turn slots | `LIGHTBULB_MAX_CONCURRENT_TURNS`, default **4** | per microVM (the whole workspace, all agents) | `LB apps/infra/hermes/bridge/turn.py:2889-2890` |
| Compactions | at most 3 slots (4-1) | per microVM | `turn.py:2893` |
| Conversation lock | one turn at a time per (agent, conversation) | per conversation | `turn.py:2886, 2903-2907, 2956-2989` |
| Hermes `max_concurrent_sessions` | 3 | per profile | `LB apps/backend/core/services/hermes_profiles.py:360` |
| Room queue | FIFO group per (owner, room, agent) | per room+agent | `LB apps/backend/core/repositories/messaging_repo.py:272-274` |

**A second wake while a run is active.**

- **Paperclip, same issue.** `admitWakeBehindIssueExecution` (`H:~27200-27250`; `PC server/src/modules/wake-queue/domain/policy.ts:44-61`) does one of three things:
  - **coalesces** into the running run (same actor and same agent);
  - **defers** it (`deferred_issue_execution`; for example a comment wake while running, `issue_blockers_resolved`, or a different agent), and promotes it when the run ends;
  - **proceeds**, when there is no live run.
- **Bridge, same conversation.**
  - A room follow-up first tries to **steer** the live run (`openclaw_work.py:642-647`, steer call at ~820).
  - Otherwise it waits on the conversation lock and becomes the next turn. This is the "lock = +1 turn" behaviour: one more turn queued behind the running one, never two in parallel (`turn.py:2880-2885`).
  - After the running job has held the lock for 30 s, a typed turn runs beside it in a side session (`SIDE_SESSION_AFTER_S = 30.0`, `turn.py:2898-2900`; `_side_session` 3666).
- **Two agents of one company** can run at the same time:
  - Paperclip has only per-agent slots.
  - In the bridge, different conversations run concurrently, up to 4 turns per microVM (`turn.py:2880-2890`).
- **One agent on two issues** can also run at the same time, up to 20 runs in Paperclip and 4 turns in the bridge.
- **The 5th concurrent turn waits** for a slot inside the bridge. The wait counts against the Paperclip run's 3600 s adapter timeout.

## 3. Run lifecycle

### 3.1 Timeouts, retries, failure

- **Paperclip run wall clock: 3600 s.**
  - We set `timeoutSec` in `paperclip_adapter_config.py:25`. The adapter default is 600 s (`PC .../gateway/shared/constants.ts:4`).
  - On timeout the adapter POSTs `/stop`, waits a 10 s grace, and fails with `hermes_gateway_timeout` (`execute.ts:~936-960`).
- **Hermes turn budget.** `run_budget_seconds: 1800` (`LB hermes_profiles.py:363`). A turn ends long before the Paperclip clock does.
- **Adapter error classes** (`execute.ts:~349-355`):
  - 429 and 5xx are `transient_upstream`.
  - 401 and 403 are `hermes_gateway_auth_failed`.
  - A 404 on create is `runs_unsupported`.
- **Retry ladder** (agent report on `H`/`R`):

| Failure | Retries | Then | Evidence |
|---|---|---|---|
| Transient (429, 5xx, connect fail, cold-start 503) | 2 bounded retries, 30 s apart | default continuation class: 1 more attempt, then `blocked` | `H:759-762`; `R:531` |
| `timeout` / `adapter_failed` | up to 3, backoff 60 s, 120 s, 240 s | `blocked` | `R:476-483, 530-532, 5307-5316` |
| `hermes_gateway_*` codes not in the transient list | 1 | `blocked` | `R:531, 757-763` |
| `setup_failed` | 0 | `blocked` on the first failure | `R:485-497, 744-751` |
| `process_lost` (orphan reaper: run not updated for 5 min) | 0 for hermes_gateway (it has no local child process); then default class: 1 attempt | `blocked` | `PC server/src/index.ts:1761`; `H:18883-18887`. The hermes case is the agent's inference |
| Our `runtime_lost` (bridge 404 after the microVM died) | treated as a failed run | default class 1 | `LB routers/messaging.py:1692-1698` |
| A successful run that left no disposition | 1 `finish_successful_run_handoff` wake | board escalation | `PC .../recovery/successful-run-handoff.ts:21`; `H:13176` |
| A successful run that posted no comment (for `issue_assigned` and execution-stage wakes) | 1 follow-up wake | `retry_exhausted` | `H:5580-5592, 14146-14160` |
| Plan-only or empty-response runs on `todo`/`in_progress` | 2 liveness continuations | comment "Bounded liveness continuation exhausted" | `PC .../recovery/run-liveness-continuations.ts:9-12` |

- **Exhaustion.** `escalateStrandedAssignedIssue` sets the issue to `blocked` and posts "No live execution path / Automatic recovery blocked" (`R:3840`; `PC .../recovery/stranded-notice.ts:27-29`).

### 3.2 How a run reports back

- **Issue comment.** Paperclip posts the run's final text as an issue comment automatically (`H:24566-24655`), unless `skipIssueComment` is set.
  - For a wake that was steered into a room run, that comment is the bridge's placeholder: "Handled in the room conversation that owns this issue (room run ...)" (`turn.py:4035-4036`).
- **Status changes.** These are the agent's own job, through `paperclip-api.sh update`. Paperclip does not change status on success (`H:13176`).
- **Room.** A Paperclip-family turn cannot post to a room. `send_room_message` answers NO_TURN (`turn.py:3773-3775`). The CEO checklist forbids room tools (`LB .../paperclip/references/ceo-heartbeat.md:223-226`).
- **Push.** The fork plugin posts `agent.run.finished`/`failed` to `LB apps/backend/routers/paperclip_events.py`.
  - It is fire-and-forget with no retry (lines 9-11).
  - Pushes are sent only for `assignment`/`on_demand` sources (line 47). Failures have a 1 h cooldown (line 51).

### 3.3 Heartbeats, CEO timer, check-ins, feed

There is no mechanism called "daily check-in" in code. The candidates are listed below.

| Mechanism | Owner | Schedule | Gate | What it does |
|---|---|---|---|---|
| Specialist timer heartbeat | Paperclip `tickTimers` | `intervalSec: 86400` | `skipTimerWhenNoActionableWork: true`: skipped unless the agent has an assigned **`todo`/`in_progress`** issue (`H:751`, `H:26162-26175`) | Reads `HEARTBEAT.md`: in_progress first, then todo; "Nothing assigned? Exit." (`LB heartbeat_checklist.py` SPECIALIST_CHECKLIST) |
| CEO timer heartbeat | Paperclip `tickTimers` | 28800 s paid, 43200 s trial, 86400 s BYO/ChatGPT (`LB user_provision.py:226-237`) | runs even when idle (`skipTimerWhenNoActionableWork: False`, 427) | CEO checklist plus board hygiene: close done, merge duplicates, cancel stale, **nudge stalled `todo`/`in_progress` with no run and no comment for 48 h**, at most 10 mutations per wake (`ceo-heartbeat.md:160-215`) |
| Arming | LB `arm_nightly_ceo` | every provision and billing reconcile | not entitled -> `pause_owner_automation` (`user_provision.py:349-435`) | sets the heartbeat configs above |
| Feed routines | Paperclip routines | 08:00, 13:00, 18:00 in the creator's timezone (`user_provision.py:246, 263-267, 332`) | `coalesce_if_active`, `skip_missed` (304-305) | one feed post if something matters (249-256) |
| Starter routines | Paperclip routines | weekly or monthly at 13:00 UTC | created **paused** (`paperclip_provisioning.py:142-147`) | summaries |
| Scheduler tick | Paperclip | every 30 s (env `HEARTBEAT_SCHEDULER_INTERVAL_MS`, minimum 10 s) | | `tickTimers`, routines, reaper, reconciler (`PC server/src/config.ts:366`; `index.ts:1181, 1625-1796`) |

- Timer elapsed time is measured from `lastHeartbeatAt ?? createdAt` (`H:29144-29146`).
- We set no `maxDailyRuns` on purpose. Paperclip counts assignment wakes against it (`user_provision.py:363-365`).

## 4. Projects and delegation

**Friday (CEO) to Atlas, from the DM turn.**

1. Friday files a task with `paperclip-api.sh create ... --assignee <Atlas> --status todo`.
   - In a room conversation, the helper creates the task as `backlog`, writes a binding in **Friday's** profile, then PATCHes it to `todo` (`LB .../paperclip/scripts/paperclip-api.sh:357-364, 513-530`).
2. The `backlog -> todo` PATCH wakes Atlas (`routes/issues.ts:14577-14590`) **at once**.
3. Atlas's bridge finds no binding in Atlas's own profile, because bindings belong to one agent (`turn.py:3966`). So Atlas runs in its own issue session, in parallel with Friday's DM turn, if a slot is free.
4. Atlas's result becomes an issue comment. Atlas cannot post in the DM.
5. Friday is woken again only in these cases:
   - Atlas's task is a **child** of a task assigned to Friday that is not backlog, and all children finish (`services/issues.ts:9307-9360`).
   - Atlas's task blocks one of Friday's tasks.
   - Someone mentions Friday.
   - Friday's next CEO timer fires.
   
   The project itself never wakes anyone.

**Default status works against the Ideas instruction.**

- `ideas.md:6-8` says: break the project into tasks, each assigned, "Assigned tasks wake their agent".
- The paperclip skill says backlog is "the normal case; all planning output" (`SKILL.md:27`). The helper sends an explicit `backlog` when `--status` is missing (`paperclip-api.sh:107-108, 119-120, 209-214`).
- A task filed as backlog never runs:
  - The reconciler excludes backlog (`R:4257-4275`).
  - The timers ignore it (`H:751`).
  - The CEO's nudge rule covers only `todo`/`in_progress`.

**Tasks cannot be put in the project with the helper.**

- `create`/`subtask` have no `--project` flag. Their body lists no `projectId` (`paperclip-api.sh:98-120`). `update` has none either (44-48).
- The only flag named `--project` is on `search` (172, 1251).
- To file a task "in that project", the agent must use `raw` with `projectId`. It also has to pull the project id from the link's `detailId=` parameter.

**Do sub-issues wake?** Yes, when they are assigned and not backlog (`routes/issues.ts:12252`). Serialized children are the exception (12251).

**Reconciler.**

- `reconcileStrandedAssignedIssues` runs on every 30 s tick. It covers assigned `todo`/`in_progress`/`in_review` issues with no live execution path (`R:4257-4275`, `R:1089-1140`).
- What it does:
  - A `todo` task with no run gets an initial dispatch (`R:5030`).
  - A `todo` task with a prior run gets 1 recovery, then `blocked` (`R:5047-5085`).
  - An `in_progress` task gets the continuation class limits above. A productive `in_progress` task is exempt for 30 min after visible progress (`R:174-177, 5160-5199`).
- The re-wake throttle engages after 2 runs with no progress: 120 s doubling to 30 min (`PC server/src/services/issue-rewake-throttle.ts:28-40`).

**Room vs Paperclip duplicate work (current state): fixed with issue bindings, not with a guard.**

- The helper binds an issue to the room conversation **before** any PATCH or checkout that can wake (`paperclip-api.sh:367-377`) and before the `todo` flip on create (513-530).
- The bridge routes that issue's wakes into the conversation (`turn.py:3918-3941, 4057-4090`).
- Bindings expire after 14 days (3928). They only help the agent that wrote them.

**Comment-then-cancel race.** There is no code fix on our side. It is handled by instruction only:

- The CEO checklist says "Cancel-then-comment, never the reverse": one `update --status cancelled` with the reason on stdin (`ceo-heartbeat.md:167-184`).
- Paperclip cancels the live run inside the same status update.
- A comment on an open issue (backlog included) wakes the assignee (`issue-comment-wakeup.ts:24-27`).

## 5. Ideas start code

All in `LB apps/backend/core/services/ideas.py`.

**Claim and idempotency.**

- `claim_idea_start` claims `(workspace, idea, starter)` with a DynamoDB conditional write. A stale claim is taken over after 300 s (`LB messaging_repo.py:2761, 2793-2824`). `startToken` is minted once per claim.
- A second Start while the Work item is open returns the stored ids (`ideas.py:486-487`). A Start after the Work item closed deletes the row and claims again (488; `close_idea_start` 2843-2856).
- On any exception the claim is released (`claimedAt: 0`) and the error is re-raised (556-561). A Paperclip error becomes a 502 (453-462).
- The Paperclip create is replay-safe through `idempotencyKey = "lightbulb-idea-project:v1:" + sha256(owner|idea|user|startToken)` (446-450). The fork's `Idempotency-Key` patch is listed in `paperclip-version.json` notes.
- The DM message id is `uuid5(owner|idea|user|startToken)`, so a retry replays the same message (539-541).

**Project (size `project`).**

- Fields: `{name: title, status: "in_progress", leadAgentId: agent, idempotencyKey, goalIds: [onboarding_goal_id]?}` (501-507).
- There is no description, no tasks and no wake.
- "Done" means project status `completed`/`cancelled`, or archived (44, 409).

**Task (size `task`).**

- Fields: `{title, description: why + "What's included:" deliverables, status: "backlog", assigneeAgentId, idempotencyKey, allowDuplicate: true}` (517-529).
- The code comment says backlog is used so that the DM message is the only wake (520-522).
- But `ideas.md:9` tells the agent to move it to `in_progress`. That move is `backlog -> non-backlog`, which **does** wake. The binding written just before the PATCH folds that wake into the DM turn. The Paperclip run is then held open and comments "Handled in the room conversation..." on the task.
- "Done" means `done`/`cancelled` (46, 412).

**"Tracked in Work"** links to `{FRONTEND_URL}/chat?panel=teams-project-detail|teams-issue-detail&detailId=<id>` (420-423). It is an app UI link, not an API link.

**Quick idea.**

- Nothing is created in Work. Only the DM message is posted, with no link line (441-443).
- `_closed` returns True when there are no ids (417). So the next Start closes the old start and posts a new message at once.
- The card is hidden for 24 h (`QUICK_HIDDEN`, 48).

**DM.** The starter's DM with the agent is unarchived if needed (`_usable_dm`, 566-577).

**Agent suggestions.** At most 10 open per workspace (`SUGGESTION_CAP`, 52).

## 6. Routines

Source: agent report on `PC server/src/services/routines.ts`. Spot-checked.

**Triggers.**

- Kinds are `schedule`, `webhook` and `api` (`PC packages/shared/src/constants.ts:648`).
- Only `schedule` triggers are ticked (`routines.ts:3326`). The tick runs inside the 30 s heartbeat scheduler (`index.ts:1671-1680`). Routines do not run if the scheduler is disabled.

**Dispatch.**

- Dispatch creates an issue with **`status: "todo"`** assigned to the routine's agent (`routines.ts:2021, 1834`), then calls `queueIssueAssignmentWakeup` with `contextSource:"routine.dispatch"` (2122-2130).
- So a routine run is an ordinary assigned-issue wake. The issue title and description are the routine template with variables filled in (1864-1868).

**Policies.**

- Concurrency: `coalesce_if_active` (default), `skip_if_active` or `always_enqueue` (`constants.ts:636`).
- Catch-up: `skip_missed` (default) or `enqueue_missed_with_cap`, with `MAX_CATCH_UP_RUNS = 25` (`routines.ts:88`). Sub-hourly crons are never caught up (288-302).

**When a previous run is still open.**

- A new run coalesces only if the open issue has a **live run** and the same fingerprint (1616-1668, 1873-1884).
- An open issue with no live run does not block, so issues can pile up. Editing the routine changes the fingerprint.

**Circuit breaker.**

- It auto-pauses after 3 consecutive failed or blocked outcomes (instance default, `PC server/src/services/instance-settings.ts:210-222`; `routine-circuit-breaker.ts:49-77`).
- It emits `routine.auto_paused` to our backend.

**Other limits.**

- No per-company routine cap and no minimum cron interval were found (not found in repo).
- Webhook triggers support bearer or HMAC auth with a 300 s replay window, and no rate limit was found (`routines.ts:3115-3250`).

**Ours.** The 3 feed routines go to the CEO (section 3.3). Starter routines are created paused.

## 7. Every number

| Number | What | Where |
|---|---|---|
| 3600 s | Paperclip run timeout (ours) | LB `paperclip_adapter_config.py:25` |
| 600 s | adapter default timeout (overridden) | PC `gateway/shared/constants.ts:4` |
| 10 s / 30 s | adapter poll / SSE reconnect (ours, the clamp ceilings) | LB `paperclip_adapter_config.py:34-35` |
| 10 s | stop grace after timeout | PC `gateway/shared/constants.ts:7` |
| 25 s | backend facade budget to the runtime | LB `routers/messaging.py:1641` |
| 30 s | API Gateway hard cap | same, 1630-1632 |
| 54-57 s | cold microVM start (measured) | same, 1636 |
| 90 s | AgentCore ingress timeout for room work | LB `runtime_transport.py:41` |
| 1800 s | Hermes run budget per turn | LB `hermes_profiles.py:363` |
| 3 | Hermes max concurrent sessions per profile | LB `hermes_profiles.py:360` |
| 4 | bridge turns at once per microVM | LB `turn.py:2889` |
| 30 s | wait before a typed turn runs in a side session | LB `turn.py:2900` |
| 120 s | refusal hold during recycle | LB `turn.py:60` |
| 30 s / 90 s | Paperclip running-record heartbeat / stale | LB `turn.py:758-759` |
| 256 KB | run output cap in the durable record | LB `turn.py:801` |
| 512 | in-memory run registry entries | LB `turn.py:878` |
| 14 days | issue->conversation binding TTL | LB `turn.py:3928` |
| 1 s | steered Paperclip run checks the room run | LB `turn.py:4019` |
| 600 s | post-turn memory/review wait ceiling | LB `turn.py:3868` |
| 50 | room history messages per turn | LB `openclaw_work.py:44` |
| 10 s | room work retry visibility | LB `openclaw_work.py:50` |
| 300 s | idea start claim stale | LB `messaging_repo.py:2761` |
| 24 h | quick idea hidden | LB `ideas.py:48` |
| 10 | open agent idea suggestions | LB `ideas.py:52` |
| 86400 s | specialist timer | LB `paperclip_provisioning.py:70` |
| 28800 / 43200 / 86400 s | CEO timer paid / trial / BYO | LB `user_provision.py:226-237` |
| 08, 13, 18 h | feed routines | LB `user_provision.py:246` |
| 120 s | feed trigger claim | LB `user_provision.py:248` |
| 3600 s | run-failure push cooldown | LB `routers/paperclip_events.py:51` |
| 30 s (min 10 s) | Paperclip scheduler tick: timers, routines, reaper, reconciler | PC `server/src/config.ts:366` |
| 15 s | execution-control reconciliation | PC `services/execution-control-deadline.ts:3` |
| 5 min | orphan reaper staleness | PC `server/src/index.ts:1761` |
| 20 (1..50) | maxConcurrentRuns per agent | PC `packages/shared/src/constants.ts:77`; `H:639-641` |
| 30 s | agent start lock stale | PC `services/agent-start-lock.ts:3` |
| 30 s x2 | bounded transient retry | `H:759-762` |
| 3; 60 s base | transient continuation attempts; backoff | `R:530-532` |
| 1 / 0 | default / non-retryable continuation attempts | `R:531`, `R:744-751` |
| 1 | successful-run handoff attempts | PC `recovery/successful-run-handoff.ts:21` |
| 2 | liveness continuations | PC `recovery/run-liveness-continuations.ts:9` |
| 5 | disposition repair attempts | PC `recovery/disposition-repair.ts:18` |
| 30 min | productive-progress exemption | `R:174-177` |
| 1 h / 4 h | active-run output silence suspicion / critical | `R:154-156` |
| 2; 120 s -> 30 min; 6 h; 8 | re-wake throttle threshold; cooldown; lookback; sample | PC `services/issue-rewake-throttle.ts:28-40` |
| 5 min -> 1 h; 3 | accepted-interaction requeue backoff; cap | `R:517, 527-528` |
| 1 h | provider-quota default backoff | `R:533` |
| 20 | cross-issue writes per run | PC `services/cross-issue-influence-limit.ts:8` |
| 20 | child summaries on parent wake | PC `services/issues.ts:219` |
| 25 | routine catch-up runs | PC `services/routines.ts:88` |
| 3 | routine auto-pause failures | PC `services/instance-settings.ts:210-222` |
| 300 s | webhook HMAC replay window | PC `packages/shared/src/validators/routine.ts:161` |
| 10 | CEO hygiene mutations per wake | LB `ceo-heartbeat.md:186` |
| 48 h / 7 d / 14 d | CEO nudge idle / re-nudge gap / stale cancel | LB `ceo-heartbeat.md:196-207` |

## 8. Where a long job can be dropped silently

1. **A backlog task that was never moved.**
   - If the DM turn fails or forgets to move the `task` idea, nothing ever picks it up. The reconciler, the timers and the CEO nudge all skip backlog (`R:4257-4275`; `H:751`; `ceo-heartbeat.md:203`).
   - The idea stays hidden as "started" while the task is open (`ideas.py:279-281`).
2. **Project tasks filed as backlog.** This is the helper default and the skill's "normal case" (`SKILL.md:27`). A project with only backlog tasks, or no tasks, never runs again.
3. **Projects are not tracked by any sweeper.** Nothing reads project status. "Done" depends on someone setting `completed`.
4. **Retry exhaustion leads to `blocked`.** The caps are 1, 2 or 3. The notice is a comment on the issue plus a board recovery action. The DM gets nothing. A push is sent only for run failures, fire-and-forget, with a 1 h cooldown (`paperclip_events.py:9-11, 51`).
5. **A dead microVM leads to `runtime_lost`.** The 7 h recycle, idle park and disk recycle all kill the run, along with its background processes and async delegations. That counts as a failed run with 1 default retry (`messaging.py:1680-1698`).
6. **Slot starvation.** The 5th concurrent turn waits in the bridge while the Paperclip 3600 s clock runs. A timeout costs a retry from the ladder.
7. **A steered wake reports "completed" with no work.** A Paperclip wake steered into a live DM run is marked completed when the room run ends, even if that run never touched the issue (`turn.py:4033-4036`). Recovery has to notice the missing disposition later. It allows 1 handoff, then escalates.
8. **Throttle.** After 2 runs with no progress, `issue_assigned` and continuation wakes are skipped for 2 to 30 min (`issue-rewake-throttle.ts`). The skip is recorded as a skipped wake row only.
9. **Skipped wake rows nobody reads.** Paused or terminated agent, budget block, `wakeOnDemand` off, or de-entitlement through `pause_owner_automation`. Each writes a `skipped` row and nothing more (`H:25870-26175`; `paperclip_agent_lifecycle.py:176-234`).
10. **The daily cap is not parked until midnight for hermes.** The midnight-parking patch matches openclaw codes. A `hermes_gateway_rate_limited` 429 follows the transient ladder, then `blocked`. This is the agent's inference from `R:651-716`. Not verified at runtime.
11. **Lost push notifications.** The isol8-notifications plugin has no retry and no backstop poll (`paperclip_events.py:9-11`).
12. **Routine pile-up.** An open routine issue with no live run does not coalesce, so issues pile up (`routines.ts:1616-1668`). Three failed or blocked runs auto-pause the routine.
13. **Child completion does not wake a backlog parent.** It also does not wake a parent assigned to a user (`services/issues.ts:9322`).
14. **No report back to the DM.** Work done in Paperclip runs (Atlas) lands as issue comments. Nothing posts it to the person's DM unless the CEO's next timer or the feed chooses to.
15. **Cold start.** The first wake into a parked workspace always 503s (25 s < 55 s cold start). It depends on the 2 transient retries 30 s apart, which is about 60 s of total patience.

## Not found in repo

- The Paperclip fork source. It is in `~/Desktop/paperclip-fork-rebase.nosync`, not in this repo.
- A mechanism called "daily check-in". `ideas.md:8` most likely means the 24 h specialist timer and the CEO timer.
- A Paperclip company-wide or global run cap.
- A minimum cron interval, or webhook rate limiting, for routines.
- Any sweeper over projects, or over backlog tasks.
- Live values of `HEARTBEAT_SCHEDULER_INTERVAL_MS`, `LIGHTBULB_MAX_CONCURRENT_TURNS` and the instance auto-pause settings. They are env or DB values and were not read.

## 9. Comment on a blocked issue

**Case.** The owner answered the agent's question with a comment on several `blocked` issues. Each issue moved to `todo`, but no agent run started.

Code: PC c6f5a4d2e and LB 521ea1cc7. I read code only and did not query prod.

### 9.1 What moves a blocked issue to todo

- This is Paperclip, not our code. Our comment route only forwards `{body}` (`LB apps/backend/routers/teams/issues.py:586-605`).
  - Our code does move `blocked` to `todo` in one other case: a reassignment PATCH with no status (`teams/issues.py:269-272`). That case is not a comment.
- In the POST comment route, `shouldImplicitlyMoveCommentedIssueToTodo` (`PC server/src/routes/issues.ts:2283-2327`) returns true when all of these hold:
  - the actor is a user;
  - the comment did not come from the issue's own checkout run or execution run;
  - the issue is `blocked`, `done` or `cancelled`;
  - the issue has an agent assignee.
- The route then sets `status: "todo"` only if the issue has no unresolved first-class blockers (`routes/issues.ts:17437-17460`). It logs `issue.updated` with `{status:"todo", reopened:true, reopenedFrom:"blocked", source:"comment"}` (17465-17491).
- After the comment is committed, `revalidateActiveSourceRecoveryAfterCommittedWrite(... blockedToTodoRecovery: true)` runs (17897-17908).
  - This cancels an active recovery action with the note "manually moved from blocked to todo" (4392-4440; classifier 4297-4298).
  - If that call throws, it is logged as `failed to revalidate recovery action after committed issue write`, and the action stays active (4471-4483).

### 9.2 Does a person's comment wake the assignee?

- Yes. `shouldWakeAssigneeForIssueComment` (`PC server/src/services/issue-comment-wakeup.ts:18-27`) returns true for a reopened issue, and for any issue that is not `done` or `cancelled`. That includes `backlog`, `todo`, `in_progress`, `in_review` and `blocked`.
- The issue must have an **agent** assignee (`routes/issues.ts:18017`, `if (assigneeId && !goalCommentSteered && ...)`).
- A reopened issue gets reason `issue_reopened_via_comment`, with `source:"issue.comment.reopen"` and `wakeCommentId` (18018-18047).
- **The wake is fire-and-forget.**
  - It runs inside `void (async () => {...})()` after the HTTP 201 response is prepared (17910-18253).
  - An error is only a log line: `failed to wake agent on issue comment` (18246-18250).
- `heartbeat.wakeup` then does one of four things:
  - **proceeds**: a new queued run;
  - **coalesces** into a live run on the issue;
  - **defers**, behind the issue's execution lock (`modules/wake-queue/domain/policy.ts:44-61`);
  - **skips**, and writes a `skipped` row (see section 8, item 9).

### 9.3 Where the wake can stop

1. **It is deferred, and the deferred wake is never promoted.**
   - When a run holds `issues.execution_run_id`, a comment wake becomes `agent_wakeup_requests.status = 'deferred_issue_execution'`. A wake that carries a `wakeCommentId` is never coalesced (`H:6896-6913`).
   - That holding run can be:
     - `queued`;
     - `running`, for example our hermes_gateway run that keeps polling for up to 3600 s while the bridge waits on a busy conversation lock or microVM slot;
     - `scheduled_retry`, with a backoff of 60 s to 1 h.
   - A deferred wake is promoted only by the release drain when the holding run finishes (`policy.ts:63-260`; `H:~27470-27540`).
   - The stale-lock sweeper clears lock columns whose runs are terminal (`R:5878-6010`), but it does **not** promote deferred wakes.
   - The only rescue is in `resumeQueuedRuns`, every 30 s (`H:19039-19068`). It requires all of these:
     - `issues.execution_run_id IS NULL`;
     - the issue's assignee is the wake's agent;
     - the payload has `_paperclipWakeContext.wakeCommentIds`;
     - the latest run on the issue has `runtime_mode = 'legacy'` and a terminal status.
   - Meanwhile the 30 s reconciler counts a deferred wake as a live execution path and skips the issue (`R:1085-1120` `hasActiveExecutionPath`; skip at 4429-4437). So no backstop run starts either.
2. **It is held by a recovery action (an execution blocker).**
   - When `getExecutionBlocker` finds a recovery action, `deferBlockedExecution` stores the comment wake as deferred, with `payload.executionWait = {reason:"execution_recovery", message:"Waiting for execution recovery. Your message is saved."}` (`H:26614-26676`).
   - The recovery action must have a cause in `EXECUTION_RECONCILIATION_CAUSES` and must be either:
     - `active` or `escalated`, or
     - carrying `evidence.automaticRecovery.replay = 'blocked'`. **This hold survives resolution** (`PC server/src/services/execution-blocker.ts:7-14, 39-45`).
   - The blocked-to-todo revalidation in 9.1 cancels the active action. It does not clear the `replay = 'blocked'` evidence.
   - The causes list is in `PC packages/shared/src/types/execution-projection.ts:37-52`.
3. **A board-owned recovery action is still active, or a pending interaction exists.**
   - The reconciler skips an issue that has a pending `issue_thread_interactions` row with `continuation_policy` `wake_assignee` or `wake_assignee_on_accept` (`R:1154-1169`, skip 4439-4442). That is the case where the agent asked through a card and the owner answered by comment instead of on the card.
   - It also skips an issue that has an active board-owned recovery action (4444-4450). That happens if the revalidation in 9.1 failed.
4. **The run started but cannot be seen from the room.**
   - If the issue is bound to one of the agent's room conversations (the 10-04 duplicate-work fix), the bridge steers the wake into the live room run, or runs it in that room's transcript.
   - Nothing is posted to the room (`LB apps/infra/hermes/bridge/turn.py:4057-4090, 3773-3775`). Paperclip shows a run, and when the steered run ends it comments "Handled in the room conversation that owns this issue" (4035-4036).
   - If the binding's origin is a call, the run first waits for the call's lock (4069-4075).
   - This path does not drop the wake. It only hides it from the room.
5. **The wake throws or is skipped.**
   - Agent not invokable (paused, terminated, pending approval) or `budget.blocked`: the wake throws a 409 and writes a skipped row (`H:26122-26147`). It is caught by the log line in 9.2.
   - With a non-invokable agent, the reconciler sends the issue back to `blocked` within about 30 s (`R:4397-4425`). An over-budget agent is escalated as well (`R:4482`). So "stays todo" makes these causes less likely.
6. **Daily cap (gate 429) or trial pause.** This explains why the issues were `blocked` in the first place, not why they now stay `todo`.
   - The gate refusal makes the bridge turn fail with `provider_quota_exhausted` or `model_unavailable` (`turn.py:2255, 2327, 4119`).
   - The facade rewrites the quota error as "Your … has hit its usage limit — it resets …" (`LB routers/messaging.py:1700-1709`; `core/services/messaging.py:797-815`).
   - Paperclip records `errorCode "hermes_gateway_run_failed"` (`PC .../gateway/server/execute.ts:656`).
   - The quota classifier only reads the message for `adapter_failed`, `provider_quota`, `configuration_incomplete` or openclaw codes (`R:534-560, ~651-661`). So the run is not parked until the reset. It takes the default class: 1 retry, then `blocked` with the "No live execution path / Automatic recovery blocked" notice (`R:531, 3840-3890`; `stranded-notice.ts:27-29`).
   - A trial pause (`pause_owner_automation`) turns off only timers and routines (`LB core/services/paperclip_agent_lifecycle.py:176-234`). Comment wakes still dispatch and fail at the gate in the same way.
   - If the cap is still in force, a woken run fails again and the issue returns to `blocked` (section 3.1). Issues that stay `todo` with no run therefore point to items 1-3.
7. **Concurrency slots** do not drop a wake.
   - Over 20 runs for one agent leaves runs `queued` (`H:19424-19450`).
   - Over 4 turns on the microVM leaves the Paperclip run `running` while it waits (`turn.py:2889`).
   - Both are visible in `heartbeat_runs`.
8. **The 30 s re-dispatch** is the backstop that should have started a run. It does not fire in cases 1-3, or when the latest run was cancelled by an operator (`R:4446`).
9. **The comment-then-cancel race** does not apply. It needs a cancel after the comment, and that leaves the issue `cancelled`, not `todo` (`ceo-heartbeat.md:167-184`).

### 9.4 Most likely causes, in order, and how to confirm each on prod

| # | Cause | Confirm with |
|---|---|---|
| 1 | Deferred comment wake stranded behind a lock holder that ended without a release drain, while the reconciler treats that deferred wake as live | `select id,status,reason,payload->'executionWait',payload->'_paperclipWakeContext'->'wakeCommentIds',requested_at,updated_at from agent_wakeup_requests where payload->>'issueId'='<issue>' order by requested_at desc;` shows `deferred_issue_execution`. Then `select execution_run_id,checkout_run_id from issues where id='<issue>'`, plus that run's `status, runtime_mode` in `heartbeat_runs`. Paperclip logs: `failed to promote stranded legacy comments`. |
| 2 | Execution-blocker hold (`executionWait.reason = execution_recovery`) | the same wake row has `payload.executionWait`; `select id,cause,status,evidence->'automaticRecovery'->>'replay' from issue_recovery_actions where source_issue_id='<issue>'` shows a cause from `EXECUTION_RECONCILIATION_CAUSES` with status `active`/`escalated` or `replay='blocked'`. The UI text is "Waiting for execution recovery. Your message is saved." |
| 3 | Pending card interaction, or a recovery action that is still active (revalidation failed) | `select status,continuation_policy from issue_thread_interactions where issue_id='<issue>'` shows `pending`. `issue_recovery_actions.status='active'`. No `issue.recovery_action_resolved` row in `activity_log` for the comment time. Paperclip log `failed to revalidate recovery action after committed issue write`. |
| 4 | The run did happen, but inside a bound room conversation | a `heartbeat_runs` row where `context_snapshot->>'issueId'='<issue>'` and `context_snapshot->>'wakeReason'='issue_reopened_via_comment'`. Runtime CloudWatch shows `paperclip run <id> joined room run` or `runs in its bound room conversation`. The issue has a comment "Handled in the room conversation…". |
| 5 | Wake threw or was skipped (not invokable, budget, company inactive) | Paperclip log `failed to wake agent on issue comment`; `agent_wakeup_requests.status='skipped'` with `reason` `agent.not_invokable` / `budget.blocked` / `company.inactive`. |
| - | Why the issues were blocked in the first place (daily cap or trial) | the last failed `heartbeat_runs` row before the block has `error_code='hermes_gateway_run_failed'` and an error starting "Your … has hit its usage limit" or `model_unavailable`. The issue has the "Automatic recovery blocked" comment. |

I could not tell from code alone which of 1-3 applies. They share one symptom: the reconciler sees a live path or a hold, so it never re-dispatches. The first query in row 1 separates them.
