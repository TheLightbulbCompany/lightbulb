# Teams Issue Comments — Richer Rendering — Design

**Roadmap:** sub-project **D**, piece **D5** (richer comments), the final D piece. After D1 sub-issues / D2 approvals / D3 runs / D4 documents.

**Status:** Spec

## Goal

Make the `/teams` issue-detail comment thread render the way upstream Paperclip's does: comment bodies as **markdown** (today they show raw text), real **author names** (today a generic "Agent"/"User"), and **relative timestamps**. Frontend-only; reuses the markdown renderer built for D4 by extracting it into a shared component.

## Background

- Today `IssueComments.tsx` renders the body as plain text (`whitespace-pre-wrap`, line 95-100), shows a generic `authorLabel` ("Agent"/"User"/"Comment", line 22-28), and an absolute `toLocaleString()` timestamp (line 91-93). Its header docstring notes it deliberately "Drops markdown editor, @mentions, file upload, action flags, edit/delete."
- Upstream renders comment bodies via `MarkdownBody` (`react-markdown` + `remark-gfm`, no raw HTML — same posture as Isol8 D4) and shows agent names + mention chips.
- Agents already post markdown in comment bodies; it just renders as raw text today. So markdown rendering is the highest-value, lowest-effort, board-only win (no agent-side or BFF change).
- The comment endpoints already exist (`GET`/`POST /issues/{id}/comments`); `IssueComment` already carries `authorAgentId`/`authorKind`/`createdAt`. The page already loads `agents`. So **no backend change is needed.**
- D4 built a markdown config (`markdownComponents` + `safeHref` + `<ReactMarkdown remarkPlugins={[remarkGfm]}>`) inside `DocumentRow.tsx`. The D4 review noted it should be extracted to a shared component "only if D5 needs it" — D5 needs it.

## Decisions

1. **Extract a shared `MarkdownView`.** Move D4's `markdownComponents` map + `safeHref` + the `<ReactMarkdown>` call out of `DocumentRow.tsx` into `shared/components/MarkdownView.tsx`. It's pure/stateless (nothing references documents), so this is a lift-and-shift. `MarkdownView({ children }: { children: string })` renders just the `<ReactMarkdown>` (no wrapper div) so callers theme their own container. `DocumentRow` is refactored to use it (no behavior change). Same safety posture carries over: no `rehype-raw`/`dangerouslySetInnerHTML` (raw HTML inert), link `href`s guarded against `javascript:`/`data:`.
2. **Render comment bodies via `MarkdownView`.** Swap `IssueComments.tsx`'s plain `{c.body}` for `<MarkdownView>{c.body}</MarkdownView>`, keeping the existing soft-ink container styling on a wrapping div.
3. **Resolve author names.** `IssueComments` takes an `agents: CompanyAgent[]` prop; `authorLabel` resolves `authorAgentId` → agent name (fallback "Agent"). Human authors stay "User" (showing a human's display name needs a members join — deferred). `IssueDetailPage` passes its in-scope `agents`.
4. **Relative timestamps.** Replace `toLocaleString()` with `timeAgo(c.createdAt)`, keeping the absolute time as a `title` tooltip (and the machine-readable `dateTime` attr).
5. **Composer unchanged.** Keep the plain-textarea + Cmd/Ctrl+Enter composer. A rich MDXEditor mention-autocomplete composer is a heavy new dependency — deferred.

## Out of scope (deferred)

- **Mention chips** (display or composer autocomplete) — depends on the exact mention format agents emit + (for the composer) a new mentionable-agents API and the MDXEditor dependency. Agent-emission/editor-dependent; deferred per the roadmap.
- **Edit comments** — upstream has NO edit route at all.
- **General delete** — upstream `DELETE` only cancels a *queued* comment on an active run (not a general delete); niche, deferred.
- **Reactions / votes** — upstream "votes" are agent-output up/down feedback needing a new BFF route + data-sharing machinery; real emoji reactions don't exist upstream. Deferred.
- **Reopen / reassign via comment** — agent-wake- and selector-dependent; stays the separate issue-action surface.

## Architecture

### Frontend only — no BFF / backend change
- `shared/components/MarkdownView.tsx` (new) — owns `safeHref` + `markdownComponents` + `<ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{children}</ReactMarkdown>`. Exports `MarkdownView`.
- `issues/DocumentRow.tsx` (modify) — delete the local `safeHref`/`markdownComponents`/`ReactMarkdown` imports + definitions; render `<MarkdownView>{doc.body}</MarkdownView>` inside the existing container. No visual change.
- `issues/IssueComments.tsx` (modify) — add `agents: CompanyAgent[]` to props; `authorLabel(c, agents)` resolves agent names; render body via `<MarkdownView>` in a soft-ink container; timestamp via `timeAgo` with an absolute `title`.
- `issues/IssueDetailPage.tsx` (modify) — pass `agents={agents}` to `<IssueComments>`.

## Testing
- `MarkdownView`: renders gfm markdown (a heading/list/inline-code renders to elements/text); a `javascript:` link href is neutralized (rendered inert, not as a live link).
- `DocumentRow`/`IssueDocumentsSection`: existing tests stay green (extraction is behavior-preserving — expand still renders the markdown body).
- `IssueComments`: a markdown comment body renders as markdown (not raw `##`); `authorAgentId` resolves to the agent's name from the `agents` prop (fallback "Agent" when unknown); a relative timestamp shows.

## Convention
Plan: `docs/superpowers/plans/2026-05-29-teams-issue-comments-rich.md`.
