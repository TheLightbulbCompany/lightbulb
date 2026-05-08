# Teams Costs Atoms Port (PR 6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `CostsPanel.tsx` (88 LOC) to compose editorial atoms — masthead + KPI card + by-agent + by-project lists.

**Architecture:** Frontend-only. BFF surface is `GET /costs` returning `{total_cents, by_agent[], by_project[], period}`. No backend changes — the spec mentioned query params + budgets, but: (1) the existing UI doesn't surface filters, (2) budgets aren't implemented anywhere in our stack, (3) the user said "I am not sure if usage is controlled here through Paperclip so maybe we shouldnt have it?" — so keep this PR minimal.

**Tech Stack:** React 19, vitest, editorial atoms.

**Branch:** `plan/teams-costs-atoms-port`.

---

## File Structure

**Modify:**
- `apps/frontend/src/components/teams/panels/CostsPanel.tsx`

**Add tests:**
- `apps/frontend/tests/unit/components/teams/panels/CostsPanel.test.tsx`

---

## Task 0: Setup

- [ ] **Step 1: Branch + base**

```sh
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix
git checkout main && git pull origin main
git checkout -b plan/teams-costs-atoms-port
```

- [ ] **Step 2: Commit the plan first** (PR 1/PR 2 pattern)

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add docs/superpowers/plans/2026-05-07-teams-costs-atoms-port.md
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "plan(teams): PR 6 — Costs atoms port implementation steps"
```

---

## Task 1: Refactor `CostsPanel.tsx`

- [ ] **Step 1: Write atom-contract test**

`apps/frontend/tests/unit/components/teams/panels/CostsPanel.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { CostsPanel } from "@/components/teams/panels/CostsPanel";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({
      data: {
        total_cents: 1234,
        period: "April 2026",
        by_agent: [{ agent_id: "a1", agent_name: "Goose", cents: 800 }],
        by_project: [],
      },
      isLoading: false,
      error: null,
    }),
  }),
}));

describe("CostsPanel atom contract", () => {
  it("renders PanelMasthead with overline + h1 + meta period", () => {
    render(<CostsPanel />);
    expect(screen.getByRole("heading", { level: 1, name: /costs/i })).toBeInTheDocument();
    expect(screen.getByText(/april 2026/i)).toBeInTheDocument();
  });

  it("renders total in a KPI card", () => {
    render(<CostsPanel />);
    expect(screen.getByText(/\$12\.34/)).toBeInTheDocument();
  });

  it("renders by-agent breakdown", () => {
    render(<CostsPanel />);
    expect(screen.getByText("Goose")).toBeInTheDocument();
    expect(screen.getByText(/\$8\.00/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Verify failing**

```sh
cd apps/frontend && pnpm test CostsPanel -- --run
```

- [ ] **Step 3: Refactor**

```tsx
"use client";

import { Receipt } from "lucide-react";
import {
  PanelMasthead,
  PanelEmptyState,
  PanelDataTable,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface CostBreakdown {
  total_cents?: number;
  by_agent?: Array<{ agent_id: string; agent_name?: string; cents: number }>;
  by_project?: Array<{ project_id: string; project_name?: string; cents: number }>;
  period?: string;
}

function fmt(cents: number) {
  return `$${(cents / 100).toFixed(2)}`;
}

type AgentRow = NonNullable<CostBreakdown["by_agent"]>[number];
type ProjectRow = NonNullable<CostBreakdown["by_project"]>[number];

export function CostsPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<CostBreakdown>("/costs");

  if (error) {
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );
  }

  const totalCents = data?.total_cents ?? 0;
  const byAgent = data?.by_agent ?? [];
  const byProject = data?.by_project ?? [];
  const hasData = !isLoading && (totalCents > 0 || byAgent.length || byProject.length);

  const agentColumns: PanelDataColumn<AgentRow>[] = [
    { key: "agent", header: "Agent", cell: (r) => r.agent_name ?? r.agent_id, sortable: true },
    {
      key: "cents",
      header: "Cost",
      align: "right",
      cell: (r) => <span className="font-mono">{fmt(r.cents)}</span>,
    },
  ];
  const projectColumns: PanelDataColumn<ProjectRow>[] = [
    { key: "project", header: "Project", cell: (r) => r.project_name ?? r.project_id, sortable: true },
    {
      key: "cents",
      header: "Cost",
      align: "right",
      cell: (r) => <span className="font-mono">{fmt(r.cents)}</span>,
    },
  ];

  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Spend"
        title="Costs"
        meta={data?.period ? `Period · ${data.period}` : undefined}
      />

      {!hasData ? (
        <PanelEmptyState
          icon={Receipt}
          headline="No spend recorded."
          supporting="Once your agents start running, this dashboard will summarize spend by agent and by project."
        />
      ) : (
        <>
          <div className="editorial-card p-5">
            <div className="text-xs uppercase tracking-wide text-[var(--mute)]">Total</div>
            <div className="font-editorial-serif text-4xl mt-1 text-[var(--ink)]">
              {fmt(totalCents)}
            </div>
          </div>

          {byAgent.length > 0 && (
            <section className="flex flex-col gap-3">
              <h2 className="font-editorial-serif text-2xl text-[var(--ink)]">By agent</h2>
              <PanelDataTable<AgentRow>
                rows={byAgent}
                columns={agentColumns}
                rowKey={(r) => r.agent_id}
              />
            </section>
          )}

          {byProject.length > 0 && (
            <section className="flex flex-col gap-3">
              <h2 className="font-editorial-serif text-2xl text-[var(--ink)]">By project</h2>
              <PanelDataTable<ProjectRow>
                rows={byProject}
                columns={projectColumns}
                rowKey={(r) => r.project_id}
              />
            </section>
          )}
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Verify pass + commit**

```sh
cd apps/frontend && pnpm test CostsPanel -- --run
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add \
  apps/frontend/src/components/teams/panels/CostsPanel.tsx \
  apps/frontend/tests/unit/components/teams/panels/CostsPanel.test.tsx
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "feat(teams-costs): CostsPanel composes editorial atoms"
```

---

## Task 2: Final verify + push + open PR

- [ ] **Step 1: Full tests + lint + tsc**

```sh
cd apps/frontend && pnpm test 2>&1 | tail -10
cd apps/frontend && pnpm lint && pnpm tsc --noEmit
```

- [ ] **Step 2: Push + PR**

```sh
git -C ... push -u origin plan/teams-costs-atoms-port
gh pr create --title "feat(teams-costs): port to editorial atom layer (PR 6 of /teams port)" --body "$(cat <<'EOF'
## Summary

Sixth PR of the multi-step /teams panel port. Refactors `CostsPanel.tsx` (88 LOC) to compose editorial atoms: masthead + KPI card + by-agent + by-project tables.

## What changed

- `<PanelMasthead>` with period meta
- KPI card for total
- `<PanelDataTable>` for agent + project breakdowns
- `<PanelEmptyState>` for the no-spend case

## Out of scope

- Period filter / date range query params (deferred — usage may not be Paperclip-driven anyway)
- Budgets (not implemented in our stack)

## Test plan

- [x] pnpm test atom-contract guards
- [x] tsc + lint clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
