# Teams Issue Sub-issues Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Sub-issues section to the `/teams` issue-detail page — list an issue's descendants, create a sub-issue, and link up to the parent.

**Architecture:** Thin BFF passthrough to upstream Paperclip (`descendantOf` list filter + `POST /issues/:id/children`), reusing our existing SWR-native components (`IssueRow`, `NewIssueDialog`) and porting two pure helpers (a created-order sort + a progress summary) from upstream. Frontend renders a new `SubIssuesSection` in `IssueDetailPage` plus a parent breadcrumb.

**Tech Stack:** FastAPI BFF (Python/uv), Next.js 16 + React 19 + SWR + TypeScript + Tailwind v4, Vitest. Backend changes are a small passthrough; the rest is frontend.

**Spec:** `docs/superpowers/specs/2026-05-29-teams-issue-subissues-design.md`

---

## Conventions for every task

- Backend: run from `apps/backend`; tests `uv run pytest <path> -q`. Frontend: run from `apps/frontend`; a single test file is `pnpm test -- <substr>` (maps to `vitest run`).
- Per-task: run only that task's targeted test. The full suite + `tsc` + `eslint` + `build` are the final task.
- New frontend `.ts/.tsx` files start with a `// apps/frontend/<relpath>` first-line comment.
- SWR key: `useTeamsApi().read("/x")` registers key `/teams/x` (the hook prepends `/teams`).
- These issue endpoints take no service token, so backend tests need NO `PAPERCLIP_SERVICE_TOKEN_KEY`.

## File Structure

**Backend:**
- Modify `apps/backend/core/services/paperclip_admin_client.py` — add `create_child_issue`.
- Modify `apps/backend/routers/teams/issues.py` — add `GET` + `POST /issues/{issue_id}/children`.
- Test `apps/backend/tests/unit/routers/teams/test_issue_children.py` (new).

**Frontend:**
- Create `apps/frontend/src/components/teams/shared/lib/subIssues.ts` — pure sort + progress summary.
- Modify `apps/frontend/src/components/teams/issues/hooks/useIssueMutations.ts` — add `createChild`.
- Modify `apps/frontend/src/components/teams/issues/NewIssueDialog.tsx` — optional `parentId`/`parentLabel`.
- Create `apps/frontend/src/components/teams/issues/SubIssuesSection.tsx`.
- Create `apps/frontend/src/components/teams/issues/IssueParentBreadcrumb.tsx`.
- Modify `apps/frontend/src/components/teams/issues/IssueDetailPage.tsx` — mount section + breadcrumb.
- Tests under `apps/frontend/src/__tests__/teams/` + extend the existing `IssueDetailPage.test.tsx`.

---

## Task 1: BFF children endpoints (list descendants + create child)

**Files:**
- Modify: `apps/backend/core/services/paperclip_admin_client.py`
- Modify: `apps/backend/routers/teams/issues.py`
- Test: `apps/backend/tests/unit/routers/teams/test_issue_children.py`

- [ ] **Step 1: Write the failing tests**

Create `apps/backend/tests/unit/routers/teams/test_issue_children.py`:

```python
# apps/backend/tests/unit/routers/teams/test_issue_children.py
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from routers.teams.deps import TeamsContext


@pytest.fixture
def teams_ctx():
    return TeamsContext(
        user_id="u1", org_id="o1", owner_id="o1", company_id="co_abc",
        paperclip_user_id="pcu", session_cookie="cookie",
    )


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


def test_list_children_forwards_descendantOf_and_envelopes(client, monkeypatch):
    admin = MagicMock()
    admin.list_issues = AsyncMock(return_value=[{"id": "c1"}, {"id": "c2"}])
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/issues/iss1/children", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {"issues": [{"id": "c1"}, {"id": "c2"}]}
    admin.list_issues.assert_awaited_once_with(
        company_id="co_abc", session_cookie="cookie", params={"descendantOf": "iss1"}
    )


def test_create_child_whitelists_and_calls_admin(client, monkeypatch):
    admin = MagicMock()
    admin.create_child_issue = AsyncMock(return_value={"id": "child1", "title": "Sub"})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.post(
        "/api/v1/teams/issues/iss1/children",
        json={"title": "Sub", "assigneeAgentId": "ag1"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json()["id"] == "child1"
    call = admin.create_child_issue.await_args
    assert call.kwargs["company_id"] == "co_abc"
    assert call.kwargs["issue_id"] == "iss1"
    assert call.kwargs["body"] == {"title": "Sub", "assigneeAgentId": "ag1"}


def test_create_child_rejects_extra_keys(client, monkeypatch):
    admin = MagicMock()
    admin.create_child_issue = AsyncMock(return_value={})
    from routers.teams import agents as agents_mod

    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.post(
        "/api/v1/teams/issues/iss1/children",
        json={"title": "Sub", "adapterType": "x"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 422
    admin.create_child_issue.assert_not_awaited()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/routers/teams/test_issue_children.py -q`
Expected: FAIL — routes 404 / `create_child_issue` attribute missing.

- [ ] **Step 3: Add the admin-client method** — in `apps/backend/core/services/paperclip_admin_client.py`, after `create_issue`:

```python
    async def create_child_issue(
        self,
        *,
        session_cookie: str,
        company_id: str,
        issue_id: str,
        body: dict,
    ) -> dict:
        """Create a child (sub-)issue under ``issue_id``. Body whitelisted by the BFF.

        Maps to ``POST /api/companies/{companyId}/issues/{issueId}/children``.
        """
        return await self._post(
            f"/api/companies/{company_id}/issues/{issue_id}/children",
            json=body,
            session_cookie=session_cookie,
        )
```

- [ ] **Step 4: Add the BFF routes** — in `apps/backend/routers/teams/issues.py`, append at the end of the file (after the comments routes). Reuse the existing `CreateIssueBody` import (same whitelist):

```python
@router.get("/issues/{issue_id}/children")
async def list_issue_children(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List an issue's descendants (the Sub-issues section).

    Forwards upstream's ``descendantOf`` filter on the issues-list route —
    matches the upstream IssueDetail sub-issues section (all descendants,
    flat). Wrapped ``{issues: [...]}`` like the other list surfaces.
    """
    rows = await _agents._admin().list_issues(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
        params={"descendantOf": issue_id},
    )
    return envelope_list(rows, "issues")


@router.post("/issues/{issue_id}/children")
async def create_issue_child(
    issue_id: str,
    body: CreateIssueBody,
    ctx: TeamsContext = Depends(_ctx),
):
    """Create a sub-issue under ``issue_id``.

    Reuses ``CreateIssueBody`` (the same whitelist as top-level create);
    routes to upstream's ``/children`` endpoint, which wires the
    parent/child relation server-side.
    """
    return await _agents._admin().create_child_issue(
        company_id=ctx.company_id,
        issue_id=issue_id,
        body=body.model_dump(exclude_none=True, by_alias=True),
        session_cookie=ctx.session_cookie,
    )
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/unit/routers/teams/test_issue_children.py -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/paperclip_admin_client.py apps/backend/routers/teams/issues.py apps/backend/tests/unit/routers/teams/test_issue_children.py
git commit -m "feat(teams): BFF endpoints for issue children (list descendants + create)"
```

---

## Task 2: subIssues pure lib (sort + progress summary)

**Files:**
- Create: `apps/frontend/src/components/teams/shared/lib/subIssues.ts`
- Test: `apps/frontend/src/__tests__/teams/subIssues.test.ts`

Ports upstream's `buildSubIssueProgressSummary` (`paperclip/ui/src/lib/issue-detail-subissues.ts`) adapted to our `IssueStatus`. The sort is the `blockedBy`-less reduction of upstream `workflowSort` (created-order; full topo-sort can drop in when `blockedBy` lands — deferred). Our `IssueStatus` is richer than upstream's, so the terminal-status set includes `won_t_do`/`duplicate`/`closed`.

- [ ] **Step 1: Write the failing test**

```ts
// apps/frontend/src/__tests__/teams/subIssues.test.ts
import { describe, it, expect } from "vitest";
import { sortSubIssues, buildSubIssueProgressSummary } from "@/components/teams/shared/lib/subIssues";
import type { Issue } from "@/components/teams/shared/types";

function mk(id: string, status: Issue["status"], createdAt: string): Issue {
  return { id, title: id, status, createdAt } as Issue;
}

describe("sortSubIssues", () => {
  it("orders by createdAt ascending, then id", () => {
    const out = sortSubIssues([
      mk("b", "todo", "2026-05-02T00:00:00Z"),
      mk("a", "todo", "2026-05-01T00:00:00Z"),
      mk("c", "todo", "2026-05-01T00:00:00Z"),
    ]);
    expect(out.map((i) => i.id)).toEqual(["a", "c", "b"]);
  });
});

describe("buildSubIssueProgressSummary", () => {
  it("counts by bucket, ignores cancelled, and picks the first actionable as next", () => {
    const s = buildSubIssueProgressSummary([
      mk("a", "done", "2026-05-01T00:00:00Z"),
      mk("b", "in_progress", "2026-05-02T00:00:00Z"),
      mk("c", "todo", "2026-05-03T00:00:00Z"),
      mk("d", "cancelled", "2026-05-04T00:00:00Z"),
    ]);
    expect(s.totalCount).toBe(3); // cancelled excluded
    expect(s.doneCount).toBe(1);
    expect(s.inProgressCount).toBe(1);
    expect(s.target?.kind).toBe("next");
    expect(s.target?.issue.id).toBe("b"); // earliest actionable by sort
  });

  it("returns a blocked target when nothing is actionable but blocked remain", () => {
    const s = buildSubIssueProgressSummary([
      mk("a", "done", "2026-05-01T00:00:00Z"),
      mk("b", "blocked", "2026-05-02T00:00:00Z"),
    ]);
    expect(s.target?.kind).toBe("blocked");
    expect(s.target?.issue.id).toBe("b");
    expect(s.blockedCount).toBe(1);
  });

  it("treats won_t_do/duplicate/closed as terminal (no target)", () => {
    const s = buildSubIssueProgressSummary([
      mk("a", "done", "2026-05-01T00:00:00Z"),
      mk("b", "won_t_do", "2026-05-02T00:00:00Z"),
    ]);
    expect(s.target).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- subIssues`
Expected: FAIL — cannot find module.

- [ ] **Step 3: Implement the lib**

```ts
// apps/frontend/src/components/teams/shared/lib/subIssues.ts
// Progress summary ported from upstream Paperclip's issue-detail-subissues.ts
// (paperclip/ui/src/lib/issue-detail-subissues.ts) (MIT, (c) 2025 Paperclip AI),
// adapted to our richer IssueStatus. Sort is the blockedBy-less reduction of
// upstream workflowSort (created-order); swap in the topo-sort when blockedBy
// (includeBlockedBy) lands.

import type { Issue, IssueStatus } from "@/components/teams/shared/types";

export type SubIssueProgressTargetKind = "next" | "blocked";
export interface SubIssueProgressTarget {
  issue: Issue;
  kind: SubIssueProgressTargetKind;
}
export interface SubIssueProgressSummary {
  totalCount: number;
  doneCount: number;
  inProgressCount: number;
  blockedCount: number;
  target: SubIssueProgressTarget | null;
}

// Our IssueStatus is broader than upstream's; treat all clearly-finished
// states as terminal so they're never picked as the "next" actionable target.
const TERMINAL_STATUSES = new Set<IssueStatus>([
  "done",
  "cancelled",
  "won_t_do",
  "duplicate",
  "closed",
]);

function isTerminalStatus(status: IssueStatus): boolean {
  return TERMINAL_STATUSES.has(status);
}
function isActionableStatus(status: IssueStatus): boolean {
  return !isTerminalStatus(status) && status !== "blocked";
}

function toTime(value: string | undefined): number {
  const t = value ? new Date(value).getTime() : 0;
  return Number.isNaN(t) ? 0 : t;
}

/** createdAt ascending, then id — the blockedBy-less reduction of workflowSort. */
export function sortSubIssues(issues: Issue[]): Issue[] {
  return [...issues].sort((a, b) => {
    const ta = toTime(a.createdAt ?? undefined);
    const tb = toTime(b.createdAt ?? undefined);
    if (ta !== tb) return ta - tb;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
}

export function buildSubIssueProgressSummary(issues: Issue[]): SubIssueProgressSummary {
  const progress = issues.filter((i) => i.status !== "cancelled");
  const ordered = sortSubIssues(progress);
  const count = (status: IssueStatus) => progress.filter((i) => i.status === status).length;

  const next = ordered.find((i) => isActionableStatus(i.status)) ?? null;
  const remaining = ordered.filter((i) => !isTerminalStatus(i.status));
  const blocked =
    next === null && remaining.length > 0 && remaining.every((i) => i.status === "blocked")
      ? remaining[0]
      : null;

  return {
    totalCount: progress.length,
    doneCount: count("done"),
    inProgressCount: count("in_progress"),
    blockedCount: count("blocked"),
    target: next ? { issue: next, kind: "next" } : blocked ? { issue: blocked, kind: "blocked" } : null,
  };
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pnpm test -- subIssues`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/shared/lib/subIssues.ts src/__tests__/teams/subIssues.test.ts
git commit -m "feat(teams): subIssues lib — created-order sort + progress summary"
```

---

## Task 3: useIssueMutations.createChild

**Files:**
- Modify: `apps/frontend/src/components/teams/issues/hooks/useIssueMutations.ts`
- Test: `apps/frontend/src/__tests__/teams/useIssueMutations.createChild.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/teams/useIssueMutations.createChild.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

const post = vi.fn();
const patch = vi.fn();
const swrMutate = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ post, patch }) }));
vi.mock("swr", async (orig) => ({ ...(await orig<object>()), useSWRConfig: () => ({ mutate: swrMutate }) }));

import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";

beforeEach(() => { post.mockReset().mockResolvedValue({ id: "child1" }); swrMutate.mockReset(); });

describe("useIssueMutations.createChild", () => {
  it("POSTs the children endpoint and revalidates the children key", async () => {
    const { result } = renderHook(() => useIssueMutations());
    let created;
    await act(async () => { created = await result.current.createChild("parent1", { title: "Sub" }); });
    expect(post).toHaveBeenCalledWith("/issues/parent1/children", { title: "Sub" });
    expect(created).toEqual({ id: "child1" });
    expect(swrMutate).toHaveBeenCalledWith("/teams/issues/parent1/children");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- useIssueMutations.createChild`
Expected: FAIL — `createChild` is not a function.

- [ ] **Step 3: Implement** — in `useIssueMutations.ts`, add `createChild` to the interface and body, and return it.

Add to `UseIssueMutationsResult`:
```ts
  createChild: (parentId: string, input: IssueCreateInput) => Promise<Issue>;
```

Add inside the hook (after `create`):
```ts
  const createChild = useCallback(
    async (parentId: string, input: IssueCreateInput): Promise<Issue> => {
      // Routes to the upstream children endpoint (parent/child wired server-side),
      // not the plain create. Revalidate the parent's children list so the new
      // sub-issue appears.
      const created = await post<Issue>(`/issues/${parentId}/children`, input);
      mutate(swrKey(`/issues/${parentId}/children`));
      return created;
    },
    [post, mutate],
  );
```

Add `createChild` to the returned object: `return { create, createChild, update, addComment };`

- [ ] **Step 4: Run to verify it passes**

Run: `pnpm test -- useIssueMutations.createChild`
Expected: PASS (1 test). (Also re-run `pnpm test -- useIssueMutations` to confirm the existing mutation tests still pass.)

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/issues/hooks/useIssueMutations.ts src/__tests__/teams/useIssueMutations.createChild.test.tsx
git commit -m "feat(teams): useIssueMutations.createChild for sub-issue creation"
```

---

## Task 4: NewIssueDialog parentId support

**Files:**
- Modify: `apps/frontend/src/components/teams/issues/NewIssueDialog.tsx`
- Test: `apps/frontend/src/__tests__/teams/NewIssueDialog.parent.test.tsx`

When `parentId` is set, the dialog creates via `createChild(parentId, …)` (not `create`), titles itself "New sub-issue", and shows a "Parent: <parentLabel>" line.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/teams/NewIssueDialog.parent.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const create = vi.fn();
const createChild = vi.fn();
vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: () => ({ create, createChild }),
}));

import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";

beforeEach(() => {
  create.mockReset().mockResolvedValue({ id: "x" });
  createChild.mockReset().mockResolvedValue({ id: "child1" });
});

describe("NewIssueDialog with parentId", () => {
  it("creates via createChild and shows the parent label", async () => {
    render(
      <NewIssueDialog open onOpenChange={() => {}} parentId="p1" parentLabel="ISO-1 Ship billing" />,
    );
    expect(screen.getByText(/ISO-1 Ship billing/)).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText(/issue title/i), { target: { value: "Sub task" } });
    fireEvent.click(screen.getByRole("button", { name: /create/i }));
    await waitFor(() => expect(createChild).toHaveBeenCalledWith("p1", expect.objectContaining({ title: "Sub task" })));
    expect(create).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- NewIssueDialog.parent`
Expected: FAIL — parent label not rendered / `create` called instead of `createChild`.

- [ ] **Step 3: Implement** — edit `NewIssueDialog.tsx`:

Extend props:
```ts
export interface NewIssueDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  agents?: CompanyAgent[];
  projects?: IssueProject[];
  /** When set, the dialog creates a sub-issue under this parent. */
  parentId?: string;
  /** Display label for the parent (e.g. "ISO-12 Title"). */
  parentLabel?: string;
  /** Called with the created issue id on success. */
  onCreated?: (issueId: string) => void;
}
```

Destructure `parentId` and `parentLabel`, and pull `createChild` too:
```ts
export function NewIssueDialog({ open, onOpenChange, agents = [], projects = [], parentId, parentLabel, onCreated }: NewIssueDialogProps) {
  const { create, createChild } = useIssueMutations();
```

In `submit`, branch the create call:
```ts
      const payload = {
        title: trimmedTitle,
        description: description.trim() || undefined,
        priority,
        projectId: projectId || undefined,
        assigneeAgentId: assigneeAgentId || undefined,
      };
      const issue = parentId ? await createChild(parentId, payload) : await create(payload);
```

Set the dialog title dynamically: change `title="New issue"` on `<EditorialDialog>` to `title={parentId ? "New sub-issue" : "New issue"}`.

Render the parent line as the first child inside the form `<div className="flex flex-col gap-3">` (before the Title field):
```tsx
        {parentId && (
          <p className="text-sm" style={{ color: "var(--mute)" }}>
            Parent: <span style={{ color: "var(--ink)" }}>{parentLabel ?? parentId}</span>
          </p>
        )}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pnpm test -- NewIssueDialog.parent`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/issues/NewIssueDialog.tsx src/__tests__/teams/NewIssueDialog.parent.test.tsx
git commit -m "feat(teams): NewIssueDialog parentId -> sub-issue create"
```

---

## Task 5: SubIssuesSection component

**Files:**
- Create: `apps/frontend/src/components/teams/issues/SubIssuesSection.tsx`
- Test: `apps/frontend/src/__tests__/teams/SubIssuesSection.test.tsx`

Fetches descendants, renders progress summary (≥2) + a sorted `IssueRow` list, and a "New Sub-issue" button that opens `NewIssueDialog` with the parent prefilled. Empty → just the button.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/teams/SubIssuesSection.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));
// Stub the dialog so we don't pull its mutation tree; assert it opens with parentId.
vi.mock("@/components/teams/issues/NewIssueDialog", () => ({
  NewIssueDialog: ({ open, parentId }: { open: boolean; parentId?: string }) =>
    open ? <div>dialog-open:{parentId}</div> : null,
}));

import { SubIssuesSection } from "@/components/teams/issues/SubIssuesSection";

function child(id: string, status = "todo") {
  return { id, title: `T-${id}`, status, createdAt: "2026-05-01T00:00:00Z" };
}

beforeEach(() => read.mockReset());

describe("SubIssuesSection", () => {
  it("shows just the New Sub-issue button when there are no descendants", () => {
    read.mockReturnValue({ data: { issues: [] }, isLoading: false, error: null, mutate: vi.fn() });
    render(<SubIssuesSection issueId="iss1" agents={[]} projects={[]} />);
    expect(screen.getByRole("button", { name: /new sub-issue/i })).toBeInTheDocument();
    expect(screen.queryByText(/Sub-issues/)).toBeNull();
  });

  it("renders the descendants list + progress summary (>=2)", () => {
    read.mockReturnValue({
      data: { issues: [child("a", "done"), child("b", "in_progress")] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<SubIssuesSection issueId="iss1" agents={[]} projects={[]} />);
    expect(screen.getByText(/Sub-issues/)).toBeInTheDocument();
    expect(screen.getByText(/T-a/)).toBeInTheDocument();
    expect(screen.getByText(/T-b/)).toBeInTheDocument();
    expect(screen.getByText(/1\s*\/\s*2/)).toBeInTheDocument(); // 1 done of 2
  });

  it("opens the dialog with the parent id when New Sub-issue is clicked", () => {
    read.mockReturnValue({ data: { issues: [] }, isLoading: false, error: null, mutate: vi.fn() });
    render(<SubIssuesSection issueId="iss1" agents={[]} projects={[]} />);
    fireEvent.click(screen.getByRole("button", { name: /new sub-issue/i }));
    expect(screen.getByText("dialog-open:iss1")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- SubIssuesSection`
Expected: FAIL — cannot find module.

- [ ] **Step 3: Implement the component**

```tsx
// apps/frontend/src/components/teams/issues/SubIssuesSection.tsx
"use client";

import { useState } from "react";
import { Plus } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { EditorialButton } from "@/components/teams/shell";
import { IssueRow } from "@/components/teams/inbox/IssueRow";
import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";
import { sortSubIssues, buildSubIssueProgressSummary } from "@/components/teams/shared/lib/subIssues";
import type { CompanyAgent, Issue, IssueProject } from "@/components/teams/shared/types";

export function SubIssuesSection({
  issueId,
  agents,
  projects,
}: {
  issueId: string;
  agents: CompanyAgent[];
  projects: IssueProject[];
}) {
  const { read } = useTeamsApi();
  const { data, error } = read<{ issues: Issue[] }>(`/issues/${issueId}/children`);
  const [dialogOpen, setDialogOpen] = useState(false);

  const children = data?.issues ?? [];
  const sorted = sortSubIssues(children);
  const summary = buildSubIssueProgressSummary(children);
  const showProgress = summary.totalCount >= 2;

  const newButton = (
    <EditorialButton variant="secondary" size="sm" onClick={() => setDialogOpen(true)}>
      <Plus className="mr-1.5 h-3.5 w-3.5" />
      New Sub-issue
    </EditorialButton>
  );

  return (
    <section className="flex flex-col gap-3 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
      {children.length > 0 ? (
        <>
          <div className="flex items-center justify-between gap-2">
            <h2 className="section-label"><b>Sub-issues</b></h2>
            {newButton}
          </div>
          {showProgress && (
            <p className="text-xs" style={{ color: "var(--mute)" }}>
              {summary.doneCount} / {summary.totalCount} done
              {summary.inProgressCount > 0 ? ` · ${summary.inProgressCount} in progress` : ""}
              {summary.blockedCount > 0 ? ` · ${summary.blockedCount} blocked` : ""}
              {summary.target ? ` · ${summary.target.kind === "next" ? "Next" : "Blocked"}: ${summary.target.issue.title}` : ""}
            </p>
          )}
          <ul className="flex flex-col">
            {sorted.map((c) => (
              <IssueRow key={c.id} issue={c} />
            ))}
          </ul>
        </>
      ) : (
        // Empty state: just the button (no header), matching the test's
        // "shows just the New Sub-issue button" expectation.
        <div className="flex items-center justify-end gap-2">{newButton}</div>
      )}
      {error && (
        <p className="text-xs" style={{ color: "var(--danger)" }}>Couldn&apos;t load sub-issues.</p>
      )}
      <NewIssueDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        agents={agents}
        projects={projects}
        parentId={issueId}
        onCreated={() => setDialogOpen(false)}
      />
    </section>
  );
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pnpm test -- SubIssuesSection`
Expected: PASS (3 tests). If the `1 / 2` matcher is whitespace-sensitive, confirm the rendered text is `1 / 2 done`.

- [ ] **Step 5: Commit**

```bash
git add src/components/teams/issues/SubIssuesSection.tsx src/__tests__/teams/SubIssuesSection.test.tsx
git commit -m "feat(teams): SubIssuesSection (descendants list + progress + create)"
```

---

## Task 6: Parent breadcrumb + mount in IssueDetailPage

**Files:**
- Create: `apps/frontend/src/components/teams/issues/IssueParentBreadcrumb.tsx`
- Modify: `apps/frontend/src/components/teams/issues/IssueDetailPage.tsx`
- Test: extend `apps/frontend/src/__tests__/components/teams/issues/IssueDetailPage.test.tsx`

- [ ] **Step 1: Write the breadcrumb + its failing test**

Create the breadcrumb test `apps/frontend/src/__tests__/teams/IssueParentBreadcrumb.test.tsx`:

```tsx
// apps/frontend/src/__tests__/teams/IssueParentBreadcrumb.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { IssueParentBreadcrumb } from "@/components/teams/issues/IssueParentBreadcrumb";

describe("IssueParentBreadcrumb", () => {
  it("links to the parent issue with its title", () => {
    read.mockReturnValue({ data: { id: "p1", title: "Ship billing" }, isLoading: false, error: null, mutate: vi.fn() });
    render(<IssueParentBreadcrumb parentId="p1" />);
    const link = screen.getByRole("link", { name: /Ship billing/ });
    expect(link).toHaveAttribute("href", "/teams/issues/p1");
  });

  it("falls back to 'Parent issue' before the title loads", () => {
    read.mockReturnValue({ data: undefined, isLoading: true, error: null, mutate: vi.fn() });
    render(<IssueParentBreadcrumb parentId="p1" />);
    expect(screen.getByRole("link", { name: /parent issue/i })).toHaveAttribute("href", "/teams/issues/p1");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test -- IssueParentBreadcrumb`
Expected: FAIL — cannot find module.

- [ ] **Step 3: Implement the breadcrumb**

```tsx
// apps/frontend/src/components/teams/issues/IssueParentBreadcrumb.tsx
"use client";

import Link from "next/link";
import { ChevronUp } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { Issue } from "@/components/teams/shared/types";

/** Rendered only when the issue has a parent (caller guards on parentId). */
export function IssueParentBreadcrumb({ parentId }: { parentId: string }) {
  const { read } = useTeamsApi();
  const { data: parent } = read<Issue>(`/issues/${parentId}`);
  const label = parent?.title?.trim() ? parent.title : "Parent issue";
  return (
    <Link
      href={`/teams/issues/${parentId}`}
      className="inline-flex items-center gap-1 text-sm hover:underline"
      style={{ color: "var(--forest)" }}
    >
      <ChevronUp className="h-3.5 w-3.5" />
      {label}
    </Link>
  );
}
```

- [ ] **Step 4: Run the breadcrumb test**

Run: `pnpm test -- IssueParentBreadcrumb`
Expected: PASS (2 tests).

- [ ] **Step 5: Mount both in `IssueDetailPage.tsx`**

Add imports:
```ts
import { SubIssuesSection } from "./SubIssuesSection";
import { IssueParentBreadcrumb } from "./IssueParentBreadcrumb";
```

Inside `<main …>`, render the breadcrumb above `<IssueHeader>`:
```tsx
      <main className="flex flex-col gap-6 min-w-0">
        {issue.parentId && <IssueParentBreadcrumb parentId={issue.parentId} />}
        <IssueHeader
```

Render the section after the description block and before the Conversation block:
```tsx
        )}
        <SubIssuesSection issueId={issueId} agents={agents} projects={projects} />
        <div className="pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
          <h2 className="section-label mb-3"><b>Conversation</b></h2>
```

- [ ] **Step 6: Keep the IssueDetailPage test green**

The page now calls `read("/issues/{id}/children")` (via SubIssuesSection) and possibly `read("/issues/{parentId}")` (breadcrumb, only if the mocked issue has a parentId). Open `src/__tests__/components/teams/issues/IssueDetailPage.test.tsx` and ensure its `useTeamsApi` mock's `read` returns a benign empty shape for unknown paths (default branch: `{ data: { issues: [] } , isLoading: false, error: null, mutate: vi.fn() }` is fine for the children call; if the test's mocked issue has no `parentId`, the breadcrumb won't mount). If the test mocks `read` by exact path only, add a default-return branch so `/issues/{id}/children` resolves to `{ issues: [] }`. Do NOT weaken existing assertions.

- [ ] **Step 7: Run both tests**

Run: `pnpm test -- IssueDetailPage IssueParentBreadcrumb`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/components/teams/issues/IssueParentBreadcrumb.tsx src/components/teams/issues/IssueDetailPage.tsx "src/__tests__/teams/IssueParentBreadcrumb.test.tsx" "src/__tests__/components/teams/issues/IssueDetailPage.test.tsx"
git commit -m "feat(teams): mount SubIssuesSection + parent breadcrumb on issue detail"
```

---

## Task 7: Full verification

**Files:** none (verification only).

- [ ] **Step 1: Backend — run the new test + the existing issues router tests**

Run (from `apps/backend`): `uv run pytest tests/unit/routers/teams/ -q`
Expected: all pass (new children tests + existing).

- [ ] **Step 2: Frontend typecheck**

Run (from `apps/frontend`): `pnpm exec tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Frontend lint (new/changed files)**

Run: `pnpm exec eslint src/components/teams/issues src/components/teams/shared/lib/subIssues.ts`
Expected: 0 errors.

- [ ] **Step 4: Full frontend test suite**

Run: `pnpm exec vitest run`
Expected: all sub-issue tests pass. Pre-existing local-env-only failures (Clerk/Stripe: `CreditsPanel`, `ChatGPTOAuthStep`, `useChatGPTOAuth`, `BotSetupWizard`) may fail locally — confirm none of the failures are in files this plan touched. (`vitest run` may hang at teardown after printing per-file results — read the per-file ✓/❯ lines rather than waiting on the summary.)

- [ ] **Step 5: Production build**

Run: `pnpm run build`
Expected: "✓ Compiled successfully". (A local prerender error on `/admin/health` for a missing Clerk publishable key is an env-only gap unrelated to this change — CI provides the key.)

- [ ] **Step 6: Update the roadmap** — in `docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md`, set row **D** Status to `In progress` (piece D1) and link the spec + this plan. Commit:

```bash
git add docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md
git commit -m "docs(teams): roadmap — D (issue detail) in progress, piece D1 sub-issues"
```

---

## Self-Review (against the spec)

**Spec coverage:**
- Descendants flat list (descendantOf) → Task 1 (BFF) + Task 5 (render via IssueRow, sorted).
- Progress summary (≥2) → Task 2 (pure) + Task 5 (display).
- Create via children endpoint through NewIssueDialog → Task 1 (BFF) + Task 3 (createChild) + Task 4 (dialog parentId).
- Parent breadcrumb → Task 6.
- Whitelisted passthrough (reuse CreateIssueBody) → Task 1.
- Reuse our IssueRow (read-only) → Task 5.
- Deferred items (sibling nav, cost rollup, includeBlockedBy, board toggle) → not implemented (correct).

**Type/signature consistency:** `createChild(parentId, IssueCreateInput): Promise<Issue>` is defined in Task 3 and consumed identically in Task 4. `SubIssuesSection({issueId, agents, projects})` matches its mount in Task 6. `buildSubIssueProgressSummary`/`sortSubIssues` signatures match between Task 2 and Task 5. BFF children paths (`/issues/{id}/children`) match between Task 1 (backend), Task 3 (frontend POST), and Task 5 (frontend GET). `IssueParentBreadcrumb({parentId})` matches Task 6 mount.

**Placeholder scan:** none — every step has concrete code/commands.
