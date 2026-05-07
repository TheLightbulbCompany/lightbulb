# Teams Issues Atoms Port (PR 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `IssuesPanel.tsx` (268 LOC) and the `IssueDetail*` cluster (`IssueDetailPage`/`IssueHeader`/`IssueComments`/`IssueProperties`/`NewIssueDialog`, 874 LOC across 5 files) to compose the editorial atom layer from PR 1.

**Architecture:** Pure frontend refactor. BFF surface for issues is already complete (`GET/POST/PATCH /issues`, `/issues/labels`, `/issues/{id}`, `/issues/{id}/archive|unarchive|mark-read|mark-unread|comments`). No backend changes.

**Tech Stack:** React 19, vitest, Tailwind tokens (cream/forest/ink/mute/edge), editorial atoms.

**Spec:** `docs/superpowers/specs/2026-05-07-teams-panel-port-editorial-atoms-design.md` (Issues section).

**Branch:** `plan/teams-issues-atoms-port` (cut off main after PR 3 merge).

---

## Audit summary

**Frontend files in scope:**
- `apps/frontend/src/components/teams/panels/IssuesPanel.tsx` (268 LOC) — list + inline `CreateIssueDialog`
- `apps/frontend/src/components/teams/panels/IssueDetailPanel.tsx` (15 LOC) — thin wrapper over `IssueDetailPage`; no changes
- `apps/frontend/src/components/teams/issues/IssueDetailPage.tsx` (118 LOC) — header + comments + properties layout
- `apps/frontend/src/components/teams/issues/IssueHeader.tsx` (150 LOC) — inline title edit + status/priority dropdowns
- `apps/frontend/src/components/teams/issues/IssueComments.tsx` (150 LOC) — comment list + reply textarea
- `apps/frontend/src/components/teams/issues/IssueProperties.tsx` (159 LOC) — sidebar (status/priority/assignee/labels)
- `apps/frontend/src/components/teams/issues/NewIssueDialog.tsx` (297 LOC) — full create dialog (separate from the inline one in `IssuesPanel`)

**Note on duplication:** `IssuesPanel` has an inline `CreateIssueDialog` (lines 120-268), and there's also a `NewIssueDialog.tsx` next door (297 LOC). Audit + dedupe — likely the inline one is the older version. Use the standalone `NewIssueDialog` and delete the inline one.

**BFF surface (already complete, no changes):**
- `GET /issues` (envelope-wrapped)
- `GET /issues/labels` (envelope-wrapped)
- `GET /issues/{id}`
- `POST /issues`
- `PATCH /issues/{id}`
- `POST /issues/{id}/archive|unarchive|mark-read|mark-unread`
- `GET /issues/{id}/comments`, `POST /issues/{id}/comments`

---

## File Structure

**Modify:**
- `apps/frontend/src/components/teams/panels/IssuesPanel.tsx` — wrap in `<PanelMasthead>` + `<PanelToolbar>` (search), list with `<PanelDataTable>`, empty state with `<PanelEmptyState>`, primary CTA + delete inline `CreateIssueDialog` in favour of `<NewIssueDialog>`
- `apps/frontend/src/components/teams/issues/NewIssueDialog.tsx` — wrap in `<EditorialDialog>` if not already; use `<EditorialButton>` for submit/cancel
- `apps/frontend/src/components/teams/issues/IssueHeader.tsx` — replace ad-hoc `<button class="editorial-btn">` with `<EditorialButton>`; inline title save / status / priority controls keep their existing UX
- `apps/frontend/src/components/teams/issues/IssueComments.tsx` — `<EditorialButton>` for the reply submit
- `apps/frontend/src/components/teams/issues/IssueProperties.tsx` — atom adoption only where there are buttons; otherwise leave the sidebar alone (it's mostly read-only labels)
- `apps/frontend/src/components/teams/issues/IssueDetailPage.tsx` — keep the layout; no atom usage needed in the page wrapper itself

**Add tests:**
- `apps/frontend/tests/unit/components/teams/panels/IssuesPanel.test.tsx` — atom-contract guards
- `apps/frontend/tests/unit/components/teams/issues/NewIssueDialog.test.tsx` — guards `<EditorialDialog>` usage

**Delete:**
- The inline `CreateIssueDialog` function inside `IssuesPanel.tsx` (lines 120-268). Keep one source of truth.

---

## Task 0: Setup + audit

- [ ] **Step 1: Branch + base**

```sh
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix
git checkout main && git pull origin main
git checkout -b plan/teams-issues-atoms-port
```

- [ ] **Step 2: Read all 7 files end-to-end**

```sh
cat apps/frontend/src/components/teams/panels/IssuesPanel.tsx
cat apps/frontend/src/components/teams/panels/IssueDetailPanel.tsx
cat apps/frontend/src/components/teams/issues/IssueDetailPage.tsx
cat apps/frontend/src/components/teams/issues/IssueHeader.tsx
cat apps/frontend/src/components/teams/issues/IssueComments.tsx
cat apps/frontend/src/components/teams/issues/IssueProperties.tsx
cat apps/frontend/src/components/teams/issues/NewIssueDialog.tsx
```

Catalog: existing button/dialog/input usage; what is shared vs duplicated; which files reference each other.

- [ ] **Step 3: Baseline tests**

```sh
cd apps/frontend && pnpm test "components/teams/issues" -- --run 2>&1 | tail -15
cd apps/frontend && pnpm test "components/teams/panels/IssuesPanel" -- --run 2>&1 | tail -15
```

Note baseline.

- [ ] **Step 4: Commit the plan first**

The PR 1/PR 2 pattern: plan ships as the first commit on the branch.

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add docs/superpowers/plans/2026-05-07-teams-issues-atoms-port.md
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "plan(teams): PR 4 — Issues atoms port implementation steps"
```

---

## Task 1: Refactor `IssuesPanel.tsx`

Replace the 268 LOC panel with a much shorter atom composition. Delete the inline `CreateIssueDialog`; route the New-issue CTA to `NewIssueDialog`.

- [ ] **Step 1: Write atom-contract test**

Create `apps/frontend/tests/unit/components/teams/panels/IssuesPanel.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { IssuesPanel } from "@/components/teams/panels/IssuesPanel";

// Mock useTeamsApi so the panel resolves with empty issues
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: { issues: [] }, isLoading: false, error: null, mutate: vi.fn() }),
    post: vi.fn(),
  }),
}));

describe("IssuesPanel atom contract", () => {
  it("renders PanelMasthead with overline + h1", () => {
    render(<IssuesPanel />);
    expect(screen.getByRole("heading", { level: 1, name: /issues/i })).toBeInTheDocument();
  });

  it("renders PanelToolbar search input", () => {
    render(<IssuesPanel />);
    expect(screen.getByRole("searchbox")).toBeInTheDocument();
  });

  it("renders PanelEmptyState when no issues", () => {
    render(<IssuesPanel />);
    expect(screen.getByRole("heading", { level: 2 })).toHaveClass("font-editorial-serif");
  });

  it("renders New issue CTA via EditorialButton", () => {
    render(<IssuesPanel />);
    expect(screen.getByRole("button", { name: /new issue/i })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run failing**

```sh
cd apps/frontend && pnpm test IssuesPanel -- --run
```

Expected: at least 2 fails (no `searchbox`, no editorial-serif h2 because empty branch is plain `<li>`).

- [ ] **Step 3: Rewrite `IssuesPanel.tsx`**

```tsx
"use client";

import { useState, useMemo } from "react";
import { ListChecks } from "lucide-react";
import {
  PanelMasthead,
  PanelToolbar,
  PanelEmptyState,
  PanelDataTable,
  EditorialButton,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Issue {
  id: string;
  title: string;
  status?: string;
  priority?: string;
  project_id?: string;
  assignee_agent_id?: string;
}

export function IssuesPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error, mutate } = read<{ issues: Issue[] }>("/issues");
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");

  const issues = data?.issues ?? [];
  const filtered = useMemo(
    () =>
      search
        ? issues.filter((i) => i.title.toLowerCase().includes(search.toLowerCase()))
        : issues,
    [issues, search],
  );

  const columns: PanelDataColumn<Issue>[] = [
    { key: "title", header: "Title", cell: (i) => i.title, sortable: true },
    { key: "status", header: "Status", cell: (i) => i.status ?? "open" },
    {
      key: "priority",
      header: "Priority",
      cell: (i) => i.priority ?? "—",
    },
    {
      key: "open",
      header: "",
      align: "right",
      cell: (i) => (
        <a
          href={`/teams/issues/${i.id}`}
          className="text-sm hover:underline"
          style={{ color: "var(--forest)" }}
        >
          Open →
        </a>
      ),
    },
  ];

  if (error) {
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );
  }

  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Work"
        title="Issues"
        actions={
          <EditorialButton onClick={() => setCreating(true)}>
            New issue
          </EditorialButton>
        }
      />
      <PanelToolbar
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder="Find issue..."
        searchAriaLabel="Search issues"
      />
      {!isLoading && filtered.length === 0 ? (
        <PanelEmptyState
          icon={ListChecks}
          headline="No issues yet."
          supporting="Create an issue to track a piece of work — assign it to an agent and follow it through to done."
          action={{ label: "New issue", onClick: () => setCreating(true) }}
        />
      ) : (
        <PanelDataTable<Issue>
          rows={filtered}
          columns={columns}
          rowKey={(i) => i.id}
        />
      )}

      {creating && (
        <NewIssueDialog
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            mutate();
          }}
        />
      )}
    </div>
  );
}
```

(If `NewIssueDialog`'s prop signature differs from `{onClose, onCreated}`, adapt the call to match.)

- [ ] **Step 4: Run tests, verify pass**

```sh
cd apps/frontend && pnpm test IssuesPanel -- --run
```

- [ ] **Step 5: Commit**

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add \
  apps/frontend/src/components/teams/panels/IssuesPanel.tsx \
  apps/frontend/tests/unit/components/teams/panels/IssuesPanel.test.tsx
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "feat(teams-issues): IssuesPanel composes editorial atoms + uses NewIssueDialog"
```

---

## Task 2: Refactor `NewIssueDialog.tsx`

Audit first — `NewIssueDialog.tsx` is 297 LOC. It likely already has its own form fields and a custom dialog wrapper. Wrap in `<EditorialDialog>` and use `<EditorialButton>` for actions.

- [ ] **Step 1: Read it**

```sh
cat apps/frontend/src/components/teams/issues/NewIssueDialog.tsx
```

- [ ] **Step 2: Write atom-contract test**

`apps/frontend/tests/unit/components/teams/issues/NewIssueDialog.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ post: vi.fn() }),
}));

describe("NewIssueDialog atom contract", () => {
  it("renders inside EditorialDialog (role=dialog)", () => {
    render(<NewIssueDialog onClose={() => {}} onCreated={() => {}} />);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("primary CTA + Cancel are EditorialButtons", () => {
    render(<NewIssueDialog onClose={() => {}} onCreated={() => {}} />);
    expect(screen.getByRole("button", { name: /create/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Refactor**

Wrap the existing form body in `<EditorialDialog open={true} onOpenChange={(o) => !o && onClose()} title="New issue">`. Replace `<button class="editorial-btn editorial-btn--primary editorial-btn--sm">` with `<EditorialButton size="sm">`, secondary with `variant="secondary"`.

(Adapt to actual file — show what stays vs. what gets atomized.)

- [ ] **Step 4: Verify pass + commit**

```sh
cd apps/frontend && pnpm test NewIssueDialog -- --run
git -C ... add apps/frontend/src/components/teams/issues/NewIssueDialog.tsx \
  apps/frontend/tests/unit/components/teams/issues/NewIssueDialog.test.tsx
git -C ... commit -m "feat(teams-issues): NewIssueDialog uses EditorialDialog + EditorialButton"
```

---

## Task 3: Refactor `IssueHeader.tsx` + `IssueComments.tsx`

Replace any `<button class="editorial-btn ...">` with `<EditorialButton>`. Keep the layout, status/priority dropdown UX, and inline title save behaviour identical.

- [ ] **Step 1: Find ad-hoc buttons**

```sh
grep -n "editorial-btn" apps/frontend/src/components/teams/issues/IssueHeader.tsx apps/frontend/src/components/teams/issues/IssueComments.tsx apps/frontend/src/components/teams/issues/IssueProperties.tsx
```

- [ ] **Step 2: Atomize each one**

In each file: `<button className="editorial-btn editorial-btn--primary editorial-btn--sm">Save</button>` → `<EditorialButton size="sm">Save</EditorialButton>`. `<button className="editorial-btn editorial-btn--secondary editorial-btn--sm">Cancel</button>` → `<EditorialButton variant="secondary" size="sm">Cancel</EditorialButton>`.

- [ ] **Step 3: Verify all teams tests still pass**

```sh
cd apps/frontend && pnpm test "components/teams" -- --run 2>&1 | tail -15
```

- [ ] **Step 4: Commit**

```sh
git -C ... add apps/frontend/src/components/teams/issues/IssueHeader.tsx \
  apps/frontend/src/components/teams/issues/IssueComments.tsx \
  apps/frontend/src/components/teams/issues/IssueProperties.tsx
git -C ... commit -m "feat(teams-issues): IssueHeader/Comments/Properties use EditorialButton"
```

---

## Task 4: Final verify + push + open PR

- [ ] **Step 1: Full FE test suite**

```sh
cd apps/frontend && pnpm test 2>&1 | tail -15
```

Expected: counts unchanged from PR 3 baseline + new tests pass.

- [ ] **Step 2: Lint + tsc**

```sh
cd apps/frontend && pnpm lint && pnpm tsc --noEmit
```

- [ ] **Step 3: Push + open PR**

```sh
git -C ... push -u origin plan/teams-issues-atoms-port
gh pr create --title "feat(teams-issues): port to editorial atom layer (PR 4 of /teams port)" --body "$(cat <<'EOF'
## Summary

Fourth PR of the multi-step /teams panel port. Refactors `IssuesPanel.tsx` (268 LOC → ~80 LOC) and the `IssueDetail*` cluster (`IssueHeader`/`IssueComments`/`IssueProperties`/`NewIssueDialog`) to compose the editorial atom layer.

## What changed

- `IssuesPanel.tsx` — `<PanelMasthead>` + `<PanelToolbar>` + `<PanelDataTable>` + `<PanelEmptyState>`. New-issue CTA via `<EditorialButton>`. Inline `CreateIssueDialog` deleted in favour of the existing `NewIssueDialog`.
- `NewIssueDialog.tsx` — wrapped in `<EditorialDialog>`; submit/cancel via `<EditorialButton>`.
- `IssueHeader.tsx`, `IssueComments.tsx`, `IssueProperties.tsx` — ad-hoc `<button class="editorial-btn">` replaced with `<EditorialButton>`.

## Out of scope

- BFF changes — issues surface is already complete (list/labels/get/create/patch/archive/comments).
- IssueDetail layout — kept as-is; this PR is atom-adoption only.

## Test plan

- [x] Frontend pnpm test — atom-contract guards on IssuesPanel + NewIssueDialog
- [x] tsc clean
- [x] lint clean
- [ ] Manual smoke: load /teams → Issues tab. Verify search, empty state, New-issue dialog open, submit. Open issue detail and try Save/Cancel comment buttons.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review notes

**Spec coverage:** atom adoption across the 5 issue-related files — ✅ Tasks 1-3.

**Out of scope:** BFF (already complete); IssueDetail layout overhaul (separate PR if needed).

**Type consistency:** `Issue` interface matches what BFF returns (id/title/status/priority/project_id/assignee_agent_id).

**Placeholder scan:** "(Adapt to actual file — show what stays vs. what gets atomized.)" in Task 2 step 3 — this is a directive to the implementer because the dialog has 297 LOC of unique content; the implementer must read and refactor concretely. Acceptable.
