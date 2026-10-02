# Work Modes (Standard, Ask, Planning)

Every issue has a `workMode`. The person who filed the task picks it, and they
can change it later. Your wake prompt names the mode when it is not standard
(`- Work mode: "ask"` or `- Work mode: "planning"`, followed by a directive);
`GET /api/issues/{issueId}` returns it as `workMode`. The mode is an instruction
to you. Nothing blocks a write that breaks it, so keeping to it is your job.

| Mode | What you do | What you do not do |
| - | - | - |
| `standard` | The work, as usual. | |
| `ask` | Answer the question directly in the issue thread. Use tools only to investigate or for temporary scratch work. The deliverable is the answer. | Write implementation code. Produce an implementation plan. |
| `planning` | Write the plan to the issue's `plan` document, and only there. | Write code or do any implementation work. Put the plan in a workspace file or the issue description. |

Do not change an issue's `workMode` yourself. In Planning mode the accepted
plan changes it for you (below).

## The plan loop

1. **Write the plan** to the `plan` issue document
   (`PUT /api/issues/{issueId}/documents/plan`; document rules are in
   `references/comment-style.md`). If the document already exists, fetch it
   first and send its latest `baseRevisionId`. When a comment woke you in
   Planning mode, update that same document; do not start a second one.
2. **Ask for acceptance.** File a `request_confirmation` interaction whose
   `payload.target` is the plan document: `type: "issue_document"`,
   `key: "plan"`, and the revision you just wrote. The full body is the plan
   approval example in `references/api-reference.md`; send it with
   `/app/skills/paperclip/scripts/paperclip-api.sh raw POST "/api/issues/$PAPERCLIP_TASK_ID/interactions"`.
   The `confirm` helper sets no `target`, so a card filed with it does not
   start execution when accepted. Use the `raw POST` here.
3. **Stop and wait.** Set the issue to `in_review` with a comment that links
   the plan (`#document-plan`) and names the pending confirmation. Do not
   start the work and do not create implementation subtasks.
4. **On acceptance** Paperclip switches the issue back to `standard` and wakes
   you with an "Accepted plan directive". Now implement the accepted plan on
   this issue when the work is small and cohesive. Create child issues only
   where ownership, parallel work, a dependency, a review or a lifecycle
   boundary calls for one; an accepted plan is not by itself a reason to
   split the work.
5. **On rejection** nothing starts, and the rejection alone does not wake you
   by default. When the person's feedback does wake you (a comment, or a
   "Rejected plan review directive"), revise the same `plan` document and file
   a fresh confirmation. A rejected plan is not approval to implement, and
   neither is the issue being `in_progress`.
