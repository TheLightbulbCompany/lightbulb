# Asking the board: approvals and issue-thread interactions

## When a person must decide: raise an approval

**A decision that belongs to a person is a Paperclip approval.** It shows in
their **Needs you** with Approve / Reject buttons, and approving it wakes you
with the answer. These are NOT approvals, and never stand in for one:

- a task assigned to the person ("Approve pricing packages", assigned to them);
- a question in chat or a comment ("Can you sign off on this?");
- an issue left `blocked` with no approval on it.

When a person asks for "an approval", "a sign-off" or "something in Needs you",
call the **`request_approval`** tool in that turn. It is the only way to raise
one: no helper command, no hand-built body.

```
request_approval(title="Approve pricing packages",
                 summary="Starter $49/mo, Studio $149/mo, Custom quoted. Details: [Pricing](/WAL/issues/WAL-6#document-pricing)",
                 tasks=["WAL-6"],
                 recommendedAction="Publish the pricing page.")
```

- `title` is the decision; `summary` is what the person needs to decide it,
  with the document link. The long detail stays in the linked document.
- `tasks` takes identifiers like `WAL-6`; the tool links them and moves each to
  `in_review` (a linked approval is a real review path). A task it could not
  move is listed under `notMovedToInReview`.
- `companyPlan: true` only for your plan or strategy for the whole company. The
  tool files that as `approve_ceo_strategy` when you are the root CEO, and as
  `request_board_approval` for everyone and everything else. A CEO's first plan
  for a new workspace always ends with one.

It returns `approvalId` and `link`. Then:

- tell the person in one line what you asked and give the link ("I've put the
  pricing sign-off in Needs you: <link>"); it renders as an Approve card;
- do not raise a second approval for the same decision; `GET
  /api/companies/{companyId}/approvals?status=pending` shows what is open.

`hire_agent` is Lightbulb's own (never file it; see the Hiring rule) and
`budget_override_required` is raised by Paperclip, not by you.

Approval wakes you with `PAPERCLIP_APPROVAL_ID`/`PAPERCLIP_APPROVAL_STATUS`
(`references/heartbeat.md` Step 2). A reject does **not** wake you; the person's
next message or comment will.

## MCP Tool Approval Gates

Some MCP tools are configured as **ask first**; their `tools/list` description
says human approval is required. Calling one posts an approval card on your
checked-out task and returns `approval_required` — do not retry the call while
it is pending. Finish other useful work, move the task to `in_review`, and end
the run; Paperclip wakes the assignee after approval or rejection with the
decision (and, if approved, the execution outcome — approval means **approve
and run**: the stored, signed call executes exactly once). A rejected call did
not run; follow the decline reason and change your approach rather than
retrying the same call. Approval requests expire after 60 minutes — a fresh
call after expiry opens a new one — and re-calling with identical arguments is
idempotent: it reuses a pending request, returns a stored outcome, or opens one
fresh card, but never stacks duplicates. If the gateway returns
`approval_path_missing`, re-run the action from a run that has the task
checked out.

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

- **Who may resolve it.** Paperclip lets ANY participant — other agents included — resolve an interaction unless it says otherwise. A card that asks the board/user to decide must carry `"resolverPolicy": "board_only"` (the `confirm` helper sets it; add it yourself to every `raw` create). Never resolve a board card yourself.
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
  "resolverPolicy": "board_only",
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

