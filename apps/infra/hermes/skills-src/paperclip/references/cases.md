# Cases

Cases are agent-owned work records for durable outputs such as blog posts,
research packets, release notes, incidents, QA runs, or generated asset sets.
They are company-scoped and live beside issues: issues coordinate work, while
cases preserve the structured object an agent is producing.

Cases are experimental and must be enabled with `experimental.enableCases`.
If a route returns `403 Cases are disabled`, stop and report that the operator
must enable cases before the skill can use this surface.

## Core Model

A case has:

- `identifier`: server-assigned display id such as `PAP-C42`
- `caseType`: skill-owned type such as `blog_post`, `image_assets`, or `incident`
- `key`: optional deterministic upsert key inside `(companyId, caseType)`
- `title` and optional `summary`
- `status`: `draft`, `in_progress`, `in_review`, `approved`, `done`, or `cancelled`
- `fields`: JSON object owned by the skill using the case
- `parentCaseId`: optional parent case for child work
- documents, attachments, issue links, labels, and events

Use deterministic `caseType` + `key` when a skill may be retried. Repeating
`POST /api/companies/:companyId/cases` with the same `caseType` and `key`
upserts the same case instead of creating a duplicate.

There is no dedicated `paperclip-api.sh` subcommand for cases — every write
below goes through the `raw <METHOD> </api/...path>` escape hatch with a
`jq -n`-built body on stdin, never hand-typed JSON.

## Upsert Semantics

`POST /api/companies/:companyId/cases` creates or upserts a case:

```bash
jq -n '{
  caseType: "blog_post",
  key: "launch-announcement",
  title: "Launch announcement",
  summary: "Draft launch post for operators.",
  status: "draft",
  fields: {
    slug: "launch-announcement",
    target_audience: "operators"
  }
}' | /app/skills/paperclip/scripts/paperclip-api.sh raw POST "/api/companies/$PAPERCLIP_COMPANY_ID/cases"
```

Response:

- `201` when a new case was created
- `200` when an existing `(caseType, key)` case was updated

Field behavior on upsert:

- `title` is required and replaces the previous title.
- `projectId`, `summary`, `status`, `fields`, and `parentCaseId` replace the
  previous value when present.
- Omitted optional values preserve the previous value during upsert.
- `fields` is replaced as a whole object when provided. It is not deep-merged.
  Send the complete desired JSON object each time.
- Concurrent retries with the same `(caseType, key)` converge to one case.

Do not use a random `key` for retryable skills. Use a stable content slug,
external id, source URL hash, or parent-derived request key.

## Read And Search

Get a case by UUID or identifier:

```
GET /api/cases/PAP-C42
```

List cases for a company:

```
GET /api/companies/:companyId/cases?type=blog_post&status=active&q=launch
```

Useful filters:

- `type`: exact `caseType`
- `status`: exact lifecycle status, or `active` for non-terminal cases
- `projectId` / `project`: project UUID
- `labelId` / `label`: label UUID
- `q`: identifier, title, summary, or key search
- `limit`: 1-200, default 100

## Documents

Use case documents for rich bodies such as drafts, briefs, reports, or plans:

```bash
jq -n '{
  title: "Launch announcement body",
  format: "markdown",
  body: "# Launch announcement\n\nDraft copy...",
  changeSummary: "Initial draft"
}' | /app/skills/paperclip/scripts/paperclip-api.sh raw PUT /api/cases/PAP-C42/documents/body
```

Updating an existing case document requires `baseRevisionId`:

```bash
jq -n '{baseRevisionId: "latest-revision-uuid", body: "Updated body"}' \
  | /app/skills/paperclip/scripts/paperclip-api.sh raw PUT /api/cases/PAP-C42/documents/body
```

If you get `409 stale_base_revision`, refetch the case detail, read the latest
document revision id, merge intentionally, and retry with that `baseRevisionId`.

## Fields

Each skill owns the schema of `fields` for the `caseType` it creates. Keep fields
small, typed, and stable enough for other agents to inspect.

Examples:

```json
{
  "slug": "launch-announcement",
  "target_audience": "operators",
  "publish_url": "https://example.com/blog/launch-announcement"
}
```

Patch fields or status with:

```bash
jq -n '{
  status: "in_review",
  fields: {
    slug: "launch-announcement",
    target_audience: "operators",
    publish_url: "https://example.com/blog/launch-announcement"
  }
}' | /app/skills/paperclip/scripts/paperclip-api.sh raw PATCH /api/cases/PAP-C42
```

Remember: `fields` replaces the whole object when present.

## Issue Links

Link cases to issues explicitly when needed:

```bash
jq -n '{issueId: "issue-uuid", role: "reference"}' \
  | /app/skills/paperclip/scripts/paperclip-api.sh raw POST /api/cases/PAP-C42/links
```

Roles:

- `origin`: the issue/run that created the case
- `work`: an issue/run that changed the case
- `reference`: related issue context

Agent run writes auto-link the run's issue when Paperclip can resolve it from
the run JWT or `X-Paperclip-Run-Id` (which `paperclip-api.sh` always sends when
`PAPERCLIP_RUN_ID` is set). Creation/upsert writes use `origin`; later
document, patch, and attachment writes use `work` when no link already exists.
You do not need to manually link the current issue before writing the case.

## Child Cases

Create child cases by setting `parentCaseId` to the parent case UUID:

```bash
jq -n '{
  caseType: "image_assets",
  key: "launch-announcement:hero-images",
  title: "Hero images for launch announcement",
  parentCaseId: "parent-case-uuid",
  fields: {required_assets: ["hero", "social-card"]}
}' | /app/skills/paperclip/scripts/paperclip-api.sh raw POST "/api/companies/$PAPERCLIP_COMPANY_ID/cases"
```

Use child cases when the output has independently inspectable pieces or when
another agent can work on a bounded part without editing the parent case body.

## Attachments

Attach generated files with multipart form data. This is the one case write
`raw` cannot carry — its body encoder is JSON-only — so use `curl` directly,
the same way `/app/skills/paperclip/scripts/paperclip-upload-artifact.sh` does for issue attachments:

```bash
curl -sS -X POST "$PAPERCLIP_API_URL/api/cases/PAP-C42/attachments" \
  -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
  -H "X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID" \
  -F "file=@hero.png"
```

The server records an asset and adds an `attachment_added` case event.

## Lifecycle

Use the lifecycle consistently:

- `draft`: case exists but useful work has not started
- `in_progress`: an agent is actively producing or revising it
- `in_review`: ready for reviewer, board, or downstream approval
- `approved`: accepted but not finally shipped or archived
- `done`: complete and no further action remains
- `cancelled`: intentionally abandoned

Terminal statuses are `done` and `cancelled`; setting either records
`completedAt`. Moving back to a non-terminal status clears `completedAt`.

## Worked Blog Post Example

Create or upsert the parent blog post:

```bash
jq -n '{
  caseType: "blog_post",
  key: "paperclip-cases-launch",
  title: "Introducing Paperclip Cases",
  summary: "Blog post explaining the cases surface for agent outputs.",
  status: "in_progress",
  fields: {
    slug: "paperclip-cases-launch",
    target_audience: "AI company operators",
    publish_url: null
  }
}' | /app/skills/paperclip/scripts/paperclip-api.sh raw POST "/api/companies/$PAPERCLIP_COMPANY_ID/cases"
```

Write the body:

```bash
jq -n '{
  title: "Introducing Paperclip Cases",
  format: "markdown",
  body: "# Introducing Paperclip Cases\n\n..."
}' | /app/skills/paperclip/scripts/paperclip-api.sh raw PUT /api/cases/PAP-C42/documents/body
```

Create the child image-assets case:

```bash
jq -n '{
  caseType: "image_assets",
  key: "paperclip-cases-launch:image-assets",
  title: "Image assets for Introducing Paperclip Cases",
  parentCaseId: "parent-case-uuid",
  status: "in_progress",
  fields: {
    slug: "paperclip-cases-launch",
    required_assets: ["hero", "social-card"],
    publish_url: null
  }
}' | /app/skills/paperclip/scripts/paperclip-api.sh raw POST "/api/companies/$PAPERCLIP_COMPANY_ID/cases"
```

Attach generated assets to the child, then patch both cases as they move through
review:

```bash
jq -n '{
  status: "in_review",
  fields: {
    slug: "paperclip-cases-launch",
    target_audience: "AI company operators",
    publish_url: "https://example.com/blog/paperclip-cases-launch"
  }
}' | /app/skills/paperclip/scripts/paperclip-api.sh raw PATCH /api/cases/PAP-C42
```

If the same skill retries the example with the same keys, it updates the parent
and child cases rather than creating duplicates.
