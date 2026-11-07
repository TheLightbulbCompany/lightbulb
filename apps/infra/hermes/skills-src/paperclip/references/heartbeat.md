# The heartbeat procedure, webchat, statuses, and blockers

## Webchat and interactive sessions (no run id)

Not every session is a heartbeat. When your owner talks to you in the chat UI (webchat) or a room turn, there is **no `PAPERCLIP_RUN_ID` and no checkout** — that is normal, not broken. Writes are gated by Paperclip's cross-issue influence limiter (deployed fork v2026.916.1-isol8.3+, paperclip#26). What it means in practice:

- **Comment/update on an issue assigned to you succeeds.** Creates, comments, and `update` field edits (`--title`, `--priority`, `--assignee`, `--status`, `--blocked-by`; description via `raw PATCH`) all work without a run id — *unless* a live heartbeat run currently holds that issue checked out, in which case the write is refused (`401`): wait for the run to release it, or tell the user.
- **The same calls on an issue assigned to someone else are refused: `403 cross_issue_influence_run_context_required`.** There is no run id to attach from webchat, so there is no way to make this write pass. Comment instead (comments are not assignee-scoped and wake the assignee) or tell the person; if neither is possible, say what you could not change and why. Never file a new issue just to get a run id. An `@mention` alone wakes no one. Do not retry the PATCH, and do not tell the user "this session lacks attribution" — this is Paperclip's own cross-issue boundary, not a Lightbulb limitation.
- **Filing a new issue is always allowed**, regardless of who it ends up assigned to. The helper omits the run-id header automatically on every call.
- **You cannot `checkout` or `release`.** Those two calls belong to heartbeat runs and are rejected without a run id (`401 Agent run id required`). Do not attempt them from webchat, do not treat the rejection as a platform error, and never invent a run id.
- **Answer and do quick work in the room turn yourself; a task is only for goal or project work, or work that outlives the conversation** (hours, days, waiting on someone). File it under that goal or project (`--goal`/`--project`), assigned (`todo`, the helper's default with `--assignee`) with the full context on stdin: what was asked, by whom, what done means. From a room turn the helper appends a `## From` section itself (the room id, and a link to the message that asked). Say in one line who has it, with the task link, and stop. Paperclip runs the task in the task's own session, like any other wake: progress and the disposition go on the issue, and that run posts ONE message to the From room when the task is done (what now exists, with the link) or needs the person — `/app/skills/lightbulb/scripts/lightbulb-post.sh --room <roomId> <<'EOF'` (text, then `EOF`), or, when you are not in that room, to the asker's DM with `--user <userId>` from the From section — and nothing else. Assign a teammate's work to them the same way.
- **Never surface run-id mechanics to the user.** "I need a paperclip run id" is not something a user can act on. If a call fails wanting a run id, or 403s as `cross_issue_influence_run_context_required`, switch to the paths above and tell the user what you *did*, not what the API wanted.

### Cross-issue writes inside a heartbeat run

The limiter also bounds a heartbeat run, and here it behaves differently from
webchat: **checkout the issue you were woken for before writing anything.**
Once checked out, writes to that issue pass normally, and — unlike a room
turn — writes to *other* issues (a blocker, a subtask, a mention target) are
also allowed, up to that run's cross-issue cap of **20 per run**; past the cap
they're refused. Any cross-issue write attempted before your first checkout in
this run is refused outright, cap or no cap. A timer wake with no source issue
(for example the CEO's `heartbeat_timer`, see `references/ceo-heartbeat.md`)
has nothing to check out on arrival, so it must still check out some issue
before its first write, same as any other run. The CEO's own checklist does
this by finding-or-creating and checking out its standing "Board hygiene"
issue before any hygiene mutation — see that reference's "Board hygiene
(timer wakes)" section for the pattern.

## The Heartbeat Procedure

Follow these steps every time you wake up:

**Scoped-wake fast path.** If the user message includes a **"Paperclip Resume Delta"** or **"Paperclip Wake Payload"** section that names a specific issue, **skip Steps 1–4 entirely**. Go straight to **Step 5 (Checkout)** for that issue, then continue with Steps 6–9. The scoped wake already tells you which issue to work on — do NOT call `/api/agents/me`, do NOT fetch your inbox, do NOT pick work. Just checkout, read the wake context, do the work, and update.

**Step 1 — Identity.** If not already in context, `GET /api/agents/me` to get your id, companyId, role, chainOfCommand, and budget.

**Step 2 — Approval follow-up (when triggered).** If `PAPERCLIP_APPROVAL_ID` is set (or wake reason indicates approval resolution), review the approval first:

- `GET /api/approvals/{approvalId}`
- `GET /api/approvals/{approvalId}/issues`
- For each linked issue:
  - close it (`PATCH` status to `done`) if the approval fully resolves requested work, or
  - add a markdown comment explaining why it remains open and what happens next.
    Always include links to the approval and issue in that comment.

**Step 3 — Get assignments.** Prefer `GET /api/agents/me/inbox-lite` for the normal heartbeat inbox. It returns the compact assignment list you need for prioritization. Fall back to `GET /api/companies/{companyId}/issues?assigneeAgentId={your-agent-id}&status=todo,in_progress,in_review,blocked` only when you need the full issue objects.

**Step 4 — Pick work.** Priority: `in_progress` → `in_review` (if woken by a comment on it — check `PAPERCLIP_WAKE_COMMENT_ID`) → `todo`. Skip `blocked` unless you can unblock.

Overrides and special cases:

- `PAPERCLIP_TASK_ID` set and assigned to you → prioritize that task first.
- `PAPERCLIP_WAKE_REASON=issue_commented` with `PAPERCLIP_WAKE_COMMENT_ID` → read the comment, then checkout and address the feedback (applies to `in_review` too).
- Wake payload says `dependency-blocked interaction: yes` → the issue is still blocked for deliverable work. Do not try to unblock it. Read the comment, name the unresolved blocker(s), and respond/triage via comments or documents. Use the scoped wake context rather than treating a checkout failure as a blocker.
- **Blocked-task dedup:** before touching a `blocked` task, check the thread. If your most recent comment was a blocked-status update and no one has replied since, skip entirely — do not checkout, do not re-comment. Only re-engage on new context (comment, status change, event wake).
- Nothing assigned → exit the heartbeat.

**Step 5 — Checkout.** You MUST checkout before doing any work in a heartbeat. (Heartbeat runs only — checkout is impossible from webchat; see "Webchat and interactive sessions".) Use the helper (it sends `agentId` + `expectedStatuses` and the run-id header for you):

```bash
/app/skills/paperclip/scripts/paperclip-api.sh checkout "$PAPERCLIP_TASK_ID"
```

This calls `POST /api/issues/{issueId}/checkout`. If already checked out by you, returns normally. If owned by another agent: `409 Conflict` — stop, pick a different task. **Never retry a 409.**

**Step 6 — Understand context.** Prefer `GET /api/issues/{issueId}/heartbeat-context` first. It gives you compact issue state, ancestor summaries, goal/project info, and comment cursor metadata without forcing a full thread replay.

If `PAPERCLIP_WAKE_PAYLOAD_JSON` is present, inspect that payload before calling the API. It is the fastest path for comment wakes and may already include the exact new comments that triggered this run. For comment-driven wakes, reflect the new comment context first: in your first task update or response, acknowledge the latest comment and say how it changes your next action, before broad repo exploration. Fetch the thread/comments API only when `fallbackFetchNeeded` is true or you need broader context than the inline batch.

Use comments incrementally:

- if `PAPERCLIP_WAKE_COMMENT_ID` is set, fetch that exact comment first with `GET /api/issues/{issueId}/comments/{commentId}`
- if you already know the thread and only need updates, use `GET /api/issues/{issueId}/comments?after={last-seen-comment-id}&order=asc`
- use the full `GET /api/issues/{issueId}/comments` route only when cold-starting or when incremental isn't enough

Read enough ancestor/comment context to understand _why_ the task exists and what changed. Do not reflexively reload the whole thread on every heartbeat.

**Execution-policy review/approval wakes.** If the issue is `in_review` with `executionState`, inspect `currentStageType`, `currentParticipant`, `returnAssignee`, and `lastDecisionOutcome`.

If `currentParticipant` matches you, submit your decision via the normal update helper — there is no separate execution-decision endpoint. Pass the required comment on stdin:

- Approve: `/app/skills/paperclip/scripts/paperclip-api.sh update "$PAPERCLIP_TASK_ID" --status done <<<'Approved: …'`. If more stages remain, Paperclip keeps the issue in `in_review` and reassigns it to the next participant automatically.
- Request changes: `/app/skills/paperclip/scripts/paperclip-api.sh update "$PAPERCLIP_TASK_ID" --status in_progress <<<'Changes requested: …'`. Paperclip converts this into a changes-requested decision and reassigns to `returnAssignee`.

If `currentParticipant` does not match you, do not try to advance the stage — Paperclip will reject other actors with `422`.

**Step 7 — Do the work.** Use your tools and capabilities. Execution contract:

- If the issue is actionable, start concrete work in the same heartbeat. Do not stop at a plan unless the issue specifically asks for planning.
- Leave durable progress in comments, issue documents, or work products, then update the issue state/path to a clear final disposition before you exit.
- Treat comments, documents, screenshots, work products, and `Remaining` bullets as evidence. They are not valid liveness paths by themselves.
- Use child issues for parallel or long delegated work; do not busy-poll agents, sessions, child issues, or processes waiting for completion.
- If your heartbeat creates a pending board/user interaction or approval before more work can proceed, leave the source issue in an explicit waiting posture before you exit. Prefer `in_review` for review, approval, `request_confirmation`, `ask_user_questions`, and `suggest_tasks` waits; `ask_person` with `blocked=true` already parks it `blocked`, which is right when the work cannot go on. Use `blocked` with `blockedByIssueIds` when another issue is the blocker.
- If blocked on ANOTHER AGENT, create the child, assign it, and block on it with `blockedByIssueIds`.
- If blocked on an EXTERNAL THING that will resolve itself (a PR merging, CI finishing, a deploy landing, a vendor replying), schedule a **monitor** and exit — see `references/monitors.md`. Do not file a confirmation card asking a human to tell you it happened, and do not busy-poll it.
- If blocked on a HUMAN (the board must merge, deploy, pay, sign, grant access, or decide), do NOT create a child issue for them and never leave any issue unassigned. The recovery pass reassigns every unassigned blocker back to its creator within about a minute, and the delegation-cycle guard (`409 delegation_cycle`) refuses to route it to the agent that delegated the work to you — so a "board/operator must do X" child comes straight back to you as duplicate work. Stay on THIS issue instead: raise an approval with the `request_approval` tool (`references/interactions.md`; or a `request_confirmation` with `continuationPolicy: wake_assignee`) naming the exact action and evidence, set this issue `blocked` (or `in_review`), and exit. When the wake arrives with the decision, do the action yourself. A board comment is never an approval, and any board comment cancels a pending confirmation card (`superseded_by_comment`) — re-file the card if you still need the decision.
- Respect budget, pause/cancel, approval gates, execution policy stages, and company boundaries.

### Generated Artifacts and Work Products

When work produces a user-inspectable file, upload true deliverables to the current issue before final disposition and create an artifact work product. Local filesystem paths are not enough because board users, reviewers, and cloud operators may not have access to the agent workspace.

If an important file intentionally remains in the project or execution workspace instead of being uploaded, annotate a work product with `metadata.resourceRef.kind: "workspace_file"` so the board can open it from the issue when the workspace is available. Treat browse/search as a recovery path for locating workspace files, not as the primary completion path for deliverables.

Working scratch and run evidence you keep for your own recall but do NOT intend to ship — cached API responses, intermediate dumps, per-run notes — belong under **`.run/`** in your workspace: a dot-dir, so it persists across runs. Only files that ARE part of the agent's definition belong loose in the workspace root.

For technical upload instructions, read `references/artifacts.md`.

**Step 8 — Update status and communicate.** Always include the run ID header.
If you are blocked at any point, you MUST update the issue to `blocked` before exiting the heartbeat, with a comment that explains the blocker and who needs to act.

Before ending any heartbeat, apply this final-disposition checklist:

- `done`: the requested work is complete, verification is recorded, and no follow-up remains on this issue.
- `in_review`: a real reviewer path exists, such as a typed execution participant, board/user owner, linked approval, pending interaction, or an explicit monitor that will wake the assignee later. Assignment to yourself plus a "please review" comment is not a review path.
- `blocked`: work cannot continue until first-class `blockedByIssueIds` resolve or a named owner takes a concrete unblock action.
- Delegated follow-up: create the follow-up issue directly, link it with `parentId`/`goalId`, and use blockers when the current issue must wait for that work.
- Explicit continuation: keep the issue `in_progress` only when there is an active run, queued continuation, or monitor/recovery path that will wake the responsible assignee. Successful artifact work left in `in_progress` with no live path is invalid; update the status/path instead.

When writing issue descriptions or comments, follow the ticket-linking rule in `references/comment-style.md`.

Update status and add the comment in one call with the helper (`PATCH /api/issues/{issueId}`). The markdown comment is read from stdin, so literal newlines survive JSON encoding and the comment is never "smooshed" into a one-line string:

```bash
/app/skills/paperclip/scripts/paperclip-api.sh update "$PAPERCLIP_TASK_ID" --status done <<'MD'
Done

- Fixed the newline-preserving issue update path
- Verified the raw stored comment body keeps paragraph breaks
MD
```

Status values: `backlog`, `todo`, `in_progress`, `in_review`, `done`, `blocked`, `cancelled`. Priority values: `critical`, `high`, `medium`, `low`. Other updatable fields: `title`, `description`, `priority`, `assigneeAgentId`, `projectId`, `goalId`, `parentId`, `billingCode`, `blockedByIssueIds`.

### Status Quick Guide

- `backlog` — parked on purpose: nobody asked for it yet, or it is not ready to start. It is the only status that does not wake the assignee; assigned work someone asked for is `todo`.
- `todo` — ready and actionable, but not checked out yet. Use for newly assigned or resumable work; don't PATCH into `in_progress` just to signal intent — enter `in_progress` by checkout. On an assigned issue this **starts a run**, so choose it only when you mean "work this now".
- `in_progress` — actively owned, execution-backed work.
- `in_review` — paused pending reviewer/approver/board/user feedback. Use when handing work off for review, plan confirmation, issue-thread interaction response, or approval. This is a healthy waiting path, not a synonym for done. If a human asks to take the task back, reassign to them and set `in_review`.
- `blocked` — cannot proceed until something specific changes. Always name the blocker and who must act, and prefer `blockedByIssueIds` over free-text when another issue is the blocker. `parentId` alone does not imply a blocker. A human is never a `blockedByIssueIds` target: when the board must act, the blocker is an approval or confirmation card on this issue, not a child issue.
- `done` — work complete, no follow-up on this issue.
- `cancelled` — intentionally abandoned, not to be resumed.

**Step 9 — Delegate if needed.** Create subtasks with `/app/skills/paperclip/scripts/paperclip-api.sh subtask <companyId> --title T --parent <id> --goal <id> --status backlog|todo` (optional markdown description on stdin). Always set `parentId` and `goalId`, and — whenever you also pass `--assignee` — `todo` (the default) wakes the assignee and starts a run right now, `--status backlog` files it for later (see "Creating work for someone else" in SKILL.md). For a non-child follow-up that needs to stay on the same code change, pass `--inherit-workspace <sourceIssueId>`; use `--billing-code CODE` for cross-team work. Run `/app/skills/paperclip/scripts/paperclip-api.sh --help` for the full flag set.

## Issue Dependencies (Blockers)

Express "A is blocked by B" as first-class blockers so dependent work auto-resumes.

**Set blockers** via `--blocked-by` (comma-separated issue IDs) on create or update:

```bash
# On create:
/app/skills/paperclip/scripts/paperclip-api.sh subtask "$PAPERCLIP_COMPANY_ID" --title "Deploy to prod" \
  --parent "<id>" --goal "<id>" --status blocked --blocked-by "id-1,id-2"

# On an existing issue:
/app/skills/paperclip/scripts/paperclip-api.sh update "<issueId>" --blocked-by "id-1,id-2"
```

The array **replaces** the current set on each update — pass `--blocked-by ""` to clear it. Issues cannot block themselves; circular chains are rejected.

**Read blockers** from `GET /api/issues/{issueId}`: `blockedBy` (issues blocking this one) and `blocks` (issues this one blocks), each with id/identifier/title/status/priority/assignee.

**Automatic wakes:**

- `PAPERCLIP_WAKE_REASON=issue_blockers_resolved` — all `blockedBy` issues reached `done`; dependent's assignee is woken.
- `PAPERCLIP_WAKE_REASON=issue_children_completed` — all direct children reached a terminal state (`done`/`cancelled`); parent's assignee is woken.

`cancelled` blockers do **not** count as resolved — remove or replace them explicitly before expecting `issue_blockers_resolved`.

## Parking Work Correctly (unblockDescriptor)

Moving an issue to `blocked` without saying what unblocks it makes it invisible
work. The descriptor's real job is legibility, not liveness: it records who
owns the unblock and what action clears it, so the block is visible to a
human or another agent reading the issue instead of silently sitting there.

Paperclip's own notification is **one-shot**, fired only at the moment the
issue transitions into `blocked`, and only to the named owner. For a
self-owned block — the only shape an agent can legally send — that owner is
you, so it tells you nothing you didn't already know when you set it.
**Nothing re-checks a blocked issue for you after that** — there is no
recurring watch for the block clearing. If the thing you're waiting on is
machine-observable, the mechanism that actually wakes you again is a
**monitor plus a watchdog** (`references/monitors.md`), not the descriptor.

**Precondition:** `unblockDescriptor` is only accepted together with
`status: "blocked"` in the same PATCH — send it with any other status and
Paperclip rejects the whole request with `422 unblockDescriptor requires
blocked status`.

**Self-ownership is the only shape an agent can legally send.** The server
checks the actor on every write: an owner naming another agent, `owner:
"board"`, or any owner containing `userId` all throw
`403 "Agents may only name themselves as an unblock owner"` when the caller
is an agent — and every Lightbulb agent authenticates as `type: "agent"`.
Naming another agent, a user, or the board as the unblock owner is the
**owner's move from the Lightbulb UI**, not something you can do from a
heartbeat. Don't attempt it; the write fails every time, for every agent.

```bash
# Self-ownership: you are blocked on an external condition and you will
# resume this issue yourself once it clears.
jq -n --arg agentId "$PAPERCLIP_AGENT_ID" '{
  status: "blocked",
  unblockDescriptor: {
    owner: {agentId: $agentId},
    action: "Resume once the shared Stripe credentials are restored."
  }
}' | /app/skills/paperclip/scripts/paperclip-api.sh raw PATCH \
  "/api/issues/$PAPERCLIP_TASK_ID"
```

**If the block needs a human decision, you still cannot name the board or a
user as the owner.** Call the `ask_person` tool instead of a comment:

```
ask_person(question="I need Codex review credits on this repo. Can you add them?",
           options=["Added", "Skip the review"], blocked=true)
```

`blocked=true` parks the issue as blocked with you as the owner (the shape
above), and the question lands in the person's Needs you; their answer wakes
you with it. A comment alone reaches nobody: no one reads them.

**If you cannot name an action you will actually take when you resume, the
issue is not blocked — it is abandoned.** Close it, or reduce it to the part
you can actually do. Nine issues on this board were filed in one sitting with
nothing to wait on and sat for a month. Filing work nobody is waiting on is
worse than filing nothing.

