# Teams Goals + Projects Atoms Port (PR 7) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor 3 panels — `GoalsPanel.tsx` (192 LOC), `ProjectsListPanel.tsx` (159 LOC), `ProjectDetailPanel.tsx` (71 LOC) — to compose editorial atoms.

**Architecture:** Pure frontend refactor. Same pattern as PRs 2-4: `<PanelMasthead>` + `<PanelToolbar>` + list/tree (`<PanelDataTable>` for projects flat list, custom tree for goals due to nested structure) + `<PanelEmptyState>` + `<EditorialDialog>` for create dialogs.

**Tech Stack:** React 19, vitest, editorial atoms.

**Branch:** `plan/teams-goals-projects-atoms-port`.

---

## Audit summary

- Goals BFF: `GET /goals`, `POST /goals` (in `routers/teams/work.py`). No DELETE / PATCH today; create-only is fine for the editorial restyle scope.
- Projects BFF: `GET /projects`, `POST /projects`, `GET /projects/{id}` (the detail panel uses this). No DELETE / PATCH today.
- Goals tree builder lives in the panel itself (`buildTree` helper). Keep the helper; just wrap the rendering in atoms.
- ProjectDetailPanel has tabs: `overview` / `issues` / `budget`. Two of those are placeholders ("coming soon"). Replace with `<PanelTabs>`.

---

## Task 0: Setup

- [ ] **Step 1: Branch + base**

```sh
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix
git checkout main && git pull origin main
git checkout -b plan/teams-goals-projects-atoms-port
```

- [ ] **Step 2: Commit the plan first** (PR 1/PR 2 pattern)

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add docs/superpowers/plans/2026-05-07-teams-goals-projects-atoms-port.md
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "plan(teams): PR 7 — Goals + Projects atoms port implementation steps"
```

---

## Task 1: `GoalsPanel.tsx` refactor

**Files:**
- Modify: `apps/frontend/src/components/teams/panels/GoalsPanel.tsx`
- Test: `apps/frontend/tests/unit/components/teams/panels/GoalsPanel.test.tsx`

- [ ] **Step 1: Atom-contract test**

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { GoalsPanel } from "@/components/teams/panels/GoalsPanel";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: { goals: [] }, isLoading: false, error: null, mutate: vi.fn() }),
    post: vi.fn(),
  }),
}));

describe("GoalsPanel atom contract", () => {
  it("renders PanelMasthead h1 + New goal CTA", () => {
    render(<GoalsPanel />);
    expect(screen.getByRole("heading", { level: 1, name: /goals/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /new goal/i })).toBeInTheDocument();
  });

  it("renders PanelEmptyState when empty", () => {
    render(<GoalsPanel />);
    expect(screen.getByRole("heading", { level: 2 })).toHaveClass("font-editorial-serif");
  });
});
```

- [ ] **Step 2: Refactor**

Wrap the panel in `<PanelMasthead overline="Strategy" title="Goals" actions={<EditorialButton onClick={() => setCreating(true)}>New goal</EditorialButton>} />`. Keep the `buildTree` helper + `<GoalRow>` recursive renderer (data table doesn't fit nested data well — keep an `editorial-card` `<ul>` for the tree). Use `<PanelEmptyState icon={Target} headline="No goals yet." supporting="Define a goal to give your agents direction — break it into nested sub-goals as needed." />` for empty branch. Use `<PanelToolbar>` if a search field would help; otherwise skip (goals are tree-shaped).

Refactor `CreateGoalDialog` to wrap in `<EditorialDialog>` with `<EditorialButton>` actions.

- [ ] **Step 3: Verify + commit**

```sh
cd apps/frontend && pnpm test GoalsPanel -- --run
git -C ... add apps/frontend/src/components/teams/panels/GoalsPanel.tsx \
  apps/frontend/tests/unit/components/teams/panels/GoalsPanel.test.tsx
git -C ... commit -m "feat(teams-goals): GoalsPanel composes editorial atoms"
```

---

## Task 2: `ProjectsListPanel.tsx` refactor

**Files:**
- Modify: `apps/frontend/src/components/teams/panels/ProjectsListPanel.tsx`
- Test: `apps/frontend/tests/unit/components/teams/panels/ProjectsListPanel.test.tsx`

- [ ] **Step 1: Atom-contract test**

Same shape as GoalsPanel test, asserting `<PanelMasthead>` + searchbox + empty state + New-project CTA.

- [ ] **Step 2: Refactor**

```tsx
"use client";

import { useState, useMemo } from "react";
import { FolderKanban } from "lucide-react";
import {
  PanelMasthead,
  PanelToolbar,
  PanelEmptyState,
  PanelDataTable,
  EditorialButton,
  EditorialDialog,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Project {
  id: string;
  name: string;
  description?: string;
}

export function ProjectsListPanel() {
  const { read, post } = useTeamsApi();
  const { data, isLoading, error, mutate } = read<{ projects: Project[] }>("/projects");
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");

  const projects = data?.projects ?? [];
  const filtered = useMemo(
    () =>
      search
        ? projects.filter((p) =>
            (p.name + (p.description ?? "")).toLowerCase().includes(search.toLowerCase()),
          )
        : projects,
    [projects, search],
  );

  const columns: PanelDataColumn<Project>[] = [
    { key: "name", header: "Name", cell: (p) => p.name, sortable: true },
    {
      key: "description",
      header: "Description",
      cell: (p) => (
        <span className="text-[var(--ink-soft)] text-sm">{p.description ?? "—"}</span>
      ),
    },
    {
      key: "open",
      header: "",
      align: "right",
      cell: (p) => (
        <a
          href={`/teams/projects/${p.id}`}
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
        title="Projects"
        actions={
          <EditorialButton onClick={() => setCreating(true)}>
            New project
          </EditorialButton>
        }
      />
      <PanelToolbar
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder="Find project..."
        searchAriaLabel="Search projects"
      />
      {!isLoading && filtered.length === 0 ? (
        <PanelEmptyState
          icon={FolderKanban}
          headline="No projects yet."
          supporting="Create a project to group related issues + measure progress on a deliverable."
          action={{ label: "New project", onClick: () => setCreating(true) }}
        />
      ) : (
        <PanelDataTable<Project>
          rows={filtered}
          columns={columns}
          rowKey={(p) => p.id}
        />
      )}

      {creating && (
        <CreateProjectDialog
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

function CreateProjectDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const { post } = useTeamsApi();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    setSubmitting(true);
    setErr(null);
    try {
      const body: Record<string, unknown> = { name };
      if (description) body.description = description;
      await post("/projects", body);
      onCreated();
    } catch (e) {
      setErr(String(e));
      setSubmitting(false);
    }
  }

  return (
    <EditorialDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="New project"
    >
      <div className="flex flex-col gap-3">
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          />
        </label>
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Description</span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
            rows={3}
          />
        </label>
        {err && (
          <div className="text-[var(--danger)] text-sm border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] rounded px-3 py-2">
            {err}
          </div>
        )}
        <div className="flex justify-end gap-2 mt-2">
          <EditorialButton variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </EditorialButton>
          <EditorialButton
            size="sm"
            onClick={submit}
            disabled={submitting || !name}
          >
            {submitting ? "Creating…" : "Create"}
          </EditorialButton>
        </div>
      </div>
    </EditorialDialog>
  );
}
```

- [ ] **Step 3: Verify + commit**

```sh
cd apps/frontend && pnpm test ProjectsListPanel -- --run
git -C ... add apps/frontend/src/components/teams/panels/ProjectsListPanel.tsx \
  apps/frontend/tests/unit/components/teams/panels/ProjectsListPanel.test.tsx
git -C ... commit -m "feat(teams-projects): ProjectsListPanel composes editorial atoms"
```

---

## Task 3: `ProjectDetailPanel.tsx` refactor

**Files:**
- Modify: `apps/frontend/src/components/teams/panels/ProjectDetailPanel.tsx`
- Test: `apps/frontend/tests/unit/components/teams/panels/ProjectDetailPanel.test.tsx`

- [ ] **Step 1: Atom-contract test**

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ProjectDetailPanel } from "@/components/teams/panels/ProjectDetailPanel";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({
      data: { id: "p1", name: "Test Project", description: "A test" },
      isLoading: false,
    }),
  }),
}));

describe("ProjectDetailPanel atom contract", () => {
  it("renders PanelMasthead with title", () => {
    render(<ProjectDetailPanel projectId="p1" />);
    expect(screen.getByRole("heading", { level: 1, name: /test project/i })).toBeInTheDocument();
  });

  it("renders PanelTabs with overview/issues/budget", () => {
    render(<ProjectDetailPanel projectId="p1" />);
    expect(screen.getByRole("tab", { name: /overview/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /issues/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /budget/i })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Refactor**

Replace the panel body with:

```tsx
"use client";

import { useState } from "react";
import { PanelMasthead, PanelTabs } from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Project {
  id: string;
  name: string;
  description?: string;
  budget_monthly_cents?: number;
}

const TABS = [
  { value: "overview", label: "Overview" },
  { value: "issues", label: "Issues" },
  { value: "budget", label: "Budget" },
];

export function ProjectDetailPanel({ projectId }: { projectId: string }) {
  const { read } = useTeamsApi();
  const { data, isLoading } = read<Project>(`/projects/${projectId}`);
  const [tab, setTab] = useState<string>("overview");

  if (isLoading) {
    return (
      <div className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--mute)]">
        Loading…
      </div>
    );
  }
  if (!data) {
    return (
      <div className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--mute)]">
        Project not found.
      </div>
    );
  }

  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead overline="Project" title={data.name} />
      {data.description && (
        <p className="text-sm text-[var(--ink-soft)] -mt-3">{data.description}</p>
      )}
      <PanelTabs tabs={TABS} value={tab} onValueChange={setTab} />

      {tab === "overview" && (
        <pre className="editorial-card text-xs p-4 whitespace-pre-wrap text-[var(--ink-soft)] bg-[var(--cream-deeper)]">
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
      {tab === "issues" && (
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
          Issues per project — coming soon.
        </div>
      )}
      {tab === "budget" && (
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
          Budget breakdown — coming soon.
        </div>
      )}
    </div>
  );
}
```

(Verify `PanelTabs` prop signature — adapt if it expects different props.)

- [ ] **Step 3: Verify + commit**

```sh
cd apps/frontend && pnpm test ProjectDetailPanel -- --run
git -C ... add apps/frontend/src/components/teams/panels/ProjectDetailPanel.tsx \
  apps/frontend/tests/unit/components/teams/panels/ProjectDetailPanel.test.tsx
git -C ... commit -m "feat(teams-projects): ProjectDetailPanel composes editorial atoms"
```

---

## Task 4: Final verify + push + open PR

- [ ] **Step 1: Full FE test suite + lint + tsc**

```sh
cd apps/frontend && pnpm test 2>&1 | tail -10
cd apps/frontend && pnpm lint && pnpm tsc --noEmit
```

- [ ] **Step 2: Push + PR**

```sh
git -C ... push -u origin plan/teams-goals-projects-atoms-port
gh pr create --title "feat(teams-goals-projects): port to editorial atom layer (PR 7)" --body "$(cat <<'EOF'
## Summary

PR 7 of the /teams panel port. Refactors GoalsPanel (192 LOC), ProjectsListPanel (159 LOC), and ProjectDetailPanel (71 LOC) to compose editorial atoms.

## What changed

- GoalsPanel: PanelMasthead + EditorialDialog (CreateGoalDialog) + PanelEmptyState. Tree renderer kept (data table doesn't fit nested data).
- ProjectsListPanel: PanelMasthead + PanelToolbar + PanelDataTable + PanelEmptyState + EditorialDialog (CreateProjectDialog).
- ProjectDetailPanel: PanelMasthead + PanelTabs.

## Test plan

- [x] Atom-contract guards
- [x] tsc + lint clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
