# Teams Issue Documents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** A read-only "Documents" section on the `/teams` issue-detail page — the issue's documents, each expandable to show its rendered-markdown body.

**Architecture:** Thin issue-scoped BFF passthrough (`GET /issues/{id}/documents`, body inline) + a new `IssueDocumentsSection` (+ `DocumentRow`) mounted in `IssueDetailPage`, rendering markdown via `react-markdown` + `remark-gfm` (no raw HTML; link-guarded; CSS-var themed).

**Spec:** `docs/superpowers/specs/2026-05-29-teams-issue-documents-design.md`

Conventions: backend from `apps/backend` (`uv run pytest <path> -q`); frontend from `apps/frontend` (`pnpm test -- <substr>`). Per-task targeted tests; full suite/tsc/build in the final task. SWR key for `read("/x")` is `/teams/x`.

---

## Task 1: BFF GET /issues/{id}/documents

**Files:** Modify `apps/backend/core/services/paperclip_admin_client.py`, `apps/backend/routers/teams/issues.py`; Test `apps/backend/tests/unit/routers/teams/test_issue_documents.py` (new).

- [ ] **Step 1: Failing test** — `tests/unit/routers/teams/test_issue_documents.py`:

```python
# apps/backend/tests/unit/routers/teams/test_issue_documents.py
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi.testclient import TestClient
from main import app
from routers.teams.deps import TeamsContext


@pytest.fixture
def teams_ctx():
    return TeamsContext(user_id="u1", org_id="o1", owner_id="o1", company_id="co_abc",
                        paperclip_user_id="pcu", session_cookie="cookie")


@pytest.fixture
def client(teams_ctx):
    from routers.teams import agents as agents_mod

    async def fake_ctx():
        return teams_ctx

    app.dependency_overrides[agents_mod._ctx] = fake_ctx
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(agents_mod._ctx, None)


def test_list_issue_documents_forwards_and_envelopes(client, monkeypatch):
    admin = MagicMock()
    admin.list_issue_documents = AsyncMock(
        return_value=[{"id": "d1", "key": "plan"}, {"id": "d2", "key": "report"}]
    )
    from routers.teams import agents as agents_mod
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/issues/iss1/documents", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {"documents": [{"id": "d1", "key": "plan"}, {"id": "d2", "key": "report"}]}
    admin.list_issue_documents.assert_awaited_once_with(issue_id="iss1", session_cookie="cookie")
```

- [ ] **Step 2: Run → fail.** `uv run pytest tests/unit/routers/teams/test_issue_documents.py -q`

- [ ] **Step 3: admin method** — add to `paperclip_admin_client.py` after `list_issue_runs` (mirror its `_get` style exactly):
```python
    async def list_issue_documents(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> list | dict:
        """List the documents attached to an issue.

        Maps to ``GET /api/issues/{issueId}/documents`` (issue-scoped).
        The body is returned inline per document, so no fetch-by-key is needed.
        """
        return await self._get(
            f"/api/issues/{issue_id}/documents",
            session_cookie=session_cookie,
        )
```

- [ ] **Step 4: BFF route** — append to `routers/teams/issues.py`, next to `list_issue_runs`:
```python
@router.get("/issues/{issue_id}/documents")
async def list_issue_documents(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List the documents attached to an issue (the issue-detail Documents section)."""
    rows = await _agents._admin().list_issue_documents(
        session_cookie=ctx.session_cookie,
        issue_id=issue_id,
    )
    return envelope_list(rows, "documents")
```

- [ ] **Step 5: Run → pass.** `uv run pytest tests/unit/routers/teams/test_issue_documents.py -q`. Ensure ruff clean (`uv run ruff check core/services/paperclip_admin_client.py routers/teams/issues.py`).

- [ ] **Step 6: Commit** — `git commit -m "feat(teams): BFF GET /issues/{id}/documents (per-issue document list)"`

---

## Task 2: IssueDocument type + DocumentRow + IssueDocumentsSection + mount

**Files:** Modify `apps/frontend/src/components/teams/shared/types.ts`; Create `apps/frontend/src/components/teams/issues/DocumentRow.tsx` + `apps/frontend/src/components/teams/issues/IssueDocumentsSection.tsx`; Modify `apps/frontend/src/components/teams/issues/IssueDetailPage.tsx`; Test `apps/frontend/src/__tests__/teams/IssueDocumentsSection.test.tsx` (new); keep `IssueDetailPage.test.tsx` green.

- [ ] **Step 1: Add the `IssueDocument` type** to `shared/types.ts` (near `IssueRun`):
```ts
/** A document attached to an issue (upstream IssueDocument subset). `body` is inline markdown. */
export interface IssueDocument {
  id: string;
  key: string;
  title?: string | null;
  format?: string | null;
  body?: string | null;
  updatedByAgentId?: string | null;
  updatedByUserId?: string | null;
  updatedAt?: string | null;
  createdAt?: string | null;
}
```

- [ ] **Step 2: Failing test** — `IssueDocumentsSection.test.tsx`:
```tsx
// apps/frontend/src/__tests__/teams/IssueDocumentsSection.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { IssueDocumentsSection } from "@/components/teams/issues/IssueDocumentsSection";

function doc(id: string, key: string, body: string, updatedAt: string, title: string | null = null) {
  return { id, key, body, title, updatedAt, createdAt: updatedAt };
}

beforeEach(() => read.mockReset());

describe("IssueDocumentsSection", () => {
  it("renders nothing when there are no documents", () => {
    read.mockReturnValue({ data: { documents: [] }, isLoading: false, error: null, mutate: vi.fn() });
    const { container } = render(<IssueDocumentsSection issueId="iss1" agents={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders documents newest-first with key badge + title + updated time, collapsed by default", () => {
    read.mockReturnValue({
      data: { documents: [
        doc("d-old", "report", "# Old body heading", "2026-05-01T00:00:00Z", "Report"),
        doc("d-new", "plan", "## New body heading", "2026-05-03T00:00:00Z"),
      ] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<IssueDocumentsSection issueId="iss1" agents={[]} />);
    expect(screen.getByText(/Documents/)).toBeInTheDocument();
    // key badges present (newest-first order: plan before report)
    const toggles = screen.getAllByRole("button");
    expect(toggles[0]).toHaveTextContent(/plan/i);
    expect(toggles[1]).toHaveTextContent(/report/i);
    // title shown when present and != key
    expect(screen.getByText("Report")).toBeInTheDocument();
    // collapsed by default → body text NOT in the DOM
    expect(screen.queryByText("New body heading")).toBeNull();
    expect(screen.queryByText("Old body heading")).toBeNull();
  });

  it("expands a document to render its markdown body", () => {
    read.mockReturnValue({
      data: { documents: [doc("d1", "plan", "## Hello section", "2026-05-01T00:00:00Z")] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<IssueDocumentsSection issueId="iss1" agents={[]} />);
    expect(screen.queryByText("Hello section")).toBeNull();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText("Hello section")).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run → fail.** `pnpm test -- IssueDocumentsSection`

- [ ] **Step 4: Implement `DocumentRow.tsx`:**
```tsx
// apps/frontend/src/components/teams/issues/DocumentRow.tsx
"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";
import { ChevronRight, ChevronDown } from "lucide-react";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { CompanyAgent, IssueDocument } from "@/components/teams/shared/types";

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
  code: ({ children }) => (
    <code className="rounded px-1 py-0.5 text-xs font-mono"
          style={{ background: "var(--edge)" }}>{children}</code>
  ),
  pre: ({ children }) => (
    <pre className="rounded p-2 text-xs font-mono overflow-x-auto my-2"
         style={{ background: "var(--edge)" }}>{children}</pre>
  ),
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 pl-3 my-2 text-sm"
                style={{ borderColor: "var(--edge)", color: "var(--mute)" }}>{children}</blockquote>
  ),
};

export function DocumentRow({ doc, agents }: { doc: IssueDocument; agents: CompanyAgent[] }) {
  const [open, setOpen] = useState(false);
  const when = doc.updatedAt ?? doc.createdAt;
  const titleShown =
    doc.title && doc.title.trim() && doc.title.trim().toLowerCase() !== doc.key.toLowerCase()
      ? doc.title.trim()
      : null;
  const agentName = doc.updatedByAgentId
    ? agents.find((a) => a.id === doc.updatedByAgentId)?.name ?? null
    : null;
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <li className="flex flex-col" style={{ border: "1px solid var(--edge)", borderRadius: 6 }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex items-center gap-2 px-3 py-2 text-left"
      >
        <Chevron size={14} className="flex-shrink-0" style={{ color: "var(--mute)" }} />
        <span
          className="text-[10px] uppercase tracking-wide font-mono px-1.5 py-0.5 rounded flex-shrink-0"
          style={{ background: "var(--edge)", color: "var(--ink)" }}
        >
          {doc.key}
        </span>
        {titleShown ? (
          <span className="flex-1 truncate text-[var(--ink)]">{titleShown}</span>
        ) : (
          <span className="flex-1" />
        )}
        {when && (
          <span className="text-xs text-[var(--mute)] flex-shrink-0">
            updated {timeAgo(when)}
            {agentName ? ` · ${agentName}` : ""}
          </span>
        )}
      </button>
      {open && (
        <div className="px-3 pb-3 pt-1 max-w-none text-[var(--ink)]"
             style={{ borderTop: "1px solid var(--edge)" }}>
          {doc.body && doc.body.trim() ? (
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
              {doc.body}
            </ReactMarkdown>
          ) : (
            <p className="text-xs text-[var(--mute)] pt-2">(empty document)</p>
          )}
        </div>
      )}
    </li>
  );
}
```

- [ ] **Step 5: Implement `IssueDocumentsSection.tsx`:**
```tsx
// apps/frontend/src/components/teams/issues/IssueDocumentsSection.tsx
"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { CompanyAgent, IssueDocument } from "@/components/teams/shared/types";
import { DocumentRow } from "./DocumentRow";

function docTime(d: IssueDocument): number {
  const v = d.updatedAt ?? d.createdAt ?? null;
  const t = v ? new Date(v).getTime() : 0;
  return Number.isNaN(t) ? 0 : t;
}

export function IssueDocumentsSection({
  issueId,
  agents,
}: {
  issueId: string;
  agents: CompanyAgent[];
}) {
  const { read } = useTeamsApi();
  const { data } = read<{ documents: IssueDocument[] }>(`/issues/${issueId}/documents`);
  const documents = [...(data?.documents ?? [])].sort((a, b) => docTime(b) - docTime(a)); // newest first
  if (documents.length === 0) return null;

  return (
    <section className="flex flex-col gap-3 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
      <h2 className="section-label">
        <b>Documents</b>
      </h2>
      <ul className="flex flex-col gap-2">
        {documents.map((d) => (
          <DocumentRow key={d.id} doc={d} agents={agents} />
        ))}
      </ul>
    </section>
  );
}
```

- [ ] **Step 6: Mount in `IssueDetailPage.tsx`** — add `import { IssueDocumentsSection } from "./IssueDocumentsSection";` to the import block, and render `<IssueDocumentsSection issueId={issueId} agents={agents} />` immediately AFTER the existing `<IssueRunsSection issueId={issueId} agents={agents} />` line and BEFORE the Conversation `<div className="pt-4" ...>` block.

- [ ] **Step 7: Keep `IssueDetailPage.test.tsx` green** — the page now also calls `read("/issues/{id}/documents")`. Its `read` mock has a benign default; `IssueDocumentsSection` reads `.documents` → `undefined ?? []` → renders nothing. Run `pnpm test -- IssueDetailPage`; adjust the mock default only if it throws (don't weaken assertions).

- [ ] **Step 8: Run → pass.** `pnpm test -- IssueDocumentsSection IssueDetailPage`

- [ ] **Step 9: Commit** — `git commit -m "feat(teams): IssueDocumentsSection (per-issue documents, rendered markdown) on issue detail"`

---

## Task 3: Full verification + roadmap

- [ ] **Step 1:** Backend — `uv run pytest tests/unit/routers/teams/ -q`.
- [ ] **Step 2:** `pnpm exec tsc --noEmit` (clean).
- [ ] **Step 3:** `pnpm exec eslint src/components/teams/issues/IssueDocumentsSection.tsx src/components/teams/issues/DocumentRow.tsx` (0 errors).
- [ ] **Step 4:** `pnpm exec vitest run` — D4 tests pass; only the pre-existing local-env-only files may fail (BotSetupWizard, useChatGPTOAuth, CreditsPanel, ChatGPTOAuthStep). (vitest may hang at teardown — read per-file results.)
- [ ] **Step 5:** `pnpm run build` — "✓ Compiled successfully".
- [ ] **Step 6:** Roadmap: note D4 alongside D1/D2/D3 under row D. Commit.

---

## Self-Review
- Spec coverage: list endpoint inline-body (T1), section newest-first + key badge + title + updated + author + hide-when-empty + collapse-by-default + expand-renders-markdown + mount (T2), verification + roadmap (T3). Editor/revisions/locks/annotations/voting/download/system-toggle deferred (correct).
- Type consistency: `list_issue_documents(issue_id, session_cookie)` (T1) used identically in admin + route + test; `IssueDocumentsSection({issueId, agents})` matches mount (T2); `DocumentRow({doc, agents})`; `IssueDocument` shape consistent across type + section + row + test.
- Safety: no `rehype-raw` → raw HTML inert; link hrefs guarded (`javascript:`/`data:`).
- Placeholder scan: none.
