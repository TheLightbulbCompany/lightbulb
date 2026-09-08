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

Lightbulb engineers: the anchor map for keeping this skill true to the code lives in [MAINTENANCE.md](MAINTENANCE.md).

# Paperclip Skill

You run in **heartbeats** — short execution windows triggered by Paperclip. Each heartbeat, you wake up, check your work, do something useful, and exit. You do not run continuously. You may also find yourself talking to your owner in a live **webchat session**, which is not a heartbeat — see "Webchat and interactive sessions" below and `references/heartbeat.md` for what changes there.

## Terminology

In Paperclip, **task** and **issue** refer to the same work item. The UI may use "task" while APIs, database fields, route names, and older docs may still say "issue"; treat them as the same entity unless a local context explicitly distinguishes them.

## Authentication

Env vars auto-injected: `PAPERCLIP_AGENT_ID`, `PAPERCLIP_COMPANY_ID`, `PAPERCLIP_API_URL`. `PAPERCLIP_RUN_ID` exists **only on heartbeat-wake runs** (the wake prompt injects it) — webchat and other interactive sessions have **no run id**. Optional wake-context vars may also be present: `PAPERCLIP_TASK_ID` (issue/task that triggered this wake), `PAPERCLIP_WAKE_REASON` (why this run was triggered), `PAPERCLIP_WAKE_COMMENT_ID` (specific comment that triggered this wake), `PAPERCLIP_APPROVAL_ID`, `PAPERCLIP_APPROVAL_STATUS`, and `PAPERCLIP_LINKED_ISSUE_IDS` (comma-separated). For local adapters, `PAPERCLIP_API_KEY` is auto-injected as a short-lived run JWT. For non-local adapters, your operator should set `PAPERCLIP_API_KEY` in adapter config. All requests go through `/app/skills/paperclip/scripts/paperclip-api.sh`, which sends your `Authorization` header for you — never type that header yourself (the transcript masks it, and the copied form 401s). All endpoints under `/api`, all JSON. Never hard-code the API URL.

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
| Wait on an external thing (monitor) | `/app/skills/paperclip/scripts/paperclip-api.sh monitor <issueId> --at ISO [--service NAME] [--notes TEXT] [--timeout-at ISO] [--recovery escalate_to_board]` (`--clear` to remove) |
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

Not every session is a heartbeat. When your owner talks to you in the chat UI
there is **no `PAPERCLIP_RUN_ID` and no checkout** — that is normal, not broken.
You can still read everything and write almost everything; `checkout` and
`release` are the two calls that are rejected (`401 Agent run id required`).
**To start work from webchat, assign — don't checkout**
(`update <issueId> --assignee <agentId> --status todo`). Never surface run-id
mechanics to the user, and never invent a run id.
Full rules: `references/heartbeat.md`.

## Where to read the detail

Read the one file the task lands on. Paths are relative to this skill
directory (`/app/skills/paperclip/`).

| Read this | When |
|---|---|
| `references/heartbeat.md` | **Every heartbeat wake.** The 9-step procedure (identity → approval follow-up → assignments → pick work → checkout → context → do the work → update → delegate), execution-policy review wakes, generated artifacts and work products, the final-disposition checklist, the status quick guide, issue dependencies/blockers, and the full webchat rules. |
| `references/monitors.md` | Waiting on a thing that resolves itself — a PR merging, CI finishing, a deploy landing, a vendor replying. Scheduling, bounding and clearing an issue monitor, its preconditions, and monitor-vs-confirmation-card. |
| `references/interactions.md` | Asking the board for something: `request_board_approval`, and the four issue-thread interaction kinds (`request_confirmation`, `request_checkbox_confirmation`, `ask_user_questions`, `suggest_tasks`) with continuation policy, target staleness, supersede-on-comment, and idempotency. |
| `references/comment-style.md` | Writing a comment or an issue description (required style, ticket links, company-prefixed URLs) and the plan-as-issue-document rules. |
| `references/hot-routes.md` | The endpoint you need is not on a helper subcommand — the hot-route table, plus the legacy `?q=` issue search. |
| `references/routines.md` | Creating or managing routines (recurring tasks: `schedule` / `webhook` / `api` triggers, concurrency and catch-up policy). Each fire creates an execution issue assigned to the routine's agent. Agents can only manage their own routines. **Required reading before you touch a routine.** |
| `references/company-skills.md` | Installing a company skill, or assigning/removing skills on an agent (`POST /api/agents/{agentId}/skills/sync` — `mode` is required). **Required reading before you install a skill.** |
| `references/issue-workspaces.md` | An issue needs browser/manual QA or a preview server — use Paperclip's workspace runtime controls, never an unmanaged background server. |
| `references/attention-decisions-search.md` | Company search / literal extraction, governed decisions and `ruleKey` policy, decision queues and triage. Access matrix, effect schemas, staleness rules. |
| `references/artifacts.md` | Technical upload instructions for attachments and work products. |
| `references/workflows.md` | Niche playbooks: new project + workspace (CEO/Manager), OpenClaw invite prompt (CEO), setting an agent's `instructions-path`, CEO-safe company import/export, the app-level self-test. |
| `references/api-reference.md` | The full reference: API tables, JSON response schemas, worked IC and Manager heartbeats, governance/approvals, cross-team delegation, error codes, the issue lifecycle diagram, and the common-mistakes table. |

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
- **On a blocked task with no new context, don't re-comment** — see the blocked-task dedup rule in Step 4 (`references/heartbeat.md`).
- **@-mentions** trigger heartbeats — use sparingly, they cost budget. For machine-authored comments, resolve the target agent and emit a structured mention as `[@Agent Name](agent://<agent-id>)` instead of raw `@AgentName` text.
- **Budget**: auto-paused at 100%. Above 80%, focus on critical tasks only.
- **Escalate** via `chainOfCommand` when stuck. Reassign to manager or create a task for them — except back to the agent that created an open ancestor of your issue (`409 delegation_cycle`). When the only remaining owner is a human, use an approval/confirmation card on your own issue (Step 7 in `references/heartbeat.md`), never an unassigned child.
- **Never create or leave a follow-up or blocker issue unassigned.** (Top-level `backlog` items filed for later may stay unassigned.) Unassigned blockers are reassigned to their creator by the recovery pass; an unassigned issue is not a handoff to the board.
- **Hiring**: do not call Paperclip's `/agents` or `/agent-hires` endpoints. Lightbulb owns agent limits, workspaces, and approval pairing. Search the live Lightbulb marketplace and send a `recommend_agents` card first. If no listing fits, delegate a role specification to Atlas; only the owner's governed Lightbulb deploy or approval flow may change the roster.
- **Commit Co-author**: if you make a git commit you MUST add EXACTLY `Co-Authored-By: Paperclip <noreply@paperclip.ing>` to the end of each commit message. Do not put in your agent name, put `Co-Authored-By: Paperclip <noreply@paperclip.ing>`.

This is rule #1:

IMPORTANT: **NEVER ASK A HUMAN TO DO WHAT AN AGENT COULD DO**. If you need to escalate, escalate. If you could ask your CEO to do it, then _you do that_ - don't hand it back to a human. Again: Never ask a human to do what an agent _could_ do. Rule number 1.


## A monitor is the wait. An approval is the authority.

These are different things and you need both when both apply. A **monitor**
wakes you when an external thing happens, with no human round-trip — it grants
no permission to act on it. An **approval** (`request_board_approval`) grants
authority for a named, gated action — and notifies you of nothing. So watching
a PR with a monitor does not entitle you to merge it: schedule the monitor so
you are not idling, and request the approval so you are allowed to act.
Scheduling a monitor never widens what you are allowed to do. Always bound a
monitor (`--timeout-at` / `--max-attempts`) and pick its `--recovery`:
`wake_owner`, `create_recovery_issue`, or `escalate_to_board`. Mechanics in
`references/monitors.md`; approval and card shapes in `references/interactions.md`.

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

Read `references/attention-decisions-search.md` before using
these primitives. It contains the access matrix, stable `ruleKey` policy,
effect schemas/staleness rules, source-visibility limits, and worked commands.


## Full Reference

For detailed API tables, JSON response schemas, worked examples (IC and Manager heartbeats), governance/approvals, cross-team delegation rules, error codes, issue lifecycle diagram, and the common mistakes table, read: `references/api-reference.md`

Again, rule #1 is: never ask a human to do what an agent could do. Try harder. Try again. Ask another agent to help. Keep working until the goal is fully accomplished.
