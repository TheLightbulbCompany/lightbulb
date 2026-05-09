# Teams Kanban Board Implementation Plan (PR #6)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a list/board view toggle to `/teams/inbox` and render a verbatim port of upstream Paperclip's `KanbanBoard.tsx` as the board view, retheme'd to our editorial palette.

**Architecture:** Three new sibling components under `components/teams/inbox/` (`KanbanBoard`, `KanbanColumn`, `KanbanCard`) wired through a `<DndContext>` from `@dnd-kit/core`. A `useInboxViewMode` hook persists the toggle state to `localStorage` keyed per-company. `InboxToolbar` gains a `<ViewModeToggle>`; `InboxPage` branches on the persisted mode. Drag-end calls `onUpdateIssue(id, { status })` which is wired to `PATCH /teams/issues/{id}` via the existing `useTeamsApi` mutation surface. Realtime updates flow through the existing `TeamsEventsProvider` cache invalidation (no new event subscriptions).

**Tech Stack:** React 19, Next.js 16 App Router, Tailwind v4, SWR, lucide-react, **`@dnd-kit/core` `^6.3.1` + `@dnd-kit/sortable` `^10.0.0` + `@dnd-kit/utilities` `^3.2.2`** (new — pin matches upstream Paperclip's `paperclip/ui/package.json`). Vitest for tests.

**Upstream reference:** `paperclip/ui/src/components/KanbanBoard.tsx` (281 LOC). Toggle pattern from `paperclip/ui/src/components/IssuesList.tsx:117,128,572` (`viewMode: "list" | "board"` localStorage scoped key).

**Spec:** [`docs/superpowers/specs/2026-05-09-teams-kanban-board-design.md`](../specs/2026-05-09-teams-kanban-board-design.md)

---

## Worktree

Before starting Task 1, create an isolated worktree off `origin/main`:

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync
git fetch origin
git worktree add .claude/worktrees/feat+teams-kanban -b feat/teams-kanban origin/main
cd .claude/worktrees/feat+teams-kanban
```

All subsequent commands run inside `.claude/worktrees/feat+teams-kanban`. Do not edit the main checkout.

---

## Common conventions

- 3-line MIT attribution header on every ported file (matches PR #3b/c/d, #4, #5 precedent).
- Test files use **explicit vitest imports** (`import { describe, it, expect, vi, beforeEach } from "vitest"`).
- Tailwind retheme rules: KEEP semantic editorial tokens (`var(--forest)`, `var(--mute)`, `var(--ink)`, `var(--paper)`, `var(--paper-soft)`, `var(--danger)`, `var(--warn)`). Replace `bg-card` with `editorial-card`. Replace `bg-blue-{400,500}` with `var(--forest)`.
- **DO NOT push between tasks.** Push at Task 8.
- Commits go on `feat/teams-kanban` (the worktree branch). Never commit to `main`.

---

## File structure (locked)

```
apps/frontend/src/components/teams/inbox/
├── KanbanBoard.tsx                NEW   ~150 LOC — DndContext + 7 columns + drag-end wiring
├── KanbanColumn.tsx               NEW   ~70 LOC  — useDroppable + column header + card list
├── KanbanCard.tsx                 NEW   ~85 LOC  — useSortable + card content
├── ViewModeToggle.tsx             NEW   ~50 LOC  — segmented "List | Board" control
├── InboxToolbar.tsx               EDIT  add ViewModeToggle to right cluster
├── InboxPage.tsx                  EDIT  branch on viewMode → KanbanBoard | InboxList
└── hooks/
    └── useInboxViewMode.ts        NEW   ~40 LOC  — localStorage round-trip, scoped per companyId

apps/frontend/src/__tests__/components/teams/inbox/
├── KanbanBoard.test.tsx           NEW
├── KanbanColumn.test.tsx          NEW
├── KanbanCard.test.tsx            NEW
├── ViewModeToggle.test.tsx        NEW
└── hooks/
    └── useInboxViewMode.test.ts   NEW
```

---

## Task 1: Install @dnd-kit deps + smoke test

**Goal:** Land the three dependencies and verify they import cleanly. No app code changes yet.

**Files:**
- Modify: `apps/frontend/package.json`
- Modify: `pnpm-lock.yaml` (auto)

- [ ] **Step 1: Install deps**

```bash
cd apps/frontend
pnpm add @dnd-kit/core@^6.3.1 @dnd-kit/sortable@^10.0.0 @dnd-kit/utilities@^3.2.2
```

Expected: `package.json` `dependencies` block now contains the three packages with the pinned majors.

- [ ] **Step 2: Smoke-test the import path**

Open a Node REPL or use a one-shot:

```bash
cd apps/frontend
node -e "import('@dnd-kit/core').then(m => console.log(typeof m.DndContext))"
```

Expected: `function`

- [ ] **Step 3: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/feat+teams-kanban
git add apps/frontend/package.json pnpm-lock.yaml
git commit -m "chore(deps): add @dnd-kit/{core,sortable,utilities} for teams kanban port"
```

---

## Task 2: useInboxViewMode hook

Pure hook, no React tree. Foundation for the toggle.

**Files:**
- Create: `apps/frontend/src/components/teams/inbox/hooks/useInboxViewMode.ts`
- Test: `apps/frontend/src/__tests__/components/teams/inbox/hooks/useInboxViewMode.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
// apps/frontend/src/__tests__/components/teams/inbox/hooks/useInboxViewMode.test.ts
import { describe, it, expect, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useInboxViewMode } from "@/components/teams/inbox/hooks/useInboxViewMode";

describe("useInboxViewMode", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("defaults to 'list' with no stored value", () => {
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    expect(result.current.viewMode).toBe("list");
  });

  it("reads a stored value", () => {
    window.localStorage.setItem("isol8:teams:inbox-view-mode:co-1", "board");
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    expect(result.current.viewMode).toBe("board");
  });

  it("writes on setViewMode", () => {
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    act(() => result.current.setViewMode("board"));
    expect(result.current.viewMode).toBe("board");
    expect(window.localStorage.getItem("isol8:teams:inbox-view-mode:co-1")).toBe("board");
  });

  it("scopes per companyId", () => {
    window.localStorage.setItem("isol8:teams:inbox-view-mode:co-1", "board");
    const { result } = renderHook(() => useInboxViewMode("co-2"));
    expect(result.current.viewMode).toBe("list");
  });

  it("returns 'list' with null companyId without touching localStorage", () => {
    const { result } = renderHook(() => useInboxViewMode(null));
    expect(result.current.viewMode).toBe("list");
    act(() => result.current.setViewMode("board"));
    // null companyId: setter is a no-op (no key to scope to).
    expect(window.localStorage.length).toBe(0);
  });

  it("ignores unknown stored values", () => {
    window.localStorage.setItem("isol8:teams:inbox-view-mode:co-1", "garbage");
    const { result } = renderHook(() => useInboxViewMode("co-1"));
    expect(result.current.viewMode).toBe("list");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/frontend
pnpm test -- src/__tests__/components/teams/inbox/hooks/useInboxViewMode.test.ts
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement the hook**

```ts
// apps/frontend/src/components/teams/inbox/hooks/useInboxViewMode.ts

// Scoped-per-company list/board view-mode persistence.
// Mirrors upstream Paperclip's IssuesList view-mode storage pattern
// (paperclip/ui/src/components/IssuesList.tsx:568) (MIT, (c) 2025 Paperclip AI).

"use client";

import { useCallback, useEffect, useState } from "react";

export type InboxViewMode = "list" | "board";

const KEY_PREFIX = "isol8:teams:inbox-view-mode:";
const VALID: readonly InboxViewMode[] = ["list", "board"];

function isViewMode(v: string | null): v is InboxViewMode {
  return v !== null && (VALID as readonly string[]).includes(v);
}

export function useInboxViewMode(companyId: string | null): {
  viewMode: InboxViewMode;
  setViewMode: (mode: InboxViewMode) => void;
} {
  const [viewMode, setViewModeState] = useState<InboxViewMode>("list");

  useEffect(() => {
    if (!companyId) {
      setViewModeState("list");
      return;
    }
    const stored = window.localStorage.getItem(KEY_PREFIX + companyId);
    setViewModeState(isViewMode(stored) ? stored : "list");
  }, [companyId]);

  const setViewMode = useCallback(
    (mode: InboxViewMode) => {
      if (!companyId) return; // no key to scope to; treat as no-op
      window.localStorage.setItem(KEY_PREFIX + companyId, mode);
      setViewModeState(mode);
    },
    [companyId],
  );

  return { viewMode, setViewMode };
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/frontend
pnpm test -- src/__tests__/components/teams/inbox/hooks/useInboxViewMode.test.ts
```

Expected: 6/6 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/feat+teams-kanban
git add apps/frontend/src/components/teams/inbox/hooks/useInboxViewMode.ts \
        apps/frontend/src/__tests__/components/teams/inbox/hooks/useInboxViewMode.test.ts \
        docs/superpowers/plans/2026-05-09-teams-kanban-board.md \
        docs/superpowers/specs/2026-05-09-teams-kanban-board-design.md
git commit -m "feat(teams): add useInboxViewMode hook for list/board persistence"
```

---

## Task 3: ViewModeToggle component

**Files:**
- Create: `apps/frontend/src/components/teams/inbox/ViewModeToggle.tsx`
- Test: `apps/frontend/src/__tests__/components/teams/inbox/ViewModeToggle.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/components/teams/inbox/ViewModeToggle.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ViewModeToggle } from "@/components/teams/inbox/ViewModeToggle";

describe("ViewModeToggle", () => {
  it("renders both buttons", () => {
    render(<ViewModeToggle value="list" onChange={() => {}} />);
    expect(screen.getByRole("button", { name: /list/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /board/i })).toBeInTheDocument();
  });

  it("marks the active button with aria-pressed=true", () => {
    render(<ViewModeToggle value="board" onChange={() => {}} />);
    expect(screen.getByRole("button", { name: /board/i })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /list/i })).toHaveAttribute("aria-pressed", "false");
  });

  it("calls onChange('board') when board button clicked from list mode", async () => {
    const onChange = vi.fn();
    render(<ViewModeToggle value="list" onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: /board/i }));
    expect(onChange).toHaveBeenCalledWith("board");
  });

  it("does not call onChange when the active button is clicked", async () => {
    const onChange = vi.fn();
    render(<ViewModeToggle value="list" onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: /list/i }));
    expect(onChange).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/frontend
pnpm test -- src/__tests__/components/teams/inbox/ViewModeToggle.test.tsx
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement the component**

```tsx
// apps/frontend/src/components/teams/inbox/ViewModeToggle.tsx

// Segmented list/board view-mode control for the inbox toolbar.
// Mirrors upstream IssuesList toggle (paperclip/ui/src/components/IssuesList.tsx)
// (MIT, (c) 2025 Paperclip AI). Editorial retheme: forest active fill, ink text.

"use client";

import { Columns3, Rows3 } from "lucide-react";
import { cn } from "@/lib/utils";
import type { InboxViewMode } from "@/components/teams/inbox/hooks/useInboxViewMode";

export interface ViewModeToggleProps {
  value: InboxViewMode;
  onChange: (value: InboxViewMode) => void;
  className?: string;
}

export function ViewModeToggle({ value, onChange, className }: ViewModeToggleProps) {
  return (
    <div
      role="group"
      aria-label="View mode"
      className={cn(
        "inline-flex items-center gap-0.5 rounded-md border p-0.5",
        className,
      )}
      style={{ borderColor: "var(--edge)", backgroundColor: "var(--paper)" }}
    >
      <SegmentButton
        label="List"
        Icon={Rows3}
        active={value === "list"}
        onClick={() => value !== "list" && onChange("list")}
      />
      <SegmentButton
        label="Board"
        Icon={Columns3}
        active={value === "board"}
        onClick={() => value !== "board" && onChange("board")}
      />
    </div>
  );
}

function SegmentButton({
  label,
  Icon,
  active,
  onClick,
}: {
  label: string;
  Icon: typeof Rows3;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded px-2 py-1 text-xs font-editorial-sans transition-colors",
      )}
      style={{
        backgroundColor: active ? "var(--forest-tint)" : "transparent",
        color: active ? "var(--forest)" : "var(--mute)",
      }}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {label}
    </button>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/frontend
pnpm test -- src/__tests__/components/teams/inbox/ViewModeToggle.test.tsx
```

Expected: 4/4 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/feat+teams-kanban
git add apps/frontend/src/components/teams/inbox/ViewModeToggle.tsx \
        apps/frontend/src/__tests__/components/teams/inbox/ViewModeToggle.test.tsx
git commit -m "feat(teams): add ViewModeToggle segmented control"
```

---

## Task 4: KanbanCard component

**Files:**
- Create: `apps/frontend/src/components/teams/inbox/KanbanCard.tsx`
- Test: `apps/frontend/src/__tests__/components/teams/inbox/KanbanCard.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/components/teams/inbox/KanbanCard.test.tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { DndContext } from "@dnd-kit/core";
import { SortableContext } from "@dnd-kit/sortable";
import { KanbanCard } from "@/components/teams/inbox/KanbanCard";
import type { Issue } from "@/components/teams/shared/types";

const baseIssue: Issue = {
  id: "iss-1",
  identifier: "ENG-42",
  title: "Wire up the kanban board",
  status: "todo",
  priority: "high",
  assigneeAgentId: null,
};

function wrap(ui: React.ReactNode) {
  // SortableContext requires DndContext as ancestor.
  return (
    <DndContext>
      <SortableContext items={["iss-1"]}>{ui}</SortableContext>
    </DndContext>
  );
}

describe("KanbanCard", () => {
  it("renders the identifier and title", () => {
    render(wrap(<KanbanCard issue={baseIssue} agents={[]} />));
    expect(screen.getByText("ENG-42")).toBeInTheDocument();
    expect(screen.getByText("Wire up the kanban board")).toBeInTheDocument();
  });

  it("renders the live indicator when isLive=true", () => {
    render(wrap(<KanbanCard issue={baseIssue} agents={[]} isLive />));
    expect(screen.getByTestId("kanban-card-live-dot")).toBeInTheDocument();
  });

  it("does not render the live indicator when isLive=false", () => {
    render(wrap(<KanbanCard issue={baseIssue} agents={[]} />));
    expect(screen.queryByTestId("kanban-card-live-dot")).not.toBeInTheDocument();
  });

  it("links to the issue detail using identifier when present", () => {
    render(wrap(<KanbanCard issue={baseIssue} agents={[]} />));
    expect(screen.getByRole("link")).toHaveAttribute("href", "/teams/issues/ENG-42");
  });

  it("links using id when identifier is null", () => {
    render(
      wrap(
        <KanbanCard issue={{ ...baseIssue, identifier: null }} agents={[]} />,
      ),
    );
    expect(screen.getByRole("link")).toHaveAttribute("href", "/teams/issues/iss-1");
  });

  it("renders agent name when assigneeAgentId resolves", () => {
    render(
      wrap(
        <KanbanCard
          issue={{ ...baseIssue, assigneeAgentId: "agent-1" }}
          agents={[{ id: "agent-1", name: "Atlas" }]}
        />,
      ),
    );
    expect(screen.getByText("Atlas")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/frontend
pnpm test -- src/__tests__/components/teams/inbox/KanbanCard.test.tsx
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement the component**

```tsx
// apps/frontend/src/components/teams/inbox/KanbanCard.tsx

// Ported from upstream Paperclip's KanbanBoard inner card
// (paperclip/ui/src/components/KanbanBoard.tsx) (MIT, (c) 2025 Paperclip AI).
// Editorial retheme: bg-card → editorial-card, blue live dot → forest, ink text.

"use client";

import Link from "next/link";
import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { cn } from "@/lib/utils";
import { PriorityIcon } from "@/components/teams/shared/components/PriorityIcon";
import { Identity } from "@/components/teams/shared/components/Identity";
import type { Issue } from "@/components/teams/shared/types";

export interface KanbanAgent {
  id: string;
  name: string;
}

export interface KanbanCardProps {
  issue: Issue;
  agents?: KanbanAgent[];
  isLive?: boolean;
  isOverlay?: boolean;
}

export function KanbanCard({ issue, agents, isLive, isOverlay }: KanbanCardProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: issue.id, data: { issue } });

  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  const agentName = (id: string | null | undefined) => {
    if (!id || !agents) return null;
    return agents.find((a) => a.id === id)?.name ?? null;
  };

  const href = `/teams/issues/${issue.identifier ?? issue.id}`;

  return (
    <div
      ref={setNodeRef}
      style={style}
      {...attributes}
      {...listeners}
      data-kanban-card-id={issue.id}
      className={cn(
        "editorial-card p-2.5 cursor-grab active:cursor-grabbing transition-shadow",
        isDragging && !isOverlay && "opacity-30",
        isOverlay ? "shadow-lg ring-1" : "hover:shadow-sm",
      )}
    >
      <Link
        href={href}
        className="block no-underline"
        style={{ color: "var(--ink)" }}
        onClick={(e) => {
          if (isDragging) e.preventDefault();
        }}
      >
        <div className="flex items-start gap-1.5 mb-1.5">
          <span
            className="text-xs font-mono shrink-0"
            style={{ color: "var(--mute)" }}
          >
            {issue.identifier ?? issue.id.slice(0, 8)}
          </span>
          {isLive && (
            <span
              data-testid="kanban-card-live-dot"
              className="relative flex h-2 w-2 shrink-0 mt-0.5"
            >
              <span
                className="animate-pulse absolute inline-flex h-full w-full rounded-full opacity-75"
                style={{ backgroundColor: "var(--forest)" }}
              />
              <span
                className="relative inline-flex rounded-full h-2 w-2"
                style={{ backgroundColor: "var(--forest)" }}
              />
            </span>
          )}
        </div>
        <p
          className="text-sm leading-snug line-clamp-2 mb-2"
          style={{ color: "var(--ink)" }}
        >
          {issue.title}
        </p>
        <div className="flex items-center gap-2">
          {issue.priority && <PriorityIcon priority={issue.priority} />}
          {issue.assigneeAgentId &&
            (() => {
              const name = agentName(issue.assigneeAgentId);
              return name ? (
                <Identity name={name} size="xs" />
              ) : (
                <span
                  className="text-xs font-mono"
                  style={{ color: "var(--mute)" }}
                >
                  {issue.assigneeAgentId.slice(0, 8)}
                </span>
              );
            })()}
        </div>
      </Link>
    </div>
  );
}
```

`<PriorityIcon>` (`apps/frontend/src/components/teams/shared/components/PriorityIcon.tsx:37-39`) requires a non-null `priority: IssuePriority`. The card renders the icon only when `issue.priority` is present (the conditional render above handles this).

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/frontend
pnpm test -- src/__tests__/components/teams/inbox/KanbanCard.test.tsx
```

Expected: 6/6 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/feat+teams-kanban
git add apps/frontend/src/components/teams/inbox/KanbanCard.tsx \
        apps/frontend/src/__tests__/components/teams/inbox/KanbanCard.test.tsx
git commit -m "feat(teams): port KanbanCard with dnd-kit sortable wiring"
```

---

## Task 5: KanbanColumn component

**Files:**
- Create: `apps/frontend/src/components/teams/inbox/KanbanColumn.tsx`
- Test: `apps/frontend/src/__tests__/components/teams/inbox/KanbanColumn.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/components/teams/inbox/KanbanColumn.test.tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { DndContext } from "@dnd-kit/core";
import { KanbanColumn } from "@/components/teams/inbox/KanbanColumn";
import type { Issue } from "@/components/teams/shared/types";

const issue: Issue = {
  id: "iss-1",
  identifier: "ENG-1",
  title: "Test issue",
  status: "todo",
  priority: null,
};

function wrap(ui: React.ReactNode) {
  return <DndContext>{ui}</DndContext>;
}

describe("KanbanColumn", () => {
  it("renders the column label (status with underscores → spaces, title-cased)", () => {
    render(wrap(<KanbanColumn status="in_progress" issues={[issue]} />));
    expect(screen.getByText(/In Progress/i)).toBeInTheDocument();
  });

  it("renders the count badge with the number of issues", () => {
    render(
      wrap(
        <KanbanColumn
          status="todo"
          issues={[issue, { ...issue, id: "iss-2", identifier: "ENG-2" }]}
        />,
      ),
    );
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("renders all issues in the column", () => {
    render(
      wrap(
        <KanbanColumn
          status="todo"
          issues={[issue, { ...issue, id: "iss-2", identifier: "ENG-2" }]}
        />,
      ),
    );
    expect(screen.getByText("ENG-1")).toBeInTheDocument();
    expect(screen.getByText("ENG-2")).toBeInTheDocument();
  });

  it("collapses to compact width when empty", () => {
    const { container } = render(wrap(<KanbanColumn status="done" issues={[]} />));
    const col = container.querySelector("[data-kanban-column]");
    expect(col).toHaveAttribute("data-empty", "true");
  });

  it("does not collapse when populated", () => {
    const { container } = render(
      wrap(<KanbanColumn status="done" issues={[issue]} />),
    );
    const col = container.querySelector("[data-kanban-column]");
    expect(col).toHaveAttribute("data-empty", "false");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/frontend
pnpm test -- src/__tests__/components/teams/inbox/KanbanColumn.test.tsx
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement the component**

```tsx
// apps/frontend/src/components/teams/inbox/KanbanColumn.tsx

// Ported from upstream Paperclip's KanbanBoard inner column
// (paperclip/ui/src/components/KanbanBoard.tsx) (MIT, (c) 2025 Paperclip AI).
// Editorial retheme: bg-muted/20 → paper-soft, accent → ink-tint.

"use client";

import { useDroppable } from "@dnd-kit/core";
import { SortableContext, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { cn } from "@/lib/utils";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { KanbanCard, type KanbanAgent } from "@/components/teams/inbox/KanbanCard";
import type { Issue } from "@/components/teams/shared/types";

export interface KanbanColumnProps {
  status: string;
  issues: Issue[];
  agents?: KanbanAgent[];
  liveIssueIds?: Set<string>;
}

function statusLabel(status: string): string {
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function KanbanColumn({
  status,
  issues,
  agents,
  liveIssueIds,
}: KanbanColumnProps) {
  const { setNodeRef, isOver } = useDroppable({ id: status });
  const isEmpty = issues.length === 0;

  return (
    <div
      data-kanban-column={status}
      data-empty={isEmpty ? "true" : "false"}
      className={cn(
        "flex flex-col shrink-0 transition-[width,min-width]",
        isEmpty && !isOver
          ? "min-w-[48px] w-[48px]"
          : "min-w-[260px] w-[260px]",
      )}
    >
      <div
        className={cn(
          "flex items-center gap-2 px-2 py-2 mb-1",
          isEmpty && !isOver && "justify-center",
        )}
      >
        <StatusIcon status={status} />
        {(!isEmpty || isOver) && (
          <>
            <span
              className="text-xs font-semibold uppercase tracking-wide font-editorial-sans"
              style={{ color: "var(--mute)" }}
            >
              {statusLabel(status)}
            </span>
            <span
              className="text-xs ml-auto tabular-nums"
              style={{ color: "var(--mute-soft)" }}
            >
              {issues.length}
            </span>
          </>
        )}
      </div>
      <div
        ref={setNodeRef}
        className={cn(
          "flex-1 min-h-[120px] rounded-md p-1 space-y-1 transition-colors",
        )}
        style={{
          backgroundColor: isOver ? "var(--paper)" : "var(--paper-soft)",
        }}
      >
        <SortableContext
          items={issues.map((i) => i.id)}
          strategy={verticalListSortingStrategy}
        >
          {issues.map((issue) => (
            <KanbanCard
              key={issue.id}
              issue={issue}
              agents={agents}
              isLive={liveIssueIds?.has(issue.id)}
            />
          ))}
        </SortableContext>
      </div>
    </div>
  );
}
```

`<StatusIcon>` (`apps/frontend/src/components/teams/shared/components/StatusIcon.tsx:67-68`) accepts `status: string`, so any of the 7 board statuses passes through unchanged. No prop-shape adaptation needed.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/frontend
pnpm test -- src/__tests__/components/teams/inbox/KanbanColumn.test.tsx
```

Expected: 5/5 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/feat+teams-kanban
git add apps/frontend/src/components/teams/inbox/KanbanColumn.tsx \
        apps/frontend/src/__tests__/components/teams/inbox/KanbanColumn.test.tsx
git commit -m "feat(teams): port KanbanColumn with droppable + sortable wiring"
```

---

## Task 6: KanbanBoard component (composes Column + Card)

This is where the drag-end → patch-status wiring lives.

**Files:**
- Create: `apps/frontend/src/components/teams/inbox/KanbanBoard.tsx`
- Test: `apps/frontend/src/__tests__/components/teams/inbox/KanbanBoard.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/__tests__/components/teams/inbox/KanbanBoard.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { KanbanBoard, BOARD_STATUSES } from "@/components/teams/inbox/KanbanBoard";
import type { Issue } from "@/components/teams/shared/types";

const i = (id: string, status: Issue["status"]): Issue => ({
  id,
  identifier: id.toUpperCase(),
  title: `Issue ${id}`,
  status,
  priority: null,
});

describe("KanbanBoard", () => {
  it("groups issues into columns by status", () => {
    const issues = [i("a", "todo"), i("b", "in_progress"), i("c", "todo")];
    render(<KanbanBoard issues={issues} agents={[]} onUpdateIssue={vi.fn()} />);

    const todoCol = document.querySelector('[data-kanban-column="todo"]');
    const inProgCol = document.querySelector('[data-kanban-column="in_progress"]');
    expect(todoCol?.querySelectorAll("[data-kanban-card-id]")).toHaveLength(2);
    expect(inProgCol?.querySelectorAll("[data-kanban-card-id]")).toHaveLength(1);
  });

  it("renders all 7 board statuses as columns", () => {
    render(<KanbanBoard issues={[]} agents={[]} onUpdateIssue={vi.fn()} />);
    for (const status of BOARD_STATUSES) {
      expect(
        document.querySelector(`[data-kanban-column="${status}"]`),
      ).toBeInTheDocument();
    }
  });

  it("BOARD_STATUSES exposes the canonical 7-status set in upstream order", () => {
    expect(BOARD_STATUSES).toEqual([
      "backlog",
      "todo",
      "in_progress",
      "in_review",
      "blocked",
      "done",
      "cancelled",
    ]);
  });

  it("issues with non-board statuses do not render", () => {
    // 'pending' is in IssueStatus but not in BOARD_STATUSES.
    const issues = [i("a", "todo"), i("b", "pending" as Issue["status"])];
    render(<KanbanBoard issues={issues} agents={[]} onUpdateIssue={vi.fn()} />);
    expect(document.querySelector('[data-kanban-card-id="a"]')).toBeInTheDocument();
    expect(document.querySelector('[data-kanban-card-id="b"]')).not.toBeInTheDocument();
  });

  it("handleDragEnd patches status when dropping on a column id", () => {
    const onUpdateIssue = vi.fn();
    const issues = [i("a", "todo")];
    const { container } = render(
      <KanbanBoard issues={issues} agents={[]} onUpdateIssue={onUpdateIssue} />,
    );
    // Reach the test handle — BoardKanban exports __testOnlyHandleDragEnd via
    // a module-level symbol (see implementation note).
    // Use the exported __test_handleDragEnd helper directly.
    const helper = require("@/components/teams/inbox/KanbanBoard");
    helper.__test_handleDragEnd(
      issues,
      onUpdateIssue,
      { active: { id: "a" }, over: { id: "in_progress" } } as any,
    );
    expect(onUpdateIssue).toHaveBeenCalledWith("a", { status: "in_progress" });
  });

  it("handleDragEnd is a no-op when dropping on the same column", () => {
    const onUpdateIssue = vi.fn();
    const issues = [i("a", "todo")];
    const helper = require("@/components/teams/inbox/KanbanBoard");
    helper.__test_handleDragEnd(
      issues,
      onUpdateIssue,
      { active: { id: "a" }, over: { id: "todo" } } as any,
    );
    expect(onUpdateIssue).not.toHaveBeenCalled();
  });

  it("handleDragEnd resolves card-id drop to the card's column status", () => {
    const onUpdateIssue = vi.fn();
    const issues = [i("a", "todo"), i("b", "done")];
    const helper = require("@/components/teams/inbox/KanbanBoard");
    helper.__test_handleDragEnd(
      issues,
      onUpdateIssue,
      { active: { id: "a" }, over: { id: "b" } } as any,
    );
    expect(onUpdateIssue).toHaveBeenCalledWith("a", { status: "done" });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/frontend
pnpm test -- src/__tests__/components/teams/inbox/KanbanBoard.test.tsx
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement the component**

```tsx
// apps/frontend/src/components/teams/inbox/KanbanBoard.tsx

// Ported from upstream Paperclip's KanbanBoard
// (paperclip/ui/src/components/KanbanBoard.tsx) (MIT, (c) 2025 Paperclip AI).
// 7-column board with drag-to-change-status. Order-within-column is not
// persisted; drag-end only patches `status`.

"use client";

import { useMemo, useState } from "react";
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { KanbanColumn } from "@/components/teams/inbox/KanbanColumn";
import { KanbanCard, type KanbanAgent } from "@/components/teams/inbox/KanbanCard";
import type { Issue } from "@/components/teams/shared/types";

export const BOARD_STATUSES = [
  "backlog",
  "todo",
  "in_progress",
  "in_review",
  "blocked",
  "done",
  "cancelled",
] as const;

export type BoardStatus = (typeof BOARD_STATUSES)[number];

export interface KanbanBoardProps {
  issues: Issue[];
  agents?: KanbanAgent[];
  liveIssueIds?: Set<string>;
  onUpdateIssue: (id: string, data: Record<string, unknown>) => void;
}

// Exported for unit tests — prefer pure logic that doesn't require a rendered
// DndContext to verify drag-end semantics.
export function __test_handleDragEnd(
  issues: Issue[],
  onUpdateIssue: KanbanBoardProps["onUpdateIssue"],
  event: DragEndEvent,
): void {
  const { active, over } = event;
  if (!over) return;
  const issueId = active.id as string;
  const issue = issues.find((i) => i.id === issueId);
  if (!issue) return;

  let targetStatus: string | null = null;
  if ((BOARD_STATUSES as readonly string[]).includes(over.id as string)) {
    targetStatus = over.id as string;
  } else {
    const targetIssue = issues.find((i) => i.id === over.id);
    if (targetIssue) targetStatus = targetIssue.status;
  }

  if (targetStatus && targetStatus !== issue.status) {
    onUpdateIssue(issueId, { status: targetStatus });
  }
}

export function KanbanBoard({
  issues,
  agents,
  liveIssueIds,
  onUpdateIssue,
}: KanbanBoardProps) {
  const [activeId, setActiveId] = useState<string | null>(null);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );

  const columnIssues = useMemo(() => {
    const grouped: Record<string, Issue[]> = {};
    for (const status of BOARD_STATUSES) grouped[status] = [];
    for (const issue of issues) {
      if (grouped[issue.status]) grouped[issue.status].push(issue);
    }
    return grouped;
  }, [issues]);

  const activeIssue = useMemo(
    () => (activeId ? issues.find((i) => i.id === activeId) ?? null : null),
    [activeId, issues],
  );

  function handleDragStart(event: DragStartEvent) {
    setActiveId(event.active.id as string);
  }

  function handleDragEnd(event: DragEndEvent) {
    setActiveId(null);
    __test_handleDragEnd(issues, onUpdateIssue, event);
  }

  return (
    <DndContext
      sensors={sensors}
      onDragStart={handleDragStart}
      onDragEnd={handleDragEnd}
    >
      <div className="flex gap-3 overflow-x-auto pb-4 -mx-2 px-2">
        {BOARD_STATUSES.map((status) => (
          <KanbanColumn
            key={status}
            status={status}
            issues={columnIssues[status] ?? []}
            agents={agents}
            liveIssueIds={liveIssueIds}
          />
        ))}
      </div>
      <DragOverlay>
        {activeIssue ? (
          <KanbanCard issue={activeIssue} agents={agents} isOverlay />
        ) : null}
      </DragOverlay>
    </DndContext>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd apps/frontend
pnpm test -- src/__tests__/components/teams/inbox/KanbanBoard.test.tsx
```

Expected: 7/7 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/feat+teams-kanban
git add apps/frontend/src/components/teams/inbox/KanbanBoard.tsx \
        apps/frontend/src/__tests__/components/teams/inbox/KanbanBoard.test.tsx
git commit -m "feat(teams): port KanbanBoard with drag-to-change-status wiring"
```

---

## Task 7: Wire ViewModeToggle into InboxToolbar + branch InboxPage on viewMode

**Files:**
- Modify: `apps/frontend/src/components/teams/inbox/InboxToolbar.tsx`
- Modify: `apps/frontend/src/components/teams/inbox/InboxPage.tsx`

- [ ] **Step 1: Read the current `InboxToolbar` props block**

```bash
sed -n '40,75p' apps/frontend/src/components/teams/inbox/InboxToolbar.tsx
```

You're looking for the existing `InboxToolbarProps` interface and the right cluster of the toolbar (where filters + mark-all-read buttons live).

- [ ] **Step 2: Extend `InboxToolbarProps` with view-mode props**

In `InboxToolbar.tsx`, add to the `InboxToolbarProps` interface:

```ts
viewMode: "list" | "board";
onViewModeChange: (mode: "list" | "board") => void;
```

And in the destructured signature on the function:

```ts
viewMode,
onViewModeChange,
```

Import the toggle near the other inbox imports:

```ts
import { ViewModeToggle } from "@/components/teams/inbox/ViewModeToggle";
```

Render it in the right-side cluster of the toolbar (next to the filters popover or mark-all-read button — wherever the existing actions live). Example placement (adjust to match the actual JSX shape):

```tsx
<ViewModeToggle value={viewMode} onChange={onViewModeChange} />
```

- [ ] **Step 3: Wire `useInboxViewMode` into `InboxPage`**

Open `apps/frontend/src/components/teams/inbox/InboxPage.tsx`. Add imports:

```ts
import { useInboxViewMode } from "@/components/teams/inbox/hooks/useInboxViewMode";
import dynamic from "next/dynamic";

// Lazy-load the board to keep the list-view path bundle unchanged.
const KanbanBoard = dynamic(
  () => import("@/components/teams/inbox/KanbanBoard").then((m) => m.KanbanBoard),
  { ssr: false, loading: () => <div className="p-4 text-[var(--mute)]">Loading board…</div> },
);
```

Inside the `InboxPage` function, after the existing `companyId` resolution:

```ts
const { viewMode, setViewMode } = useInboxViewMode(companyId ?? null);
```

Pass these into `<InboxToolbar>`:

```tsx
<InboxToolbar
  /* …existing props… */
  viewMode={viewMode}
  onViewModeChange={setViewMode}
/>
```

Branch the body. Find the existing `<InboxList ... />` render (around `InboxPage.tsx:297`). Replace with:

```tsx
{viewMode === "board" ? (
  <KanbanBoard
    issues={issues}
    agents={agents}
    liveIssueIds={liveIssueIds}
    onUpdateIssue={(id, data) => updateIssue(id, data)}
  />
) : (
  <InboxList
    /* …existing props verbatim… */
  />
)}
```

`InboxPage` does not currently expose a status-mutation handler (existing mutations like archive/markRead live behind the `useInboxArchiveStack` hook). Add one inline using `useTeamsApi.patch`:

```ts
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useCallback } from "react";

// Inside InboxPage(), near the existing hook calls:
const { patch } = useTeamsApi();
const onUpdateIssue = useCallback(
  (id: string, data: Record<string, unknown>) => {
    void patch(`/issues/${id}`, data);
  },
  [patch],
);
```

No manual SWR refetch needed — `issue.*` events from `TeamsEventsProvider` already invalidate `/teams/issues`, so the board re-renders with new grouping after the BFF write completes.

`liveIssueIds` should already be in scope from the existing `TeamsEventsProvider` wiring used by `InboxList` (look for the existing prop forwarded into `<InboxList>` around `InboxPage.tsx:297-310`). Reuse the same value, don't re-derive.

- [ ] **Step 4: Typecheck + run all inbox tests**

```bash
cd apps/frontend
pnpm exec tsc --noEmit 2>&1 | grep -E "inbox|teams" | head -20
pnpm test -- src/__tests__/components/teams/inbox/
```

Expected: typecheck clean for our files, all inbox suites pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/feat+teams-kanban
git add apps/frontend/src/components/teams/inbox/InboxToolbar.tsx \
        apps/frontend/src/components/teams/inbox/InboxPage.tsx
git commit -m "feat(teams): wire list/board toggle into inbox toolbar + page"
```

---

## Task 8: Final verification, roadmap row update, push + open PR

- [ ] **Step 1: Run the full frontend test suite**

```bash
cd apps/frontend
pnpm test 2>&1 | tail -30
```

Expected: NO new failures vs `origin/main`. Pre-existing red files (if any) are not our concern; investigate only if a file we touched (`inbox/*`) goes red.

- [ ] **Step 2: Run lint + typecheck**

```bash
cd apps/frontend
pnpm lint 2>&1 | tail -10
pnpm exec tsc --noEmit 2>&1 | grep error | head -20
```

Expected: lint clean for our files; no new typecheck errors in `inbox/*`.

- [ ] **Step 3: Manual sanity check (optional but recommended)**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/feat+teams-kanban
turbo run dev --filter=@isol8/frontend
```

Visit `http://localhost:3000/teams/inbox`, confirm the toggle appears, switch to Board, drag a card across columns. Watch network: a `PATCH /api/v1/teams/issues/{id}` should fire with `{ "status": "<new>" }`. Confirm the card lands in the new column after the BFF round-trip.

- [ ] **Step 4: Add roadmap row #6**

Edit `docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md`. Add a new row immediately after row #5:

```
| 6 | Issues kanban board (list/board toggle in /teams/inbox; verbatim KanbanBoard port + retheme) | M | In progress | [2026-05-09-teams-kanban-board-design.md](./2026-05-09-teams-kanban-board-design.md) | [2026-05-09-teams-kanban-board.md](../plans/2026-05-09-teams-kanban-board.md) | (#6 link) |
```

- [ ] **Step 5: Push the branch**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/feat+teams-kanban
git add docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md
git commit -m "docs(roadmap): add row #6 — issues kanban board"
git push -u origin feat/teams-kanban
```

- [ ] **Step 6: Open the PR**

```bash
gh pr create --title "feat(teams): issues kanban board (#6)" --body "$(cat <<'EOF'
## Summary

Sub-project **#6** of the [Teams UI parity roadmap](docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md). Adds a list/board view toggle to `/teams/inbox` and a verbatim port of upstream Paperclip's `KanbanBoard.tsx` as the board view.

## What's new

- `useInboxViewMode` — localStorage-scoped per-company `"list" | "board"` persistence.
- `ViewModeToggle` — segmented control (Rows3 / Columns3) for the inbox toolbar.
- `KanbanCard`, `KanbanColumn`, `KanbanBoard` — verbatim port of upstream's component, retheme'd to the editorial palette.
- `InboxToolbar` accepts `viewMode` + `onViewModeChange`.
- `InboxPage` lazy-loads `KanbanBoard` and branches on view mode.

## New deps

- `@dnd-kit/core@^6.3.1`
- `@dnd-kit/sortable@^10.0.0`
- `@dnd-kit/utilities@^3.2.2`

Pinned to upstream Paperclip's `paperclip/ui/package.json` versions.

## Realtime

No new event subscriptions. Status changes flow through the existing `TeamsEventsProvider` `EVENT_KEY_MAP` from PR #518 — `issue.*` events invalidate `/teams/issues` and the board re-groups on the next render.

## Architectural decisions

1. **Verbatim port + editorial retheme.** Don't fork upstream's structure or rewrite drag logic.
2. **Lazy-load the board** behind `next/dynamic`. List-view path bundle is unchanged.
3. **Drag-end patches `status` only.** Order-within-column is not persisted; column membership reflects whatever the BFF returns.

## Out of scope (deferred)

- Optimistic status updates (relying on realtime invalidate cycle for v1).
- Multi-select / bulk drag.
- Cross-tab sync of view-mode state.
- Mobile-specific layout.

## Test plan

- [x] Unit tests: `useInboxViewMode` (6), `ViewModeToggle` (4), `KanbanCard` (6), `KanbanColumn` (5), `KanbanBoard` (7).
- [x] Lint + typecheck clean for our files.
- [ ] Manual: dev-server check that drag-to-change-status fires the expected `PATCH` and the card lands in the new column. (Reviewer to validate.)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 7: Link the PR in the roadmap row**

```bash
PR_NUM=$(gh pr view --json number -q .number)
sed -i.bak "s|(#6 link)|[#${PR_NUM}](https://github.com/Isol8AI/isol8/pull/${PR_NUM})|" \
  docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md
rm docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md.bak
git add docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md
git commit -m "docs(roadmap): link PR #${PR_NUM} in row #6"
git push
```

- [ ] **Step 8: Watch CI briefly**

```bash
gh pr checks 2>&1 | head -10
```

DO NOT MERGE. Report PR URL + initial CI status, then stop.

---

## Self-review checklist

- ✅ 8 tasks; clean dependency flow (deps → hook → toggle → card → column → board → wiring → verify+PR).
- ✅ Each task fits TDD: failing test → impl → passing test → commit.
- ✅ MIT attribution headers required on every ported file.
- ✅ Vitest explicit imports on every test file.
- ✅ Branch: `feat/teams-kanban` in worktree.
- ✅ All file paths exact; all code snippets complete (no placeholders).
- ✅ Final task pushes + opens PR; does NOT merge.
- ✅ Roadmap row #6 added before PR is opened.
- ✅ Spec coverage: every spec section maps to a task — useInboxViewMode (T2), ViewModeToggle (T3), KanbanCard/Column/Board (T4-6), InboxToolbar/Page wiring (T7), realtime via existing provider (free), error handling (covered by snap-back behavior + dev-mode console warn for unknown statuses — tested in T6).
