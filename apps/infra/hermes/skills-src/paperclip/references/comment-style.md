# Comment Style (Required)

This style is for Paperclip issue comments and descriptions only. A chat message (a room reply, a DM, a `lightbulb-post.sh` post) is never shaped like this: write it in plain sentences, with no headings or status lines.

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
- Work products: `/<prefix>/issues/<issue-identifier>#work-product-<work-product-id>` (deep link to a specific deliverable)
- Agents: `/<prefix>/agents/<agent-url-key>` (e.g., `/PAP/agents/claudecoder`)
- Projects: `/<prefix>/projects/<project-url-key>` (id fallback allowed)
- Goals: `/<prefix>/goals/<goal-id>`
- Approvals: `/<prefix>/approvals/<approval-id>`
- Runs: `/<prefix>/agents/<agent-url-key-or-id>/runs/<run-id>`

The company prefix is ALWAYS required, on every link kind above — never
unprefixed paths like `/issues/PAP-123` or `/agents/cto`.

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

If the plan needs explicit approval before implementation, update the `plan` document, then create a `request_confirmation` issue-thread interaction with `/app/skills/paperclip/scripts/paperclip-api.sh confirm <issueId> --plan --prompt "Accept this plan?"` (optional details markdown on stdin; the helper sets `payload.version` and `payload.prompt` correctly so the card cannot be malformed, and `--plan` binds the card to the latest plan revision — without it an accepted card does not switch a planning-mode issue back to standard). Then update the source issue to `in_review` with a comment that links the plan and names the pending confirmation. This is a deliberate waiting path, not an abandoned productive run. Wait for acceptance before creating implementation subtasks. See `references/api-reference.md` for the full interaction payload (e.g. binding the confirmation to the latest plan revision via `target`).

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

