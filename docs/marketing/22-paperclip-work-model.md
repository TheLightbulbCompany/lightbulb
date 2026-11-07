# How work flows through the task board (Paperclip fork)

Read from code on 2026-10-14. Fork = `~/Desktop/paperclip-fork.nosync` at `c6f5a4d2e`. Repo paths are relative to `lightbulb.nosync`.

## 1. Issue lifecycle

- Seven statuses exist: backlog, todo, in_progress, in_review, done, blocked, cancelled (`packages/shared/src/constants.ts:189`).
- A create with no status gets `todo` if it has any assignee. Otherwise it gets `backlog` (`packages/shared/src/validators/issue.ts:636-660`, applied at `server/src/routes/issues.ts:513`).
- Create wakes the assigned agent unless the status is backlog (`server/src/services/issue-assignment-wakeup.ts:43`, called at `issues.ts:12013`).
- Our agent helper sends `backlog` when an agent omits the status (`apps/infra/hermes/skills-src/paperclip/SKILL.md:71-80`).
- PATCH wakes the agent only on these changes (`issues.ts:14431-14600`):
  - a new agent assignee, when status is not backlog,
  - backlog to any other status,
  - blocked to todo,
  - done or cancelled to todo,
  - a human moving in_review to todo.
- A PATCH from todo to in_progress, or to in_review, done or cancelled, dispatches nothing.
- All wakes from one PATCH merge into one per agent per issue (`issues.ts:14460-14480`).
- A human comment on a closed, agent-assigned issue reopens it to todo and wakes the agent (`issues.ts:2283`).

## 2. How an agent picks up work

- Wakes come from assignment, comments, checkout, approvals, blockers resolving and the timer heartbeat.
- The CEO timer runs every 8 h (paid), 12 h (trial) or 24 h (own key) (`apps/backend/core/services/user_provision.py:222-233`).
- Specialists wake once a day, and skip the wake if they have no actionable work (`apps/backend/core/services/paperclip_provisioning.py:64-73`).
- A reconciler runs every 30 s (`server/src/config.ts:366`). It re-dispatches every agent-assigned todo, in_progress or in_review issue with no live run (`server/src/services/recovery/service.ts:4257-4277`).
- Checkout is an atomic claim. A second claimant gets 409 "Issue checkout conflict" (`server/src/services/issues.ts:7993-8015`).
- One issue has one active run. Extra wakes for that issue wait as `deferred_issue_execution` (`server/src/services/heartbeat.ts:26639`).
- One agent can run up to 20 issues at once (`packages/shared/src/constants.ts:77`, `heartbeat.ts:19443`).
- The bridge then caps one microVM at 4 turns at once, across all agents (`apps/infra/hermes/bridge/turn.py:2589`).
- So two issues for one agent run in parallel, in separate work folders (`turn.py:2579-2585`).

## 3. Blocked

- Entering blocked needs one of three things. These are an open blocker issue, a pending interaction or approval, or an `unblockDescriptor`. Otherwise the PATCH returns 422 (`issues.ts:13243-13302`).
- An `unblockDescriptor` names an owner and an action. Only an agent owner gets a wake. A board owner gets nothing (`server/src/services/routable-blocked.ts:37-55`).
- When all `blockedBy` issues reach done, the dependent assignee wakes. When all children close, the parent assignee wakes (`skills-src/paperclip/references/heartbeat.md:170-171`).
- Nothing re-checks a blocked issue after that (`heartbeat.md:186`).
- Platform failures (timeout, lost process) retry, then block the issue on a "recovery owner". Our backend then hands it to Engineering, then the CEO (`apps/backend/core/services/paperclip_escalation.py:1-21`).

## 4. Approvals and "Needs you"

- Four approval types exist: `hire_agent`, `approve_ceo_strategy`, `budget_override_required`, `request_board_approval` (`packages/shared/src/constants.ts:689-694`). There is no "public post" or "spend" type.
- An agent raises one with `POST /api/companies/:id/approvals` and links issues (`references/interactions.md:5-22`).
- Marketplace hires file `hire_agent` (`apps/backend/core/services/marketplace_hire.py:285`).
- "Ask first" tools post an approve-and-run card. It expires after 60 minutes (`SKILL.md:229-245`).
- Four issue-thread cards also wait on people: confirm, checkbox confirm, questions, suggested tasks (`references/interactions.md`).
- Any human board user may decide. Paperclip only checks `assertBoard` (`server/src/routes/approvals.ts:286-288`). Our BFF adds no admin check (`apps/backend/routers/teams/approvals.py:69-95`).
- While waiting, the run ends. The agent parks the issue in in_review or blocked (`references/heartbeat.md:95-98`).
- Approve wakes the requesting agent (`approvals.ts:310-330`).
- Reject does not wake the requester. It only wakes issues whose review path was lost (`approvals.ts:402-433`).
- Any human comment cancels a pending confirm card (`references/interactions.md:44`).
- "Needs you" is the attention feed with approvals and cards attached (`apps/backend/routers/teams/attention.py:210-230`).

## 5. Reviews and done

- An agent can enter in_review only with a real review path. That is a pending card, a pending approval, a human assignee, an execution-policy reviewer or a monitor. Otherwise 422 (`issues.ts:4627-4745`).
- Review policy can be `anyone`, `not_creator` or `human_only` (`issues.ts:5850-5900`).
- A reviewer approves by setting done. More stages keep it in in_review with the next reviewer (`references/heartbeat.md:80-84`).
- No PR review is wired to an issue. Paperclip keeps only a PR snapshot, with no manual add route (`SKILL.md:140`, `SKILL.md:220-222`).
- A monitor waits for a PR merge. Merging still needs an approval (`SKILL.md:192-199`).
- Done is just a PATCH. No gate checks the work.

## 6. Human assignees

- An issue can carry `assigneeUserId`. The reconciler skips those issues (`recovery/service.ts:4265`). Nothing nudges the person.
- They appear in the person's "Mine" inbox (`apps/backend/routers/teams/inbox.py:46-51`). "Mine" also includes every issue the person created or touched (`server/src/services/issues.ts:2767-2785`).
- An agent may return an issue to its human creator without assign rights (`issues.ts:13356-13366`). Other user assignments need `tasks:assign`.
- The skill tells agents to hand work back with `in_review` (`SKILL.md:151`).
- A human hands it back by assigning an agent. Our BFF also flips blocked to todo then (`apps/backend/routers/teams/issues.py:250-256`).

## 7. Parent, child and blocker relations

- The BFF creates children with `POST /teams/issues/{id}/children` (`routers/teams/issues.py:667-683`).
- The BFF PATCH accepts `parentId` and `blockedByIssueIds` (`routers/teams/schemas.py:140`, `:153`).
- The blocker list replaces the whole set on each PATCH (`references/heartbeat.md:164`).
- There is no BFF route for a "blocks" edge alone. You set it from the blocked side.

## 8. Where a human must act

1. Every approval: hire, CEO strategy, budget override, board approval.
2. Every "ask first" tool call, within 60 minutes.
3. Confirm cards, question cards and suggested-task cards.
4. Reviews where a human is the reviewer or policy is `human_only`.
5. Moving backlog issues to todo. Agents file to backlog by default.
6. Blocked issues with a board owner. Nothing wakes for them.
7. Issues assigned to a person. Nothing reminds them.
8. PR merge approval and prod deploy dispatch (repo rule, not product).
9. Recovery exhausted: "a board decision is required" (`server/src/services/recovery/successful-run-handoff.ts:17-18`).

## 9. Known failure modes for big batches

- **Status default.** An assigned create without a status starts a run at once (`SKILL.md:71`).
- **Reconciler re-dispatch.** Assigned active issues re-dispatch every 30 s. Work claimed in a room turn ran twice on 2026-10-04. Those wakes now join the room conversation (`references/heartbeat.md:11`).
- **Blocked snapback.** Assigning a blocked issue to an agent cancels the new run. The issue snaps back to the recovery owner (`routers/teams/issues.py:250-253`).
- **Comment-then-cancel.** Two-step closes break. Comment-then-cancel left a run stuck. Cancel-then-comment reopens and wakes the agent (`issues.ts:2283`). Use one PATCH with status and comment.
- **Orphan blockers.** An unassigned blocker goes back to its creator within a minute (`references/heartbeat.md:98`).
- **Card supersede.** Any human comment kills a pending confirm card (`references/interactions.md:44`).
- **Silent reject.** A rejected approval does not wake the requester (`approvals.ts:402-433`).

## Friction to remove

Ranked by founder time saved.

1. **Wake the requester on reject.** Add the same requester wake to the reject handler. Today a rejected task stalls until a human comments. Code change in the fork (`approvals.ts:402`).
2. **Batch decisions in "Needs you".** Add "approve all from this agent" for low-risk kinds. Code change in the BFF and UI.
3. **Auto-approve low-risk tool calls.** Mark read-only tools as allowed, not "ask first". Config per workspace.
4. **Let agents promote their own backlog.** Let the CEO move specialist backlog to todo on its timer. Policy change in the CEO skill.
5. **Default review policy to `anyone`.** Let a second agent review. Keep `human_only` for money and public posts. Policy change.
6. **Remind human assignees.** Add a daily digest of issues on `assigneeUserId`, and a nudge after 24 h. Code change in the backend.
7. **Wake board-owned blocked issues.** Put them in "Needs you" with the action text. Code change in the attention feed.
8. **Fix the batch traps once.** Add a BFF bulk close that sends one PATCH with status and comment. Code change. (Room-claimed wakes already join the room, `references/heartbeat.md:11`.)
9. **Trim "Mine".** Default it to issues assigned to me, not every issue I created. One BFF filter change (`inbox.py:47-51`).
