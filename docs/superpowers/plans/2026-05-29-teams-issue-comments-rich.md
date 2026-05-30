# Teams Issue Comments — Richer Rendering — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Render `/teams` issue comments as markdown with real author names and relative timestamps, by extracting D4's markdown renderer into a shared `MarkdownView`.

**Architecture:** Frontend only — no BFF/backend change. Extract `MarkdownView` from `DocumentRow`; reuse it in `IssueComments`; resolve agent names from the `agents` prop; use `timeAgo`.

**Spec:** `docs/superpowers/specs/2026-05-29-teams-issue-comments-rich-design.md`

Conventions: frontend from `apps/frontend` (`pnpm test -- <substr>`). Per-task targeted tests; full suite/tsc/build in the final task. The shared markdown renderer must preserve D4's safety posture: no `rehype-raw`/`dangerouslySetInnerHTML`, link `href`s guarded against `javascript:`/`data:`.

---

## Task 1: Extract `MarkdownView` + refactor `DocumentRow`

**Files:** Create `apps/frontend/src/components/teams/shared/components/MarkdownView.tsx`; Modify `apps/frontend/src/components/teams/issues/DocumentRow.tsx`; Create test `apps/frontend/src/__tests__/teams/MarkdownView.test.tsx`. Keep `IssueDocumentsSection.test.tsx` green (it covers DocumentRow's markdown rendering indirectly).

- [ ] **Step 1: Create `shared/components/MarkdownView.tsx`** — lift the `safeHref` + `markdownComponents` + `<ReactMarkdown>` verbatim out of `DocumentRow.tsx` into a standalone, stateless component (no wrapper div; callers theme their own container):
```tsx
// apps/frontend/src/components/teams/shared/components/MarkdownView.tsx
"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";

function safeHref(href?: string): string | undefined {
  if (!href) return undefined;
  const v = href.trim().toLowerCase();
  if (v.startsWith("javascript:") || v.startsWith("data:")) return undefined;
  return href;
}

// react-markdown does NOT render raw HTML (no rehype-raw), so the body is
// inert apart from link hrefs, which we guard. Themed with the teams CSS vars.
const markdownComponents: Components = {
  a: ({ href, children, ...props }) => {
    const safe = safeHref(href);
    return safe ? (
      <a href={safe} target="_blank" rel="noopener noreferrer"
         style={{ color: "var(--forest)", textDecoration: "underline" }} {...props}>
        {children}
      </a>
    ) : (
      <span {...props}>{children}</span>
    );
  },
  h1: ({ children }) => <h1 className="text-base font-semibold mt-3 mb-1">{children}</h1>,
  h2: ({ children }) => <h2 className="text-sm font-semibold mt-3 mb-1">{children}</h2>,
  h3: ({ children }) => <h3 className="text-sm font-semibold mt-2 mb-1">{children}</h3>,
  p: ({ children }) => <p className="text-sm my-1 leading-relaxed">{children}</p>,
  ul: ({ children }) => <ul className="list-disc pl-5 text-sm my-1">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal pl-5 text-sm my-1">{children}</ol>,
  li: ({ children }) => <li className="my-0.5">{children}</li>,
  code: ({ className, children }) => {
    const isBlock = typeof className === "string" && className.includes("language-");
    return isBlock ? (
      <code className="font-mono">{children}</code>
    ) : (
      <code className="rounded px-1 py-0.5 text-xs font-mono"
            style={{ background: "var(--edge)" }}>{children}</code>
    );
  },
  pre: ({ children }) => (
    <pre className="rounded p-2 text-xs font-mono overflow-x-auto my-2"
         style={{ background: "var(--edge)" }}>{children}</pre>
  ),
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 pl-3 my-2 text-sm"
                style={{ borderColor: "var(--edge)", color: "var(--mute)" }}>{children}</blockquote>
  ),
};

/** Shared read-only markdown renderer (gfm, no raw HTML, link-guarded, CSS-var themed). */
export function MarkdownView({ children }: { children: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
      {children}
    </ReactMarkdown>
  );
}
```

- [ ] **Step 2: Refactor `DocumentRow.tsx`** — remove the now-duplicated `ReactMarkdown`/`remarkGfm`/`Components` imports, the `safeHref` function, and the `markdownComponents` const. Add `import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";`. Replace the body-render block:
```tsx
          {doc.body && doc.body.trim() ? (
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
              {doc.body}
            </ReactMarkdown>
          ) : (
            <p className="text-xs text-[var(--mute)] pt-2">(empty document)</p>
          )}
```
with:
```tsx
          {doc.body && doc.body.trim() ? (
            <MarkdownView>{doc.body}</MarkdownView>
          ) : (
            <p className="text-xs text-[var(--mute)] pt-2">(empty document)</p>
          )}
```
Keep everything else in `DocumentRow` (the `useState` expand/collapse, the header button, the container div, the `lucide-react`/`timeAgo`/types imports) unchanged.

- [ ] **Step 3: Create `MarkdownView.test.tsx`:**
```tsx
// apps/frontend/src/__tests__/teams/MarkdownView.test.tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";

describe("MarkdownView", () => {
  it("renders gfm markdown (heading + list)", () => {
    render(<MarkdownView>{"## Section\n\n- first item\n- second item"}</MarkdownView>);
    expect(screen.getByText("Section")).toBeInTheDocument();
    expect(screen.getByText("first item")).toBeInTheDocument();
    expect(screen.getByText("second item")).toBeInTheDocument();
  });

  it("neutralizes a javascript: link (no live link rendered)", () => {
    render(<MarkdownView>{"[click me](javascript:alert(1))"}</MarkdownView>);
    expect(screen.getByText("click me")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("renders a safe https link as a link", () => {
    render(<MarkdownView>{"[docs](https://example.com)"}</MarkdownView>);
    const link = screen.getByRole("link", { name: "docs" });
    expect(link).toHaveAttribute("href", "https://example.com");
  });
});
```

- [ ] **Step 4: Run.** `pnpm test -- MarkdownView IssueDocumentsSection` — MarkdownView's 3 tests pass AND the existing IssueDocumentsSection tests stay green (the extraction is behavior-preserving). `pnpm exec tsc --noEmit` clean; `pnpm exec eslint src/components/teams/shared/components/MarkdownView.tsx src/components/teams/issues/DocumentRow.tsx` clean.

- [ ] **Step 5: Commit** — `git commit -m "refactor(teams): extract shared MarkdownView from DocumentRow"`

---

## Task 2: Richer `IssueComments` (markdown body + author names + relative time)

**Files:** Modify `apps/frontend/src/components/teams/issues/IssueComments.tsx`, `apps/frontend/src/components/teams/issues/IssueDetailPage.tsx`, `apps/frontend/src/__tests__/components/teams/issues/IssueComments.test.tsx`.

- [ ] **Step 1: Update `IssueComments.tsx`.**
  - Update imports: add `import { timeAgo } from "@/components/teams/shared/lib/timeAgo";`, `import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";`, and add `CompanyAgent` to the types import: `import type { CompanyAgent, IssueComment } from "@/components/teams/shared/types";`.
  - Add `agents` to the props interface:
```tsx
export interface IssueCommentsProps {
  comments: IssueComment[];
  agents: CompanyAgent[];
  isLoading?: boolean;
  onSubmit: (body: string) => Promise<void>;
}
```
  - Change `authorLabel` to resolve agent names:
```tsx
function authorLabel(c: IssueComment, agents: CompanyAgent[]): string {
  if (c.authorAgentId) return agents.find((a) => a.id === c.authorAgentId)?.name ?? "Agent";
  if (c.authorKind === "agent") return "Agent";
  if (c.authorUserId || c.authorKind === "user") return "User";
  return "Comment";
}
```
  - Destructure `agents` in the component signature: `export function IssueComments({ comments, agents, isLoading, onSubmit }: IssueCommentsProps) {`.
  - In the comment `<li>`: change `<span>{authorLabel(c)}</span>` to `<span>{authorLabel(c, agents)}</span>`.
  - Change the timestamp `<time>` to relative time with an absolute tooltip:
```tsx
                <time dateTime={c.createdAt} title={new Date(c.createdAt).toLocaleString()}>
                  {timeAgo(c.createdAt)}
                </time>
```
  - Replace the plain body div:
```tsx
              <div
                className="text-sm whitespace-pre-wrap"
                style={{ color: "var(--ink-soft)" }}
              >
                {c.body}
              </div>
```
with a markdown-rendered body (drop `whitespace-pre-wrap` — markdown handles structure):
```tsx
              <div className="text-sm" style={{ color: "var(--ink-soft)" }}>
                <MarkdownView>{c.body}</MarkdownView>
              </div>
```

- [ ] **Step 2: Pass `agents` from `IssueDetailPage.tsx`** — in the `<IssueComments … />` mount (around line 132-138), add the `agents={agents}` prop:
```tsx
          <IssueComments
            comments={comments}
            agents={agents}
            isLoading={isLoading}
            onSubmit={async (body) => {
              await mutations.addComment(issueId, body);
            }}
          />
```

- [ ] **Step 3: Update `IssueComments.test.tsx`.**
  - `agents` is now required. Add `agents={[]}` to every existing `render(<IssueComments … />)` call (there are ~10). The existing "renders list of comments with body" test still passes because plain-text bodies render inside markdown `<p>` elements (`getByText("First")` still matches).
  - Add three new tests inside the `describe` block:
```tsx
  test("renders comment body as markdown", () => {
    const c = baseComment({ id: "cm", body: "## Heading text\n\nbody para" });
    render(<IssueComments comments={[c]} agents={[]} onSubmit={vi.fn()} />);
    expect(screen.getByText("Heading text")).toBeInTheDocument(); // rendered as a heading, not literal "##"
    expect(screen.queryByText("## Heading text")).toBeNull();
  });

  test("resolves agent author name from the agents prop", () => {
    const c = baseComment({ id: "ca", body: "hi", authorAgentId: "ag1", authorKind: "agent" });
    render(
      <IssueComments
        comments={[c]}
        agents={[{ id: "ag1", name: "Atlas" } as never]}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.getByText("Atlas")).toBeInTheDocument();
    expect(screen.queryByText("Agent")).toBeNull();
  });

  test("shows a relative timestamp for a past comment", () => {
    const c = baseComment({ id: "ct", body: "hi", createdAt: "2020-01-01T00:00:00Z" });
    render(<IssueComments comments={[c]} agents={[]} onSubmit={vi.fn()} />);
    expect(screen.getByText(/ago/i)).toBeInTheDocument();
  });
```

- [ ] **Step 4: Run.** `pnpm test -- IssueComments` (all existing + 3 new pass). Then `pnpm test -- IssueDetailPage` (still green; the added `agents` prop is already in scope there). `pnpm exec tsc --noEmit` clean; `pnpm exec eslint src/components/teams/issues/IssueComments.tsx` clean.

- [ ] **Step 5: Commit** — `git commit -m "feat(teams): issue comments render markdown + agent names + relative time (D5)"`

---

## Task 3: Full verification + roadmap

- [ ] **Step 1:** `pnpm exec tsc --noEmit` (clean).
- [ ] **Step 2:** `pnpm exec eslint src/components/teams/shared/components/MarkdownView.tsx src/components/teams/issues/IssueComments.tsx src/components/teams/issues/DocumentRow.tsx` (0 errors).
- [ ] **Step 3:** `pnpm exec vitest run` — D5 tests pass; only the pre-existing local-env-only files may fail (BotSetupWizard, useChatGPTOAuth, CreditsPanel, ChatGPTOAuthStep). Confirm no regression in `IssueComments`, `IssueDocumentsSection`, `MarkdownView`, `IssueDetailPage`. (vitest may hang at teardown — read per-file results.)
- [ ] **Step 4:** `pnpm run build` — "✓ Compiled successfully". (A later Clerk-prerender failure on `/admin/health`/`/_not-found`/marketing pages is the known env-only issue, not a code error.)
- [ ] **Step 5:** Roadmap: mark D4 done (#765) and D5 in/after this PR under row D. Commit.

---

## Self-Review
- Spec coverage: shared `MarkdownView` extraction + DocumentRow refactor (T1), comment markdown body + author-name resolution + relative time + `agents` prop wiring + mount (T2), verification + roadmap (T3). Mentions/edit/delete/reactions/votes/reopen-reassign deferred (correct).
- Type consistency: `MarkdownView({children}: {children: string})` used identically in DocumentRow + IssueComments; `authorLabel(c, agents)`; `agents: CompanyAgent[]` required on `IssueCommentsProps` and passed from IssueDetailPage.
- Safety: extraction preserves no-`rehype-raw` + link guard; MarkdownView test asserts `javascript:` is neutralized and `https:` is preserved.
- Behavior preservation: DocumentRow renders identically post-extraction (IssueDocumentsSection tests unchanged + green).
- Placeholder scan: none.
