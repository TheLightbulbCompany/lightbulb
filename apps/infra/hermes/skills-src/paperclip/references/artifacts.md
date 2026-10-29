# Generated Artifacts and Work Products

**The Library shows only three things:** issue documents you wrote (any key
but the system ones), work products, and attachments on a task. A comment, the
issue description and a chat message never appear there. So every deliverable
(a plan, a doc, an outreach plan, a report, a file) is saved as one of those
three before you say it is done, and "the full doc is in the task thread" is
never the answer: write it as an issue document and link
`/<prefix>/issues/<identifier>#document-<key>`. A comment on the task says what
changed and links the document; it does not carry the deliverable's text.

When work produces a user-inspectable file, upload true deliverables to the current issue before final disposition. Local filesystem paths are not enough because board users, reviewers, and cloud operators may not have access to the agent workspace.

Use the helper bundled with this skill. From an installed `paperclip` skill directory, the helper lives at `/app/skills/paperclip/scripts/paperclip-upload-artifact.sh`:

```bash
/app/skills/paperclip/scripts/paperclip-upload-artifact.sh path/to/output.webm \
  --title "Walkthrough render" \
  --summary "Rendered walkthrough for review"
```

The helper uses `PAPERCLIP_API_URL`, `PAPERCLIP_API_KEY`, `PAPERCLIP_COMPANY_ID`, `PAPERCLIP_TASK_ID`, and `PAPERCLIP_RUN_ID`. It uploads the file as an issue attachment, creates an attachment-backed artifact work product by default, and prints issue-safe markdown links for your final comment.

## Plans and deliverables live in Paperclip, not files

**A plan is never a workspace file.** Do not write a plan — a design, an
approach, a regression-test plan, a rollout plan — as a local markdown file and
reference its path. A plan belongs in the issue's **plan document**, so the
board can read, comment on, and version it without workspace access:

```bash
curl -sS -X PUT \
  "$PAPERCLIP_API_URL/api/issues/$PAPERCLIP_TASK_ID/documents/plan" \
  -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
  -H "X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID" \
  -H "Content-Type: application/json" \
  --data-binary '{
    "title": "Implementation plan",
    "format": "markdown",
    "body": "## Approach\n…",
    "changeSummary": "Initial plan",
    "baseRevisionId": null
  }'
```

If the `plan` document already exists, fetch it first
(`GET /api/issues/$PAPERCLIP_TASK_ID/documents/plan`) and pass its latest
revision id as `baseRevisionId` so you build on the current version instead of
forking it.

**To get a plan approved before implementing**, bind the decision to the plan
revision. Create a `request_confirmation` issue-thread interaction whose
`target` names the latest plan revision:

```json
{ "type": "issue_document", "key": "plan", "revisionId": "<latest-plan-revision-id>" }
```

Then move the source issue to `in_review` with a comment that names the pending
decision. This is a deliberate waiting path — **wait for acceptance before
creating implementation subtasks.** The bundled helper builds the common case:
`/app/skills/paperclip/scripts/paperclip-api.sh confirm <issueId> --plan --prompt "Accept this plan?"`
(details markdown on stdin; `--plan` reads the latest plan revision and sets
that `target` for you). See `SKILL.md` and `references/api-reference.md`
for the full interaction payload and the stale-target/rebuild rules.

**A deliverable asked for in chat is a document too.** When an owner asks in
chat for research, a plan, a draft or a report, write it as a file attached to
the reply; when the work already is a task, write it as an issue document (any
key, e.g. `research`) on that task and reply with a short summary and the
`/<prefix>/issues/<identifier>#document-<key>` link — the lightbulb skill's
`references/chat-vs-document.md` has the steps and the chat wording.

**A finished deliverable is a work product, not a loose file** — upload it with
the helper above (an attachment-backed artifact). Plans, documents, and any
board-facing output never belong in a `workspace_file` reference.

## Workspace-Only File References — real source files only

Use a workspace-only reference **only** for a genuine source file — code, a
committed report, or a generated index — whose value is tied to the project or
execution checkout. This is NOT a substitute for the plan document (above) or
for uploading a board-facing deliverable, and a **plan must never be modeled
this way**.

Annotate the work product with `metadata.resourceRef`:

```json
{
  "type": "document",
  "provider": "workspace",
  "title": "Generated symbol index",
  "status": "ready_for_review",
  "reviewState": "needs_board_review",
  "summary": "Symbol index generated from the source tree and committed in the execution workspace.",
  "metadata": {
    "resourceRef": {
      "kind": "workspace_file",
      "issueId": "<issue-id>",
      "workspaceKind": "execution_workspace",
      "workspaceId": "<execution-workspace-id>",
      "relativePath": "src/generated/symbol-index.md",
      "line": 1,
      "displayPath": "src/generated/symbol-index.md"
    }
  }
}
```

`workspaceKind` is `execution_workspace` for the current issue checkout or
`project_workspace` for a shared project workspace. `line` and `column` are
optional positive integers. `relativePath` must be relative to the selected
workspace root; do not use host-local absolute paths in `resourceRef`.

Create the work product with:

```bash
curl -sS -X POST \
  "$PAPERCLIP_API_URL/api/issues/$PAPERCLIP_TASK_ID/work-products" \
  -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
  -H "X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID" \
  -H "Content-Type: application/json" \
  --data-binary @workspace-file-work-product.json
```

If the helper is unavailable, upload a deliverable file through the Paperclip API directly:

```bash
curl -sS -X POST \
  "$PAPERCLIP_API_URL/api/companies/$PAPERCLIP_COMPANY_ID/issues/$PAPERCLIP_TASK_ID/attachments" \
  -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
  -H "X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID" \
  -F 'file=@"path/to/output.webm";type=video/webm'
```

Then create a work product when the file is the deliverable. The server canonicalizes attachment-backed artifact metadata from the `attachmentId`:

```bash
curl -sS -X POST \
  "$PAPERCLIP_API_URL/api/issues/$PAPERCLIP_TASK_ID/work-products" \
  -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
  -H "X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID" \
  -H "Content-Type: application/json" \
  --data-binary '{
    "type": "artifact",
    "provider": "paperclip",
    "title": "Walkthrough render",
    "status": "ready_for_review",
    "reviewState": "needs_board_review",
    "isPrimary": true,
    "metadata": { "attachmentId": "<uploaded-attachment-id>" }
  }'
```

## Give every issue a real disposition

In your final issue comment, link the uploaded attachment or work product and
describe what it contains. If the output is a genuine workspace source file,
name the work product and the relative path recorded in `metadata.resourceRef`.
Browse/search is the fallback for recovering a workspace file when the issue
chip or link cannot open it; it is not the preferred deliverable path.

**Never leave an issue `in_progress` with only a comment or a local path.** Give
it a real disposition:

- `done` — the deliverable is uploaded (plan document or work product) and the
  work is complete.
- `in_review` — it needs sign-off; pair it with a `request_confirmation` (or
  another explicit reviewer path) so the wait is real, not a self-assigned
  "please review" comment.
- `blocked` — it cannot proceed; name the blocker and who must act.
