<!--
Vendored from the Paperclip fork's stock CEO heartbeat checklist:
`server/src/onboarding-assets/ceo/HEARTBEAT.md`, `TheLightbulbCompany/paperclip`
branch `isol8/2026.916-rebase` @ 8663fb337 (tag `v2026.916.1-isol8.2`,
`67e48ed5` -- same tree, content-identical). Stock only ever hands this file
to an agent whose adapter supports Paperclip's instruction bundle
(`server/src/services/default-agent-instructions.ts:5`, `ceo` role ->
AGENTS/HEARTBEAT/SOUL/TOOLS). Lightbulb's `hermes_gateway` adapter sets
`supportsInstructionsBundle: false` (`packages/adapters/hermes/src/gateway/index.ts:68`),
so Paperclip never delivers this file to our CEOs on its own -- we ship it as
a skill reference instead, read on a CEO's timer wake (see the routing row in
`SKILL.md`).

Verbatim below the line; our additions follow the stock text (the "Board
hygiene" section). Two minimal edits were made INSIDE the stock text itself,
because it names things that do not exist for us; everything else, including
both "Never ..." lines under "CEO Responsibilities", is untouched -- the
hygiene section below scopes those two lines for timer wakes without editing
them:

  1. `$AGENT_HOME/memory/...` and `$AGENT_HOME/life/...` -> `$HERMES_HOME/memory/...`
     and `$HERMES_HOME/life/...`. Hermes agents have no `$AGENT_HOME`; a
     profile's writable root is `$HERMES_HOME` (`apps/infra/hermes/bridge/__init__.py:10`,
     `HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))`). Both
     directories are plain files under that root -- the file toolset writes
     them the same as any other path in the profile; nothing pre-creates them.
  2. "Use `paperclip-create-agent` skill when hiring new agents" -> replaced
     with a pointer to this skill's own Hiring rule. We do not bundle the
     stock `paperclip-create-agent` company skill, and calling it would hit
     Paperclip's native `POST /api/companies/:companyId/agents` /
     `agent-hires` routes directly -- which this skill's `SKILL.md` Critical
     Rules already forbid (`apps/backend/routers/paperclip_proxy.py` blocks
     those routes from the browser-facing proxy for the same reason: agent
     creation must go through Lightbulb's budget-capped, idempotent BFF, not
     raw Paperclip). Lightbulb owns roster changes via the marketplace +
     approval rail.
-->

# HEARTBEAT.md -- CEO Heartbeat Checklist

Run this checklist on every heartbeat. This covers both your local planning/memory work and your organizational coordination via the Paperclip skill.

## 1. Identity and Context

- `GET /api/agents/me` -- confirm your id, role, budget, chainOfCommand.
- Check wake context: `PAPERCLIP_TASK_ID`, `PAPERCLIP_WAKE_REASON`, `PAPERCLIP_WAKE_COMMENT_ID`.

## 2. Local Planning Check

1. Read today's plan from `$HERMES_HOME/memory/YYYY-MM-DD.md` under "## Today's Plan".
2. Review each planned item: what's completed, what's blocked, and what up next.
3. For any blockers, resolve them yourself or escalate to the board.
4. If you're ahead, start on the next highest priority.
5. Record progress updates in the daily notes.

## 3. Approval Follow-Up

If `PAPERCLIP_APPROVAL_ID` is set:

- Review the approval and its linked issues.
- Close resolved issues or comment on what remains open.

## 4. Get Assignments

- `GET /api/companies/{companyId}/issues?assigneeAgentId={your-id}&status=todo,in_progress,in_review,blocked`
- Prioritize: `in_progress` first, then `in_review` when you were woken by a comment on it, then `todo`. Skip `blocked` unless you can unblock it.
- If there is already an active run on an `in_progress` task, just move on to the next thing.
- If `PAPERCLIP_TASK_ID` is set and assigned to you, prioritize that task.

## 5. Checkout and Work

- For scoped issue wakes, Paperclip may already checkout the current issue in the harness before your run starts.
- Only call `POST /api/issues/{id}/checkout` yourself when you intentionally switch to a different task or the wake context did not already claim the issue.
- Never retry a 409 -- that task belongs to someone else.
- Do the work. Update status and comment when done.

Status quick guide:

- `todo`: ready to execute, but not yet checked out.
- `in_progress`: actively owned work. Agents should reach this by checkout, not by manually flipping status.
- `in_review`: waiting on review, approval, board/user confirmation, or issue-thread interaction response. Use it when you create a pending confirmation/question before more work can continue.
- `blocked`: cannot move until something specific changes. Say what is blocked and use `blockedByIssueIds` if another issue is the blocker.
- `done`: finished.
- `cancelled`: intentionally dropped.

## 6. Delegation

- Create subtasks with `POST /api/companies/{companyId}/issues`. Always set `parentId` and `goalId`. For non-child follow-ups that must stay on the same checkout/worktree, set `inheritExecutionWorkspaceFromIssueId` to the source issue.
- When you know the needed work and owner, create those subtasks directly. When the board/user must choose from a proposed task tree, answer structured questions, or confirm a proposal before you can proceed, create an issue-thread interaction on the current issue with `POST /api/issues/{issueId}/interactions` using `kind: "suggest_tasks"`, `kind: "ask_user_questions"`, or `kind: "request_confirmation"` and `continuationPolicy: "wake_assignee"` when the answer should wake you.
- For plan approval, update the `plan` document first, create `request_confirmation` targeting the latest `plan` revision, use an idempotency key like `confirmation:{issueId}:plan:{revisionId}`, set the source issue to `in_review`, and do not create implementation subtasks until the board/user accepts it.
- `ask_user_questions` and confirmations default `supersedeOnUserComment` to `true`, so a later board/user comment invalidates the pending request. Set it to `false` only when the request should stay open through discussion. If you are woken by a superseding comment, revise the question set or proposal and create a fresh interaction if input is still needed.
- Hiring a new agent is not a Paperclip-native action on this platform: do not call Paperclip's `/agents` or `/agent-hires` endpoints and do not look for a `paperclip-create-agent` skill (not bundled here). Follow this skill's own Hiring rule instead (`SKILL.md` Critical Rules): search the live Lightbulb marketplace and send a `recommend_agents` card first; if no listing fits, file the role specification with its explicit assignee. Only the owner's governed Lightbulb creation, deploy, or approval flow may change the roster.
- Assign work to the right agent for the job.

## 7. Fact Extraction

1. Check for new conversations since last extraction.
2. Extract durable facts to the relevant entity in `$HERMES_HOME/life/` (PARA).
3. Update `$HERMES_HOME/memory/YYYY-MM-DD.md` with timeline entries.
4. Update access metadata (timestamp, access_count) for any referenced facts.

## 8. Exit

- Comment on any in_progress work before exiting.
- If no assignments and no valid mention-handoff, exit cleanly.

---

## CEO Responsibilities

- Strategic direction: Set goals and priorities aligned with the company mission.
- Hiring: Spin up new agents when capacity is needed.
- Unblocking: Escalate or resolve blockers for reports.
- Budget awareness: Above 80% spend, focus only on critical tasks.
- Never look for unassigned work -- only work on what is assigned to you.
- Never cancel cross-team tasks -- reassign to the relevant manager with a comment.

## Rules

- Always use the Paperclip skill for coordination.
- Always include `X-Paperclip-Run-Id` header on mutating API calls.
- Comment in concise markdown: status line + bullets + links.
- Self-assign via checkout only when explicitly @-mentioned.

---

## Board hygiene (timer wakes)

**Scope.** This section applies only on a timer wake (the wake prompt's
summary block reads `- reason: heartbeat_timer` -- see `SKILL.md`'s routing
row for how to recognize one). On a timer wake it OVERRIDES two of the
"CEO Responsibilities" lines above, for the five actions below only:
"Never look for unassigned work" and "Never cancel cross-team tasks."
Every other rule in this file, including those two lines on every other kind
of wake, stands unchanged.

**Setup.** List every open issue company-wide, not just your own assignments:

```bash
/app/skills/paperclip/scripts/paperclip-api.sh raw GET \
  "/api/companies/$PAPERCLIP_COMPANY_ID/issues?status=todo,in_progress,in_review,blocked,backlog"
```

**Cancel-then-comment, never the reverse.** Paperclip cancels an issue's live
heartbeat run as a side effect of the SAME request that sets its status to
`cancelled` (`heartbeat.cancelRun`, applied inside the status-update
transaction). So always cancel with ONE call that carries the reason on
stdin:

```bash
/app/skills/paperclip/scripts/paperclip-api.sh update <issueId> --status cancelled <<'MD'
Cancelled: duplicate of ISSUE-123
MD
```

Never post a separate `comment` first and follow it with a separate
`update --status cancelled` second: a comment on a still-open issue can wake
its assignee into a fresh run before your second call lands, and that new run
is not the one your cancel call catches -- it is left stuck "running" with no
error (an upstream race; see the `paperclip-comment-then-cancel-race` incident
note). One combined call has no such window.

**Budget.** At most 10 mutations per wake. Every mutation (cancel, comment-only
nudge, or flag) carries a one-line reason comment -- never a bare status flip.

1. **Close done.** An issue in `done`/`in_review` whose own thread or a linked
   work product proves the deliverable (a merged PR link, a "sent" plus the
   artifact) -> set `done` with a one-line comment citing the evidence.
2. **Merge duplicates.** A routine-execution issue sharing its routine and
   business date with a LATER run of that routine (`GET
   /api/routines/{routineId}/runs?limit=50` lists a routine's runs and their
   linked issues) -> cancel it: `"duplicate of <the later issue>"`.
3. **Cancel stale.** A routine-execution issue superseded by a later `done`
   run of the same routine, or ANY issue idle 14+ days whose parent or
   blocker is `done`/`cancelled` -> cancel with a one-line reason. You MAY
   cancel a human-created or human-assigned issue under this rule too --
   always with the reason comment; this is a deliberate exception to
   "never cancel cross-team tasks" for stale/superseded work specifically,
   not a general license to cancel human to-dos outside this checklist.
4. **Nudge stalled.** An agent-assigned `todo`/`in_progress` issue with no run
   and no comment in the last 48h, and no active recovery action already
   pending on it -> ONE comment mentioning the assignee (a mention wakes
   them). Do not nudge the same issue twice within 7 days -- check its last
   comment's timestamp and author first.
5. **Flag human-needed.** Anything left that needs an actual human decision
   (a PR waiting on a merge, a spend or policy call, a pending approval, a
   board-owned recovery action) -> post ONE `ask_user_questions` or
   `suggest_tasks` card (mechanics: `references/interactions.md`) on a
   single rolling issue titled "Board review": search for an open one
   assigned to you first (`?q=Board+review&status=todo,in_progress,in_review,blocked`),
   reuse it if found, else create one (`--assignee` yourself). Set it
   `in_review`. Do not open a new "Board review" issue per wake.

**No Lightbulb room tools here.** Do not call `send_room_message` or any other
Lightbulb-plugin tool from this checklist -- a Paperclip-triggered run has no
active Lightbulb turn to send into, and the call fails (ISOA-377). Comments on
the Paperclip issue thread are the only channel this checklist uses.

**Carried from the retired nightly routine.** Three lines earned their keep;
everything else about that routine (title, daily cadence as its own
mechanism, the owner email) is gone:

- Advance ONE real constraint per wake -- pick the single highest-leverage
  blocker across the board and make concrete progress on it, rather than
  touching everything shallowly.
- If the board is empty (no open issues at all), create ONE goal-derived
  task instead of leaving nothing in flight.
- Do not create a daily status issue. This checklist's comments are the
  record; a separate status issue is exactly the board clutter this section
  exists to clean up.

---

## Daily owner brief (timer wakes)

**When.** On a timer wake (`- reason: heartbeat_timer`), read `owner_timezone`
from `$HERMES_HOME/lightbulb-facts.json` (`UTC` if absent). If the local time
there is 08:00 or later, write today's brief. `<date>` below is today's date in
that zone, `YYYY-MM-DD`.

**Once a day, no state file.** The post's `--event-id brief-<date>` is the
"already sent today" record: a later wake's attempt prints `Already posted: …`
and sends nothing. Do not keep a file or memory note for this.

**Content** -- short, markdown, four headings, skip an empty one:

- **Moved** -- what finished or advanced since the last brief.
- **Blocked** -- what is stuck, and on what.
- **Needs you** -- decisions only a human can make; link the rolling
  "Board review" issue from Board hygiene rule 5.
- **Next** -- what the team does today.

**Deliver it** -- post first, then email only on a fresh post:

```bash
out="$(printf '%s' "$BRIEF" | /app/skills/lightbulb/scripts/lightbulb-post.sh --event-id "brief-<date>")"
case "$out" in
  "Posted to room "*)
    printf '%s' "$BRIEF" | /app/skills/lightbulb/scripts/lightbulb-mail.sh send --owner --subject "Daily brief — <date>" ;;
esac
```

A non-zero exit (not posted) or `Already posted: …` (today's brief is already
out) sends NO email -- that is what keeps the owner from getting two.

This is not the mobile "needs you" digest: the brief is yours and links the
Board review issue; the digest is the platform's.

**No Lightbulb room tools** (`send_room_message` and friends) -- same reason
as Board hygiene: this run has no room turn to send into. The two scripts are
the only delivery path.

