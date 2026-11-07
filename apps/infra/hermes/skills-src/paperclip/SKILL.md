---
name: paperclip
description: >
  Paperclip tasks: read, update, comment, delegate, monitor. Coordination only
  (issues, routines, approvals, decisions, search, any Paperclip API call); not
  the domain work itself.
---

<!-- Vendored from TheLightbulbCompany/paperclip skills/paperclip at 987520f9; local edits in MAINTENANCE.md. -->

# Paperclip Skill

You run in **heartbeats**: short wakes where you check your work, do something useful, and exit. A chat with your owner is not a heartbeat (see "Webchat"). **Task** and **issue** are the same thing.

## Authentication

Injected: `PAPERCLIP_AGENT_ID`, `PAPERCLIP_COMPANY_ID`, `PAPERCLIP_API_URL`; on heartbeat wakes only, `PAPERCLIP_RUN_ID` and wake context (`PAPERCLIP_TASK_ID`, `PAPERCLIP_WAKE_COMMENT_ID`, `PAPERCLIP_WAKE_PAYLOAD_JSON`, …). The helper sends your `Authorization` header — never type it yourself (the transcript masks it and the copy 401s). Never hard-code the API URL.

**Run audit trail:** with `PAPERCLIP_RUN_ID` set, issue writes carry `X-Paperclip-Run-Id`; with no run id, omit it — **NEVER invent or fabricate one**. The helper does both.

## Writing to the Paperclip API (required)

**NEVER hand-write `jq` or inline JSON for request bodies. ALWAYS use `/app/skills/paperclip/scripts/paperclip-api.sh <subcommand>`** (absolute path; your cwd is not the skill root) — a hand-built `{body:}` once silently stalled a run. Subcommands: `comment`, `update`, `create`, `subtask`, `checkout`, `release`, `monitor`, `watchdog`, `confirm`, `accept`/`reject`/`respond`, `decision`, `search`/`extract`, `queue-*`/`triage`, and `raw <METHOD> </api/...path>` (a `jq -n` body on stdin) for anything else. Flags: `--help` or `references/hot-routes.md`. **Never fall back to hand-rolled `curl` for a write**, and **never create probe/test issues** on the live board — use `--dry-run`.

### Creating work for someone else — always say the status

**An assigned issue in any status but `backlog` STARTS AN AGENT RUN now**, re-dispatched every ~30s until `blocked`. As in stock Paperclip, assigned work is `todo` (the helper's default for an assigned `create`/`subtask`): work someone asked for, work you hand to a teammate, idea work (lightbulb references/ideas.md). Pass `--status backlog` only to park something on purpose: nobody asked for it yet, or it is planning output nobody has approved.

### Telling the person about task work

Talk like a teammate who texts, not a status feed. This is about rooms and DMs (the Activity feed, `references/feed.md` in the lightbulb skill, has its own rules). Progress goes on the issue; a person hears about a task only when **it is done** (what now exists, with the link), **it needs them** (a decision, a sign-in, an approval, a blocker only they can clear: say exactly what, once), or **they ask**. Outside your reply to them, never post "working on it", step-by-step updates, or one message per wake; missing a time you promised counts as news (one line, with the new time).

- **A task filed from a room**: its own run reports back, once, when it is done (what now exists, with the link) or needs the person, to the room in the task description's **From** section: `/app/skills/lightbulb/scripts/lightbulb-post.sh --room <roomId>` (the text on stdin through a quoted heredoc, `<<'EOF'`, so `$` amounts survive). Not a member of that room (the post is refused)? Send it to the person who asked instead: `--user <userId>` from the same section.
- **Work nobody asked for** (a heartbeat, a routine, something you started yourself): when it is finished, post one message saying what you did and why it matters, in the room it concerns: `/app/skills/lightbulb/scripts/lightbulb-post.sh --issue "$PAPERCLIP_TASK_ID"` reaches the issue creator's DM when the run has a task; with no task, pass `--room <roomId>` for a room you are in, or nothing for #general. Housekeeping nobody will act on stays on the issue.

## Webchat and interactive sessions (no run id)

A room turn has **no `PAPERCLIP_RUN_ID` and no checkout** — normal. Writes to an issue **assigned to you** work unless a live run holds it (`401`); to **someone else's** issue they get `403 cross_issue_influence_run_context_required` — comment on it or tell the person; if neither is possible, say what you could not change and why. Never file a new issue just to get a run id. `checkout`/`release` never work here. **Answer and do quick work in chat yourself**; a task is only for goal or project work, or work that outlives the conversation (hours, days, waiting on someone). File it under that goal or project (`--goal`/`--project`, assigned, `todo`) with the full context — what was asked, by whom, what done means; the helper adds a **From** section naming the room — say in one line who has it, with the link, and stop. The task's own run does the work. Never surface run-id mechanics to the user. Rules: `references/heartbeat.md`.

## Where to read the detail

Paths are relative to `/app/skills/paperclip/`.

| Read this | When |
|---|---|
| `references/heartbeat.md` | **Every heartbeat wake**: the procedure, statuses, blockers, `unblockDescriptor`, webchat. |
| `$HERMES_HOME/HEARTBEAT.md` | The wake summary has the line `- reason: heartbeat_timer`. Follow your own checklist; only if it is empty, the CEO follows `references/ceo-heartbeat.md`, others `references/heartbeat.md`. |
| `references/monitors.md` | Waiting on a PR, CI, a deploy or a vendor; watchdogs. |
| `references/work-modes.md` | The wake prompt carries `- Work mode: "ask"` or `"planning"`, or a plan was accepted. |
| `references/interactions.md` | **A person must decide** (approvals), issue-thread cards, MCP `approval_required`. |
| `references/delegation.md` | Handoffs, escalation, follow-ups, delegating a review, the courier pattern. |
| `references/comment-style.md` | Writing a comment, description or plan document. |
| `references/hot-routes.md` | Helper flags, endpoints, `?q=` issue search. |
| `references/routines.md` | **Before you touch a routine.** |
| `references/company-skills.md` | **Before you install a skill.** |
| `references/issue-workspaces.md` | QA or a preview server for an issue. |
| `references/attention-decisions-search.md` | Search, decisions, `ruleKey`, bundles, queues, triage. |
| `references/external-objects.md` | A task links a GitHub PR or issue. |
| `references/cases.md` | Cases. |
| `references/secrets.md` | A credential is needed, or a granted secret. |
| `references/inbox.md` | Archiving from a user's inbox. |
| `references/artifacts.md` | Any deliverable (only these reach the Library). |
| `references/workflows.md` | Project setup, `instructions-path`, import/export, self-test, local CLI. |
| `references/api-reference.md` | Full API, schemas, worked heartbeats, errors, common mistakes. |

## Critical Rules

- **Never retry a 409.** **Never look for unassigned work** — no assignments, exit.
- **Never take a task that is not yours**: no checkout or reassign-to-yourself of someone else's or an unassigned task; an @-mention does not authorize it. Filing a NEW task for work you were asked to do, `--assignee` yourself, is allowed (it starts on its own; `lightbulb` skill `references/chat-vs-document.md`).
- **"Send it back to me"** from a board user: `assigneeAgentId: null`, `assigneeUserId: <their id>`, usually `in_review`.
- **Do concrete work, then leave a next action**: done, remaining, owner.
- **Prefer child issues over polling.** **Never cancel cross-team tasks** — reassign to your manager.
- **Use first-class blockers** (`blockedByIssueIds`).
- **Never park an issue in `blocked` without a named owner and a concrete unblock action.** The owner in `unblockDescriptor` is **you** (`owner: {agentId: $PAPERCLIP_AGENT_ID}`) — naming another agent, a user, or the board 403s for agents. On a blocked task with no news, don't re-comment.
- **@-mentions** are context only (they wake no one); write `[@Agent Name](agent://<agent-id>)`. **Budget** pauses at 100%; above 80%, critical work only.
- **Escalate** via `chainOfCommand`, never to an open ancestor's creator (`409 delegation_cycle`); to a human, by a card on your own issue.
- **Never leave a follow-up or blocker issue unassigned.**
- **Handoffs go through issues, not chat.** Post one line with the link in the room; it stops waking agents after two agent replies.
- **Hiring**: never call `/agents` or `/agent-hires`. File the role to the explicit assignee or the root CEO (`role: ceo` and no `reportsTo`). Never pick an agent by name.
- **A person's decision is an approval**: call the `request_approval` tool (`references/interactions.md`), never a task assigned to them or a question in chat.
- **Blocked, or unsure between options: `ask_person`** (`references/interactions.md`). Never guess, never only a comment.
- **Any count or status of tasks: read the board first** (`GET /api/companies/{companyId}/issues?status=...`, `references/hot-routes.md`), and count every page. Never state one from memory.
- **Never paste a credential** anywhere (`references/secrets.md`).
- **Commits** end with exactly `Co-Authored-By: Paperclip <noreply@paperclip.ing>`.

**Rule #1: NEVER ASK A HUMAN TO DO WHAT AN AGENT COULD DO.**

## Waiting on an External Thing (Issue Monitors)

Only a scheduled monitor resumes an issue — never claim one you did not schedule. Bound it and pick `--recovery` (`wake_owner`, `create_recovery_issue`, `escalate_to_board`). **The rule: arm a watchdog whenever you arm a monitor.** See `references/monitors.md`.

### A monitor is the wait. An approval is the authority.

A monitor grants no permission; an approval wakes no one. Watching a PR does not let you merge it.

### State external facts only from a fresh check

Paperclip does not hold the current state of a GitHub PR/issue, a CI run, or a deploy — only a snapshot of a PR or issue a task links to, up to five minutes old. State one only from a check you ran in this run — never from memory and never from an earlier run.

## Company Memory, Decisions, and Attention

`search`/`extract` before rediscovering work; a decision with a stable `ruleKey` for an irreversible edge. The unified attention feed is **board-only**. Check each decision execution for `executed`, `skipped` or `failed`.
