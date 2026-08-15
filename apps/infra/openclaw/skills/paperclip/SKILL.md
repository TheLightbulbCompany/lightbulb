---
name: paperclip
description: >
  Interact with the Paperclip control plane API to manage tasks, coordinate with
  other agents, and follow company governance. Use when you need to check
  assignments, update task status, delegate work, post comments, set up or manage
  routines (recurring scheduled tasks), create governed decisions, search company
  memory, manage decision queues/triage, or call any Paperclip API endpoint. Do NOT
  use for the actual domain work itself (writing code, research, etc.) — only for
  Paperclip coordination.
---

<!--
  ISOL8-LOCAL EDITS (deliberate; re-apply on re-vendor).
  This skill is vendored from the Paperclip skills-catalog, but carries
  Isol8-local changes that upstream does not have. If you re-vendor, re-apply
  each of these rather than letting them silently revert:

  1. Write-path helper mandate ("Writing to the Paperclip API" section + the
     write tables): every API write goes through /app/skills/paperclip/scripts/paperclip-api.sh
     (jq-built bodies); the raw-jq / inline-JSON escape hatches were removed,
     because hand-written bodies once produced a malformed payload like
     {body:} that silently failed the write and stalled the run.
  2. Run-id truthfulness ("Authentication" + "Run audit trail"):
     PAPERCLIP_RUN_ID exists ONLY on heartbeat-wake runs; webchat/interactive
     sessions have no run id, and the X-Paperclip-Run-Id header must be
     OMITTED there — never invented. Fabricated run ids fail requests.
  3. Board hygiene ("Writing to the Paperclip API"): never create probe/test
     issues on the owner's live board to debug API behavior; use --dry-run.
  4. Scripts: /app/skills/paperclip/scripts/paperclip-api.sh and /app/skills/paperclip/scripts/paperclip-issue-update.sh
     treat PAPERCLIP_RUN_ID as optional (header sent only when set), and
     paperclip-api.sh carries an Isol8-added top-level `create` subcommand,
     a `raw <METHOD> </api/...path>` escape hatch (jq-validated stdin
     body) so no write ever needs hand-rolled curl, and `update` field
     flags (--title/--priority/--assignee) so the webchat section's
     assign-to-start command actually works through the helper.
  5. ABSOLUTE helper paths: every scripts/ invocation is written as
     /app/skills/paperclip/scripts/... (the image bakes this skill at
     /app/skills/paperclip — Dockerfile Layer). The agent's working directory
     is its workspace, NOT the skill root, so relative `scripts/...` commands
     fail with "not found" and the first-run kickoff silently loses its board
     writes. Keep paths absolute when re-vendoring.
  6. EXPLICIT create status ("Creating work for someone else" + the create/
     subtask rows): paperclip-api.sh sends an explicit `backlog` when a create
     passes --assignee with no --status, instead of letting the server pick.
     Paperclip's own default promotes that case to `todo`
     (resolveCreateIssueStatusDefault), and assignee + non-backlog is exactly
     what dispatches an agent run — then a 30s reconciler re-dispatches it
     until it escalates to `blocked`. Upstream's docs are silent on this, so
     an agent filing a few assigned tasks in one heartbeat starts a run storm.
     Keep both the helper default and the doc wording on re-vendor.
  7. Webchat capability section ("Webchat and interactive sessions"): upstream
     documents only the heartbeat worldview; Isol8 owners drive agents from a
     live chat surface daily. Added after a first-run Friday, asked in webchat
     to adjust tasks it had just created, tried to checkout, got "Agent run id
     required", and relayed that to the owner — instead of just PATCHing the
     fields, or assigning + `todo` to start a real run.
  8. Company-memory and governance primitives: the deployed fork includes
     company search/extraction, run-scoped decisions with server-applied
     effects, own-agent ruleKey stats, and decision queues/triage. Dedicated
     helper commands encode their payloads and query strings. The unified
     attention feed is documented truthfully as board-only, not agent-readable.
-->

# Paperclip Skill

You run in **heartbeats** — short execution windows triggered by Paperclip. Each heartbeat, you wake up, check your work, do something useful, and exit. You do not run continuously. You may also find yourself talking to your owner in a live **webchat session**, which is not a heartbeat — see "Webchat and interactive sessions" below for what changes there.

## Terminology

In Paperclip, **task** and **issue** refer to the same work item. The UI may use "task" while APIs, database fields, route names, and older docs may still say "issue"; treat them as the same entity unless a local context explicitly distinguishes them.

## Authentication

Env vars auto-injected: `PAPERCLIP_AGENT_ID`, `PAPERCLIP_COMPANY_ID`, `PAPERCLIP_API_URL`. `PAPERCLIP_RUN_ID` exists **only on heartbeat-wake runs** (the wake prompt injects it) — webchat and other interactive sessions have **no run id**. Optional wake-context vars may also be present: `PAPERCLIP_TASK_ID` (issue/task that triggered this wake), `PAPERCLIP_WAKE_REASON` (why this run was triggered), `PAPERCLIP_WAKE_COMMENT_ID` (specific comment that triggered this wake), `PAPERCLIP_APPROVAL_ID`, `PAPERCLIP_APPROVAL_STATUS`, and `PAPERCLIP_LINKED_ISSUE_IDS` (comma-separated). For local adapters, `PAPERCLIP_API_KEY` is auto-injected as a short-lived run JWT. For non-local adapters, your operator should set `PAPERCLIP_API_KEY` in adapter config. All requests use `Authorization: Bearer $PAPERCLIP_API_KEY`. All endpoints under `/api`, all JSON. Never hard-code the API URL.

Some adapters also inject `PAPERCLIP_WAKE_PAYLOAD_JSON` on comment-driven wakes. When present, it contains the compact issue summary and the ordered batch of new comment payloads for this wake. Use it first. For comment wakes, treat that batch as the highest-priority new context in the heartbeat: in your first task update or response, acknowledge the latest comment and say how it changes your next action before broad repo exploration or generic wake boilerplate. Only fetch the thread/comments API immediately when `fallbackFetchNeeded` is true or you need broader context than the inline batch provides.

Manual local CLI mode (outside heartbeat runs): use `paperclipai agent local-cli <agent-id-or-shortname> --company-id <company-id>` to install Paperclip skills for Claude/Codex and print/export the required `PAPERCLIP_*` environment variables for that agent identity.

**Run audit trail:** When `PAPERCLIP_RUN_ID` is set (heartbeat-wake runs), you MUST include `-H 'X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID'` on ALL API requests that modify issues (checkout, update, comment, create issue/subtask, release). This links your actions to the current heartbeat run for traceability. If you have **no run id** (webchat/interactive sessions), OMIT the `X-Paperclip-Run-Id` header entirely — **NEVER invent or fabricate one**; fabricated run ids cause request failures. `/app/skills/paperclip/scripts/paperclip-api.sh` handles this for you: it sends the header when `PAPERCLIP_RUN_ID` is set and omits it when unset.

## Writing to the Paperclip API (required)

**NEVER hand-write `jq` or inline JSON for request bodies. ALWAYS use `/app/skills/paperclip/scripts/paperclip-api.sh <subcommand>` — it builds the JSON safely.** Hand-built JSON has produced malformed payloads like `{body:}` that silently fail writes and stall the run. The helper takes plain arguments and stdin (markdown bodies, prompts, reasons) and emits a valid, string-escaped body for you; it also sets the auth header (plus `X-Paperclip-Run-Id` when `PAPERCLIP_RUN_ID` is set) and fails loudly on any non-2xx response instead of failing silently.

**Board hygiene:** the board is the owner's live UI — **never create probe/test issues to debug API behavior**. To inspect the JSON a helper call would send, add `--dry-run` instead of firing a live request.

Every write you do in a heartbeat has a subcommand:

| Write | Command |
| ----- | ------- |
| Comment on an issue | `/app/skills/paperclip/scripts/paperclip-api.sh comment <issueId>` (markdown body on stdin) |
| Update status / fields / comment / blockers | `/app/skills/paperclip/scripts/paperclip-api.sh update <issueId> [--status S] [--title T] [--priority P] [--assignee <agentId>] [--blocked-by "id1,id2"]` (optional comment on stdin) |
| Checkout | `/app/skills/paperclip/scripts/paperclip-api.sh checkout <issueId>` |
| Release | `/app/skills/paperclip/scripts/paperclip-api.sh release <issueId>` |
| Create subtask | `/app/skills/paperclip/scripts/paperclip-api.sh subtask <companyId> --title T --parent <id> --goal <id> --status backlog\|todo` (optional description on stdin) |
| Create top-level issue | `/app/skills/paperclip/scripts/paperclip-api.sh create <companyId> --title T [--goal <id>] [--assignee <agentId>] --status backlog\|todo` (optional description on stdin) |
| Any other write (no subcommand) | `/app/skills/paperclip/scripts/paperclip-api.sh raw <METHOD> </api/...path>` (jq-built JSON body on stdin) |
| `request_confirmation` interaction | `/app/skills/paperclip/scripts/paperclip-api.sh confirm <issueId> --prompt P` (optional details markdown on stdin) |
| Accept / reject / respond an interaction | `/app/skills/paperclip/scripts/paperclip-api.sh {accept,reject,respond} <issueId> <interactionId>` |
| Create governed decision | `jq -n '{body: ..., options: [...]}' \| /app/skills/paperclip/scripts/paperclip-api.sh decision <companyId> --title T --rule-key K --idempotency-key K` |
| Read own decision / decision stats | `/app/skills/paperclip/scripts/paperclip-api.sh decision-get <decisionId>` • `decision-stats <companyId> [--since ISO]` |
| Search company memory / extract literals | `/app/skills/paperclip/scripts/paperclip-api.sh {search,extract} <companyId> ...` |
| Decision queues / triage | `/app/skills/paperclip/scripts/paperclip-api.sh {queue-list,queue-items,queue-create,queue-add,triage} <companyId> ...` |

Run `/app/skills/paperclip/scripts/paperclip-api.sh --help` for the full flag list, and add `--dry-run` to print the JSON or encoded read URL without making the request. Use dedicated helper commands for the company search, decision, queue, and triage reads documented here; other GETs may stay plain `curl`. For the rare write kinds without a dedicated subcommand (board approvals, `request_checkbox_confirmation` / `ask_user_questions` / `suggest_tasks` creates, issue documents), use `/app/skills/paperclip/scripts/paperclip-api.sh raw <METHOD> </api/...path>` with the JSON body on stdin — build the body with `jq -n` from the exact shape in `references/api-reference.md`, never as a hand-typed one-line JSON string. `raw` validates that the body parses before sending and sets the same auth/run-id headers as every other subcommand; **never fall back to hand-rolled `curl` for a write**.

### Creating work for someone else — always say the status

**A new issue with an assignee and a status other than `backlog` STARTS AN AGENT RUN immediately** — and a server-side reconciler re-dispatches it every ~30s until it escalates to `blocked`. That is not a one-off cost: file three assigned tasks with the wrong status and you have started three runs the owner never asked for, over and over.

So on **every** `create` / `subtask` that names an `--assignee`, pass `--status` and mean it:

| You want | Pass | What happens |
| -------- | ---- | ------------ |
| Put it on the board for later (the normal case) | `--status backlog` | Filed, visible, assigned. **No run.** Someone moves it to `todo` when it is time. |
| Assign it AND start it right now | `--status todo` | Assignee is woken immediately and works it. Spends money. |

Omitting `--status` on an assigned create is **not neutral**: Paperclip's API silently promotes it to `todo` and starts the run. `paperclip-api.sh` defends against this by sending an explicit `backlog` (and printing a notice) whenever you pass `--assignee` with no `--status` — but rely on the guard only as a backstop. Say what you mean.

Planning output — "here are your next three moves", a decomposition, a proposal — goes to **`backlog`**. Only file to `todo` when starting the work *now* is the thing being asked for.

Creates with **no** assignee are unaffected: leaving `--status` off is fine, and lands the issue in `backlog`.

## Webchat and interactive sessions (no run id)

Not every session is a heartbeat. When your owner talks to you in the chat UI (webchat), there is **no `PAPERCLIP_RUN_ID` and no checkout** — that is normal, not broken. What it means in practice:

- **You can still read everything and write almost everything.** Creates, comments, and `update` field edits (`--title`, `--priority`, `--assignee`, `--status`, `--blocked-by`; description via `raw PATCH`) all work without a run id on issues that are not actively being worked. The helper omits the run-id header automatically.
- **You cannot `checkout` or `release`.** Those two calls belong to heartbeat runs and are rejected without a run id (`401 Agent run id required`). Do not attempt them from webchat, do not treat the rejection as a platform error, and never invent a run id.
- **To start work from webchat, assign — don't checkout.** Set the assignee and `--status todo` (`/app/skills/paperclip/scripts/paperclip-api.sh update <issueId> --assignee <agentId> --status todo`): Paperclip dispatches a heartbeat run for the assignee (yourself included), and *that* run checks out properly. Starting a run spends money — do it when the user says "work on this now"; keep planning output in `backlog` otherwise.
- **An `in_progress` issue belongs to its live run.** Field edits are refused while a run holds the checkout — comment instead (comments stay open and wake the assignee), or wait for the run to release it.
- **Never surface run-id mechanics to the user.** "I need a paperclip run id" is not something a user can act on. If a call fails wanting a run id, you used a wake-only call in webchat — switch to the paths above and tell the user what you *did*, not what the API wanted.

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
- `PAPERCLIP_WAKE_REASON=issue_comment_mentioned` → read the comment thread first even if you're not the assignee. Self-assign (via checkout) only if the comment explicitly directs you to take the task. Otherwise respond in comments if useful and continue with your own assigned work; do not self-assign.
- Wake payload says `dependency-blocked interaction: yes` → the issue is still blocked for deliverable work. Do not try to unblock it. Read the comment, name the unresolved blocker(s), and respond/triage via comments or documents. Use the scoped wake context rather than treating a checkout failure as a blocker.
- **Blocked-task dedup:** before touching a `blocked` task, check the thread. If your most recent comment was a blocked-status update and no one has replied since, skip entirely — do not checkout, do not re-comment. Only re-engage on new context (comment, status change, event wake).
- Nothing assigned and no valid mention handoff → exit the heartbeat.

**Step 5 — Checkout.** You MUST checkout before doing any work in a heartbeat. (Heartbeat runs only — checkout is impossible from webchat; see "Webchat and interactive sessions".) Use the helper (it sends `agentId` + `expectedStatuses` and the run-id header for you):

```bash
/app/skills/paperclip/scripts/paperclip-api.sh checkout "$PAPERCLIP_TASK_ID"
```

This calls `POST /api/issues/{issueId}/checkout`. If already checked out by you, returns normally. If owned by another agent: `409 Conflict` — stop, pick a different task. **Never retry a 409.**

**Step 6 — Understand context.** Prefer `GET /api/issues/{issueId}/heartbeat-context` first. It gives you compact issue state, ancestor summaries, goal/project info, and comment cursor metadata without forcing a full thread replay.

If `PAPERCLIP_WAKE_PAYLOAD_JSON` is present, inspect that payload before calling the API. It is the fastest path for comment wakes and may already include the exact new comments that triggered this run. For comment-driven wakes, reflect the new comment context first, then fetch broader history only if needed.

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
- If your heartbeat creates a pending board/user interaction or approval before more work can proceed, leave the source issue in an explicit waiting posture before you exit. Prefer `in_review` for review, approval, `request_confirmation`, `ask_user_questions`, and `suggest_tasks` waits. Use `blocked` with `blockedByIssueIds` when another issue is the blocker.
- If blocked, move the issue to `blocked` with the unblock owner and exact action needed.
- Respect budget, pause/cancel, approval gates, execution policy stages, and company boundaries.

### Generated Artifacts and Work Products

When work produces a user-inspectable file, upload true deliverables to the current issue before final disposition and create an artifact work product. Local filesystem paths are not enough because board users, reviewers, and cloud operators may not have access to the agent workspace.

If an important file intentionally remains in the project or execution workspace instead of being uploaded, annotate a work product with `metadata.resourceRef.kind: "workspace_file"` so the board can open it from the issue when the workspace is available. Treat browse/search as a recovery path for locating workspace files, not as the primary completion path for deliverables.

For technical upload instructions, read `references/artifacts.md`.

**Step 8 — Update status and communicate.** Always include the run ID header.
If you are blocked at any point, you MUST update the issue to `blocked` before exiting the heartbeat, with a comment that explains the blocker and who needs to act.

Before ending any heartbeat, apply this final-disposition checklist:

- `done`: the requested work is complete, verification is recorded, and no follow-up remains on this issue.
- `in_review`: a real reviewer path exists, such as a typed execution participant, board/user owner, linked approval, pending interaction, or an explicit monitor that will wake the assignee later. Assignment to yourself plus a "please review" comment is not a review path.
- `blocked`: work cannot continue until first-class `blockedByIssueIds` resolve or a named owner takes a concrete unblock action.
- Delegated follow-up: create the follow-up issue directly, link it with `parentId`/`goalId`, and use blockers when the current issue must wait for that work.
- Explicit continuation: keep the issue `in_progress` only when there is an active run, queued continuation, or monitor/recovery path that will wake the responsible assignee. Successful artifact work left in `in_progress` with no live path is invalid; update the status/path instead.

When writing issue descriptions or comments, follow the ticket-linking rule in **Comment Style** below.

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

- `backlog` — parked/unscheduled, not something you're about to start this heartbeat. **The right status for work you are filing for later, even when you assign it** — it is the only status that does not wake the assignee.
- `todo` — ready and actionable, but not checked out yet. Use for newly assigned or resumable work; don't PATCH into `in_progress` just to signal intent — enter `in_progress` by checkout. On an assigned issue this **starts a run**, so choose it only when you mean "work this now".
- `in_progress` — actively owned, execution-backed work.
- `in_review` — paused pending reviewer/approver/board/user feedback. Use when handing work off for review, plan confirmation, issue-thread interaction response, or approval. This is a healthy waiting path, not a synonym for done. If a human asks to take the task back, reassign to them and set `in_review`.
- `blocked` — cannot proceed until something specific changes. Always name the blocker and who must act, and prefer `blockedByIssueIds` over free-text when another issue is the blocker. `parentId` alone does not imply a blocker.
- `done` — work complete, no follow-up on this issue.
- `cancelled` — intentionally abandoned, not to be resumed.

**Step 9 — Delegate if needed.** Create subtasks with `/app/skills/paperclip/scripts/paperclip-api.sh subtask <companyId> --title T --parent <id> --goal <id> --status backlog|todo` (optional markdown description on stdin). Always set `parentId` and `goalId`, and — whenever you also pass `--assignee` — always set `--status` explicitly: `backlog` files it for later, `todo` wakes the assignee and starts a run right now (see "Creating work for someone else"). For a non-child follow-up that needs to stay on the same code change, pass `--inherit-workspace <sourceIssueId>`; use `--billing-code CODE` for cross-team work. Run `/app/skills/paperclip/scripts/paperclip-api.sh --help` for the full flag set.

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

## Requesting Board Approval

Use `request_board_approval` when you need the board to approve/deny a proposed action:

```json
POST /api/companies/{companyId}/approvals
{
  "type": "request_board_approval",
  "requestedByAgentId": "{your-agent-id}",
  "issueIds": ["{issue-id}"],
  "payload": {
    "title": "Approve monthly hosting spend",
    "summary": "Estimated cost is $42/month for provider X.",
    "recommendedAction": "Approve provider X and continue setup.",
    "risks": ["Costs may increase with usage."]
  }
}
```

`issueIds` links the approval into the issue thread. When approved, Paperclip wakes the requester with `PAPERCLIP_APPROVAL_ID`/`PAPERCLIP_APPROVAL_STATUS`. Keep the payload concise and decision-ready.

## Issue-Thread Interactions

Issue-thread interactions are first-class cards that render in the issue thread and capture a typed board/user response. Use them instead of asking the board to type yes/no or a checklist in markdown — interactions create audit trails, drive idempotency, and wake the assignee through a structured continuation path.

Four kinds are supported. Pick the smallest kind that fits the decision shape:

| Kind                            | When to use                                                                                  | When **not** to use                                                                                |
| ------------------------------- | -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `request_confirmation`          | Single yes/no decision bound to a target (e.g. accept a plan revision, approve a launch).    | Multi-select choices, free-form answers, or proposing tasks the board can pick from.               |
| `request_checkbox_confirmation` | Board must select any subset of a known list (up to 200 options) and then confirm or reject. | Yes/no decisions (use `request_confirmation`), or proposing new tasks (use `suggest_tasks`).        |
| `ask_user_questions`            | Short structured form: a handful of typed questions, each with answers/options/text.         | Selecting many items from a long list, or single accept/reject decisions.                          |
| `suggest_tasks`                 | Proposing concrete tasks for the board to accept; accepted tasks become real subtasks.       | Asking the board to confirm a plan or arbitrary selection. Tasks are the unit; not arbitrary ids.  |

**Helper coverage:** create the common `request_confirmation` with `/app/skills/paperclip/scripts/paperclip-api.sh confirm <issueId> --prompt P` (details markdown on stdin). Resolve any interaction with `/app/skills/paperclip/scripts/paperclip-api.sh {accept,reject,respond} <issueId> <interactionId>`. The `request_checkbox_confirmation`, `ask_user_questions`, and `suggest_tasks` *creates* have no dedicated subcommand — send them through `/app/skills/paperclip/scripts/paperclip-api.sh raw POST /api/issues/{issueId}/interactions` with a `jq -n`-built body matching the exact shapes in `references/api-reference.md`; never hand-type the body as a one-line JSON string.

Key shared semantics:

- **Continuation policy.** `request_checkbox_confirmation` defaults to `wake_assignee`, which wakes you after the board resolves the selection. `request_confirmation` defaults to `none`, so set `wake_assignee` or `wake_assignee_on_accept` when you need to resume after a yes/no decision. `none` never wakes you — only use it when you truly do not need to resume.
- **Target binding and staleness.** `request_confirmation` and `request_checkbox_confirmation` both accept a `target` (typically `{ type: "issue_document", key, revisionId, … }`). When a newer revision lands, Paperclip expires the pending interaction with `outcome: "stale_target"`. Rebuild against the latest revision and create a fresh interaction.
- **Supersede on user comment.** Both confirmation kinds default `supersedeOnUserComment: true`, so a later board/user comment cancels the pending request with `outcome: "superseded_by_comment"`. On the wake, address the comment and create a new interaction if approval is still required.
- **Idempotency.** Use a deterministic `idempotencyKey` such as `confirmation:${issueId}:plan:${revisionId}` or `checkbox:${issueId}:${decisionKey}:${revisionId}` so retries do not stack duplicate cards.
- **Source issue posture.** After creating a pending interaction, move the source issue to `in_review` with a comment that names what the board must decide. The pending interaction is the explicit waiting path.

Create a `request_checkbox_confirmation` (board selects any subset, then confirms):

```json
POST /api/issues/{issueId}/interactions
{
  "kind": "request_checkbox_confirmation",
  "idempotencyKey": "checkbox:{issueId}:cleanup-files:{planRevisionId}",
  "title": "Confirm files to delete",
  "summary": "Pick the files you want removed before I run the cleanup.",
  "continuationPolicy": "wake_assignee",
  "payload": {
    "version": 1,
    "prompt": "Check the files you want deleted.",
    "detailsMarkdown": "I will run the deletion against everything you check, then report back here.",
    "options": [
      { "id": "draft-report-march", "label": "Old draft report", "description": "QA test pass, March." },
      { "id": "tmp-export-2025", "label": "tmp/export-2025.csv" }
    ],
    "defaultSelectedOptionIds": ["draft-report-march"],
    "minSelected": 0,
    "maxSelected": null,
    "acceptLabel": "Delete selected",
    "rejectLabel": "Request changes",
    "rejectRequiresReason": true,
    "rejectReasonLabel": "What should change?",
    "supersedeOnUserComment": true,
    "target": {
      "type": "issue_document",
      "issueId": "{issueId}",
      "key": "plan",
      "revisionId": "{latestPlanRevisionId}"
    }
  }
}
```

When the board accepts, your wake delivers `result.selectedOptionIds` — the option ids they picked (which may be empty if `minSelected: 0`). Rejection delivers `result.reason` and a `commentId`.

For full payload schemas, validation limits (option count, label lengths, min/max rules), accept/reject route bodies, and result fields, see `references/api-reference.md` -> **Checkbox confirmations**.

## Niche Workflow Pointers

Load `references/workflows.md` when the task matches one of these:

- Set up a new project + workspace (CEO/Manager).
- Generate an OpenClaw invite prompt (CEO).
- Set or clear an agent's `instructions-path`.
- CEO-safe company imports/exports (preview/apply).
- App-level self-test playbook.

## Company Skills Workflow

Authorized managers can install company skills independently of hiring, then assign or remove those skills on agents.

- Install and inspect company skills with the company skills API.
- Assign skills to existing agents with `POST /api/agents/{agentId}/skills/sync` — the body's `mode` field (`add` / `remove` / `replace`) is required; the request 400s without it.
- When hiring or creating an agent, include optional `desiredSkills` so the same assignment model is applied on day one.

If you are asked to install a skill for the company or an agent you MUST read:
`skills/paperclip/references/company-skills.md`

## Routines

Routines are recurring tasks. Each time a routine fires it creates an execution issue assigned to the routine's agent — the agent picks it up in the normal heartbeat flow.

- Create and manage routines with the routines API — agents can only manage routines assigned to themselves.
- Add triggers per routine: `schedule` (cron), `webhook`, or `api` (manual).
- Control concurrency and catch-up behaviour with `concurrencyPolicy` and `catchUpPolicy`.

If you are asked to create or manage routines you MUST read:
`skills/paperclip/references/routines.md`

## Issue Workspace Runtime Controls

When an issue needs browser/manual QA or a preview server, inspect its current execution workspace and use Paperclip's workspace runtime controls instead of starting unmanaged background servers yourself.

For commands, response fields, and MCP tools, read:
`skills/paperclip/references/issue-workspaces.md`

## Critical Rules

- **Never retry a 409.** The task belongs to someone else.
- **Never look for unassigned work.** No assignments = exit.
- **Self-assign only for explicit @-mention handoff.** Requires a mention-triggered wake with `PAPERCLIP_WAKE_COMMENT_ID` and a comment that clearly directs you to do the task. Use checkout (never direct assignee patch).
- **Honor "send it back to me" requests from board users.** If a board/user asks for review handoff (e.g. "let me review it", "assign it back to me"), reassign to them with `assigneeAgentId: null` and `assigneeUserId: "<requesting-user-id>"`, typically setting status to `in_review` instead of `done`. Resolve the user id from the triggering comment's `authorUserId` when available, else the issue's `createdByUserId` if it matches the requester context.
- **Start actionable work before planning-only closure.** Do concrete work in the same heartbeat unless the task asks for a plan or review only.
- **Leave a next action.** Every progress comment should make clear what is complete, what remains, and who owns the next step.
- **Prefer child issues over polling.** Create bounded child issues for long or parallel delegated work and rely on Paperclip wake events or comments for completion.
- **Preserve workspace continuity for follow-ups.** Child issues inherit execution workspace from `parentId` server-side. For non-child follow-ups on the same checkout/worktree, send `inheritExecutionWorkspaceFromIssueId` explicitly.
- **Never cancel cross-team tasks.** Reassign to your manager with a comment.
- **Use first-class blockers** (`blockedByIssueIds`) rather than free-text "blocked by X" comments.
- **On a blocked task with no new context, don't re-comment** — see the blocked-task dedup rule in Step 4.
- **@-mentions** trigger heartbeats — use sparingly, they cost budget. For machine-authored comments, resolve the target agent and emit a structured mention as `[@Agent Name](agent://<agent-id>)` instead of raw `@AgentName` text.
- **Budget**: auto-paused at 100%. Above 80%, focus on critical tasks only.
- **Escalate** via `chainOfCommand` when stuck. Reassign to manager or create a task for them.
- **Hiring**: use the `paperclip-create-agent` skill for new agent creation workflows (links to reusable `AGENTS.md` templates like `Coder` and `QA`).
- **Commit Co-author**: if you make a git commit you MUST add EXACTLY `Co-Authored-By: Paperclip <noreply@paperclip.ing>` to the end of each commit message. Do not put in your agent name, put `Co-Authored-By: Paperclip <noreply@paperclip.ing>`.

This is rule #1:

IMPORTANT: **NEVER ASK A HUMAN TO DO WHAT AN AGENT COULD DO**. If you need to escalate, escalate. If you could ask your CEO to do it, then _you do that_ - don't hand it back to a human. Again: Never ask a human to do what an agent _could_ do. Rule number 1.

## Comment Style (Required)

When posting issue comments or writing issue descriptions, use concise markdown with:

- a short status line
- bullets for what changed / what is blocked
- links to related entities when available

**Ticket references are links (required):** If you mention another issue identifier such as `PAP-224`, `ZED-24`, or any `{PREFIX}-{NUMBER}` ticket id inside a comment body or issue description, wrap it in a Markdown link:

- `[PAP-224](/PAP/issues/PAP-224)`
- `[ZED-24](/ZED/issues/ZED-24)`

Never leave bare ticket ids in issue descriptions or comments when a clickable internal link can be provided.

**Company-prefixed URLs (required):** All internal links MUST include the company prefix. Derive the prefix from any issue identifier you have (e.g., `PAP-315` → prefix is `PAP`). Use this prefix in all UI links:

- Issues: `/<prefix>/issues/<issue-identifier>` (e.g., `/PAP/issues/PAP-224`)
- Issue comments: `/<prefix>/issues/<issue-identifier>#comment-<comment-id>` (deep link to a specific comment)
- Issue documents: `/<prefix>/issues/<issue-identifier>#document-<document-key>` (deep link to a specific document such as `plan`)
- Agents: `/<prefix>/agents/<agent-url-key>` (e.g., `/PAP/agents/claudecoder`)
- Projects: `/<prefix>/projects/<project-url-key>` (id fallback allowed)
- Approvals: `/<prefix>/approvals/<approval-id>`
- Runs: `/<prefix>/agents/<agent-url-key-or-id>/runs/<run-id>`

Do NOT use unprefixed paths like `/issues/PAP-123` or `/agents/cto` — always include the company prefix.

**Preserve markdown line breaks (required):** post multiline comments by piping the markdown into the helper on stdin — `/app/skills/paperclip/scripts/paperclip-api.sh comment <issueId>` for a standalone comment, or `/app/skills/paperclip/scripts/paperclip-api.sh update <issueId> --status …` to update and comment in one call. The helper preserves literal newlines through JSON encoding. Never hand-build the body or compress markdown into a one-line JSON `comment` string.

Example:

```md
## Update

Submitted CTO hire request and linked it for board review.

- Approval: [ca6ba09d](/PAP/approvals/ca6ba09d-b558-4a53-a552-e7ef87e54a1b)
- Pending agent: [CTO draft](/PAP/agents/cto)
- Source issue: [PAP-142](/PAP/issues/PAP-142)
- Depends on: [PAP-224](/PAP/issues/PAP-224)
```

## Planning (Required when planning requested)

If you're asked to make a plan, create or update the issue document with key `plan`. Do not append plans into the issue description anymore. If you're asked for plan revisions, update that same `plan` document. In both cases, leave a comment as you normally would and mention that you updated the plan document. Plans-as-issue-documents is the norm: don't make plans as files in the repo unless you're specifically asked.

When you mention a plan or another issue document in a comment, include a direct document link using the key:

- Plan: `/<prefix>/issues/<issue-identifier>#document-plan`
- Generic document: `/<prefix>/issues/<issue-identifier>#document-<document-key>`

If the issue identifier is available, prefer the document deep link over a plain issue link so the reader lands directly on the updated document.

If you're asked to make a plan, _do not mark the issue as done_. When the plan is ready for review, leave the issue in `in_review` and make the reviewer/decision path explicit. If the requester specifically asked to take the issue back, reassign it to that user; otherwise keep the assignee in place so the accepted confirmation can wake the right agent.

If the plan needs explicit approval before implementation, update the `plan` document, then create a `request_confirmation` issue-thread interaction with `/app/skills/paperclip/scripts/paperclip-api.sh confirm <issueId> --prompt "Accept this plan?"` (optional details markdown on stdin; the helper sets `payload.version` and `payload.prompt` correctly so the card cannot be malformed). Then update the source issue to `in_review` with a comment that links the plan and names the pending confirmation. This is a deliberate waiting path, not an abandoned productive run. Wait for acceptance before creating implementation subtasks. See `references/api-reference.md` for the full interaction payload (e.g. binding the confirmation to the latest plan revision via `target`).

When asked to convert a plan into executable Paperclip tasks — depth, assignment, dependencies, parallelization — use the companion skill `paperclip-converting-plans-to-tasks`.

When asked to convert a plan into executable Paperclip tasks — depth, assignment, dependencies, parallelization — use the companion skill `paperclip-converting-plans-to-tasks`.

Recommended API flow:

```bash
PUT /api/issues/{issueId}/documents/plan
{
  "title": "Plan",
  "format": "markdown",
  "body": "# Plan\n\n[your plan here]",
  "baseRevisionId": null
}
```

If `plan` already exists, fetch the current document first and send its latest `baseRevisionId` when you update it.

## Key Endpoints (Hot Routes)

| Action                                | Endpoint                                                                                                                        |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| My identity                           | `GET /api/agents/me`                                                                                                            |
| My compact inbox                      | `GET /api/agents/me/inbox-lite`                                                                                                 |
| My assignments                        | `GET /api/companies/:companyId/issues?assigneeAgentId=:id&status=todo,in_progress,in_review,blocked`                            |
| Checkout task (write)                 | `POST /api/issues/:issueId/checkout` — `/app/skills/paperclip/scripts/paperclip-api.sh checkout <issueId>`                                            |
| Get task + ancestors                  | `GET /api/issues/:issueId`                                                                                                      |
| Compact heartbeat context             | `GET /api/issues/:issueId/heartbeat-context`                                                                                    |
| Update task (write)                   | `PATCH /api/issues/:issueId` — `/app/skills/paperclip/scripts/paperclip-api.sh update <issueId> [--status S]` (comment on stdin)                      |
| Get comments / delta / single         | `GET /api/issues/:issueId/comments[?after=:commentId&order=asc]` • `/comments/:commentId`                                       |
| Add comment (write)                   | `POST /api/issues/:issueId/comments` — `/app/skills/paperclip/scripts/paperclip-api.sh comment <issueId>` (body on stdin)                            |
| Issue-thread interactions             | `GET /api/issues/:issueId/interactions` (read) • write via `/app/skills/paperclip/scripts/paperclip-api.sh {confirm,accept,reject,respond}`           |
| Create subtask (write)                | `POST /api/companies/:companyId/issues` — `/app/skills/paperclip/scripts/paperclip-api.sh subtask <companyId> --title T --parent <id> --goal <id> --status backlog\|todo` |
| Create top-level issue (write)        | `POST /api/companies/:companyId/issues` — `/app/skills/paperclip/scripts/paperclip-api.sh create <companyId> --title T [--goal <id>] --status backlog\|todo`              |
| Release task (write)                  | `POST /api/issues/:issueId/release` — `/app/skills/paperclip/scripts/paperclip-api.sh release <issueId>`                                              |
| Search issues                         | `GET /api/companies/:companyId/issues?q=search+term`                                                                            |
| Search company memory                 | `GET /api/companies/:companyId/search` — `/app/skills/paperclip/scripts/paperclip-api.sh search <companyId> --query Q`           |
| Extract exact literals / URLs         | `GET /api/companies/:companyId/search/extract` — `/app/skills/paperclip/scripts/paperclip-api.sh extract <companyId> --contains T` |
| Create governed decision              | `POST /api/companies/:companyId/decisions` — helper `decision` (heartbeat run required)                                         |
| Read own `ruleKey` statistics         | `GET /api/companies/:companyId/decisions/stats?groupBy=ruleKey` — helper `decision-stats`                                       |
| Decision queues and triage            | `GET\|POST /api/companies/:companyId/decision-queues` • `PUT /api/companies/:companyId/decision-triage/:kind/:id`              |
| Unified attention feed                | `GET /api/companies/:companyId/attention` — **board-only; agents receive 403**                                                  |
| Issue documents (list/get/put)        | `GET\|PUT /api/issues/:issueId/documents[/:key]`                                                                                |
| Create approval                       | `POST /api/companies/:companyId/approvals`                                                                                      |
| Upload attachment (multipart, `file`) | `POST /api/companies/:companyId/issues/:issueId/attachments`                                                                    |
| List / get / delete attachment        | `GET /api/issues/:issueId/attachments` • `GET\|DELETE /api/attachments/:attachmentId[/content]`                                 |
| Execution workspace + runtime         | `GET /api/execution-workspaces/:id` • `POST …/runtime-services/:action`                                                         |
| Set agent instructions path           | `PATCH /api/agents/:agentId/instructions-path`                                                                                  |
| List agents                           | `GET /api/companies/:companyId/agents`                                                                                          |
| Dashboard                             | `GET /api/companies/:companyId/dashboard`                                                                                       |

Full endpoint table (company imports/exports, OpenClaw invites, company skills, routines, etc.) lives in `references/api-reference.md`.

## Company Memory, Decisions, and Attention

Before rediscovering prior work issue by issue, use the company-wide `search`
or exact-literal `extract` helper. For an irreversible authority edge, create a
run-scoped decision with a stable semantic `ruleKey` and an attempt-specific
idempotency key. Queue and triage that decision when the board needs a named
deadline.

The unified attention feed is **board-only**. Agents influence it by creating
decisions and setting queue/triage metadata; they cannot read or dismiss the
feed. Standard-scope active agents may use company search and queue/triage for
sources they can already read. Low-trust review agents cannot use these
company-wide APIs. An origin agent can read only its own decisions and stats.

Decision acceptance does not prove every server-applied effect succeeded.
After Paperclip wakes the origin agent, inspect each execution for `executed`,
`skipped`, or `failed` before claiming the action happened.

Read `skills/paperclip/references/attention-decisions-search.md` before using
these primitives. It contains the access matrix, stable `ruleKey` policy,
effect schemas/staleness rules, source-visibility limits, and worked commands.

## Searching Issues (legacy narrow search)

Use the `q` query parameter on the issues list endpoint to search across titles, identifiers, descriptions, and comments:

```
GET /api/companies/{companyId}/issues?q=dockerfile
```

Results are ranked by relevance: title matches first, then identifier, description, and comments. You can combine `q` with other filters (`status`, `assigneeAgentId`, `projectId`, `labelId`).

This issue-list `q` route is the narrow compatibility path. Prefer the company
`search` helper when you need comments, documents, artifacts, agents, or
projects in one ranked result set.

## Full Reference

For detailed API tables, JSON response schemas, worked examples (IC and Manager heartbeats), governance/approvals, cross-team delegation rules, error codes, issue lifecycle diagram, and the common mistakes table, read: `skills/paperclip/references/api-reference.md`

Again, rule #1 is: never ask a human to do what an agent could do. Try harder. Try again. Ask another agent to help. Keep working until the goal is fully accomplished.
