# Teams Issue Documents — Design

**Roadmap:** sub-project **D**, piece **D4** (documents). After D1 sub-issues + D2 linked approvals + D3 run list.

**Status:** Spec

## Goal

Add a read-only "Documents" section to the `/teams` issue-detail page: the issue's documents, each expandable to show its rendered-markdown body. A pragmatic Isol8 take on upstream Paperclip's `IssueDocumentsSection` (1348-line full editor) — we ship only the read path.

## Background

- Upstream `GET /api/issues/:id/documents` (`paperclip server/src/routes/issues.ts:2489`) → `documentsSvc.listIssueDocuments(issueId)` → a bare array of documents, **issue-scoped**. The body is returned **inline** in the list response (`documents.ts:155` maps every row with `includeBody=true`), so a read-only section needs **one** endpoint — no fetch-by-key round trip.
- The default response **excludes system documents** (`?includeSystem` defaults false upstream); the passthrough does not set it, so system docs are filtered for free.
- Document shape (subset we use): `id`, `key` (the stable slug, e.g. `"plan"`/`"report"`), `title` (nullable), `format` (`"markdown"` default), `body` (markdown string, inline), `updatedByAgentId`/`updatedByUserId` (nullable), `updatedAt`, `createdAt`. The rest (revisions, locks, annotations, voting, file download, drafts) is editor/operator-flavored.
- Isol8 has no per-issue documents endpoint and **no teams markdown renderer** — the only `react-markdown` usage is `chat/MessageList.tsx`. `react-markdown ^10.1.0` + `remark-gfm ^4.0.1` are in `package.json`.

## Decisions

1. **Read-only document list, not the editor.** Render a list of the issue's documents. **Drop** drafts/autosave, revision history, lock/unlock, annotations, voting, file download, and the system-doc toggle (operator concerns; consistent with the roadmap's out-of-scope flags).
2. **One BFF endpoint.** `GET /issues/{id}/documents` → bare array → `envelope_list(rows, "documents")`. Mirrors `list_issue_runs` / `list_issue_approvals` exactly (issue-scoped passthrough).
3. **Each document row:** a `key` badge (mono, uppercase), an optional title (shown only when present and not equal to the key), a relative "updated {timeAgo}", and an expand/collapse toggle. **Default collapsed** (bodies can be long); expanded renders the markdown body.
4. **Markdown rendering, safe + themed.** Render `body` with `react-markdown` + `remark-gfm`. Do **not** enable `rehype-raw`, so raw HTML in a body is inert (rendered as text) — the only injection vector is link `href`s, which we guard (`javascript:`/`data:` → inert) exactly as `MessageList` does. Theme with the teams CSS vars (`var(--ink)`, `var(--mute)`, `var(--edge)`, `var(--forest)`) for visual parity with the sibling sections, not `MessageList`'s hardcoded hexes.
5. **Newest first** (by `updatedAt ?? createdAt` desc), consistent with the Runs section.
6. **Hide-when-empty.** Render nothing when the issue has no documents (matches upstream's conditional display).

## Architecture

### BFF (`routers/teams/issues.py`) — issue-scoped passthrough
- `GET /issues/{issue_id}/documents` → `_admin().list_issue_documents(issue_id)`, wrapped `envelope_list(rows, "documents")`.

### admin client (`core/services/paperclip_admin_client.py`)
- `list_issue_documents(*, session_cookie, issue_id)` → `GET /api/issues/{issue_id}/documents` (issue-scoped, mirrors `list_issue_runs`).

### Frontend
- `shared/types.ts` — add a minimal `IssueDocument` type: `{ id; key; title?; format?; body?; updatedByAgentId?; updatedByUserId?; updatedAt?; createdAt? }` (field names match the upstream API row; `body` is the inline markdown).
- `issues/IssueDocumentsSection.tsx` (new) — `read<{documents: IssueDocument[]}>("/issues/{id}/documents")`; sort newest-first; render a "Documents" header + a row per document; render nothing when empty. Takes `{ issueId, agents }` (`agents` resolves `updatedByAgentId` → display name, matching `IssueRunsSection`).
- `issues/DocumentRow.tsx` (new, or a local child component) — owns the per-row expand/collapse `useState`; collapsed shows the header line; expanded additionally renders the markdown body via the renderer below.
- The markdown renderer is a small module-level config (remark-gfm + safe link `components` map + CSS-var theming) used by `DocumentRow`. Kept local to D4 (YAGNI); extract to a shared `MarkdownView` only if D5 needs it.
- `IssueDetailPage.tsx` — mount `<IssueDocumentsSection issueId={issueId} agents={agents} />` immediately after `<IssueRunsSection>`, before the Conversation block.

## Out of scope (deferred)
Document create/edit/upsert, revision history + restore, lock/unlock, annotations (threads), voting, file/attachment download, the `?includeSystem` system-doc toggle, and per-document permalink pages.

## Testing
- BFF: `GET /issues/{id}/documents` forwards + envelopes (admin `list_issue_documents`).
- `IssueDocumentsSection`: renders nothing when empty; renders documents newest-first with a key badge + title + updated time; collapsed by default (body markdown **not** in the DOM initially); expanding a document renders its markdown body (e.g. a `# Heading` becomes a heading element / the body text appears).

## Convention
Plan: `docs/superpowers/plans/2026-05-29-teams-issue-documents.md`.
