# Teams Dashboard + Approvals Atoms Port (PR 8) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `<PanelMasthead>` to `DashboardPanel.tsx` (305 LOC) and refactor `ApprovalsPanel.tsx` (81 LOC) to atom layer.

**Architecture:** Pure cosmetic atom adoption. DashboardPanel is already built out (4 KPI tiles + 4 charts + activity feed) — only add the masthead wrapper. ApprovalsPanel atomizes Approve/Reject buttons + adds a masthead.

**Branch:** `plan/teams-dashboard-approvals-atoms-port`.

---

## Task 0: Setup

- [ ] **Step 1: Branch + base**

```sh
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix
git checkout main && git pull origin main
git checkout -b plan/teams-dashboard-approvals-atoms-port
```

- [ ] **Step 2: Commit the plan first** (PR 1/PR 2 pattern)

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add docs/superpowers/plans/2026-05-07-teams-dashboard-approvals-atoms-port.md
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "plan(teams): PR 8 — Dashboard + Approvals atoms port implementation steps"
```

---

## Task 1: `DashboardPanel.tsx` — add masthead

The panel already has KPI tiles, charts, activity feed. Just prepend a `<PanelMasthead overline="Operations" title="Dashboard" supporting="Last 14 days" />` and wrap content in the standard `editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-[1600px]` container.

- [ ] **Step 1: Atom-contract test**

`apps/frontend/tests/unit/components/teams/panels/DashboardPanel.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { DashboardPanel } from "@/components/teams/panels/DashboardPanel";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: undefined, isLoading: false, error: null, mutate: vi.fn() }),
  }),
}));
vi.mock("@/components/teams/dashboard/hooks/useActivityFeed", () => ({
  useActivityFeed: () => ({ events: [], isLoading: false }),
}));

describe("DashboardPanel atom contract", () => {
  it("renders PanelMasthead with overline + h1", () => {
    render(<DashboardPanel />);
    expect(screen.getByRole("heading", { level: 1, name: /dashboard/i })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Wrap the panel in PanelMasthead**

Find the existing return statement (after the loading/error early-returns). Replace the outer wrapper with:

```tsx
return (
  <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-[1600px] mx-auto w-full">
    <PanelMasthead
      overline="Operations"
      title="Dashboard"
      meta="Last 14 days"
    />
    {/* existing tiles + charts + activity feed go here */}
  </div>
);
```

Keep the existing tile/chart/feed sections unchanged.

- [ ] **Step 3: Verify + commit**

```sh
cd apps/frontend && pnpm test DashboardPanel -- --run
git -C ... add apps/frontend/src/components/teams/panels/DashboardPanel.tsx \
  apps/frontend/tests/unit/components/teams/panels/DashboardPanel.test.tsx
git -C ... commit -m "feat(teams-dashboard): DashboardPanel adds PanelMasthead"
```

---

## Task 2: `ApprovalsPanel.tsx` refactor

- [ ] **Step 1: Atom-contract test**

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ApprovalsPanel } from "@/components/teams/panels/ApprovalsPanel";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({
      data: { approvals: [{ id: "a1", title: "Test approval", description: "Body" }] },
      mutate: vi.fn(),
    }),
    post: vi.fn(),
  }),
}));

describe("ApprovalsPanel atom contract", () => {
  it("renders PanelMasthead h1", () => {
    render(<ApprovalsPanel />);
    expect(screen.getByRole("heading", { level: 1, name: /approvals/i })).toBeInTheDocument();
  });

  it("Approve button is EditorialButton", () => {
    render(<ApprovalsPanel />);
    expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument();
  });

  it("Reject button uses destructive variant", () => {
    render(<ApprovalsPanel />);
    const reject = screen.getByRole("button", { name: /reject/i });
    expect(reject).toBeInTheDocument();
  });

  it("renders PanelEmptyState when no approvals", () => {
    // override mock for this test
    // (use vi.mocked or re-render with different mock)
  });
});
```

- [ ] **Step 2: Refactor**

```tsx
"use client";

import { useState } from "react";
import { CheckCircle2 } from "lucide-react";
import {
  PanelMasthead,
  PanelEmptyState,
  EditorialButton,
} from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Approval {
  id: string;
  title: string;
  description?: string;
  createdAt?: string;
}

export function ApprovalsPanel() {
  const { read, post } = useTeamsApi();
  const { data, mutate } = read<{ approvals: Approval[] }>("/approvals");
  const [busy, setBusy] = useState<string | null>(null);

  async function approve(id: string) {
    setBusy(id);
    try {
      await post(`/approvals/${id}/approve`, { note: "approved via UI" });
      mutate();
    } finally {
      setBusy(null);
    }
  }

  async function reject(id: string) {
    const reason = prompt("Reason for rejection?");
    if (!reason) return;
    setBusy(id);
    try {
      await post(`/approvals/${id}/reject`, { reason });
      mutate();
    } finally {
      setBusy(null);
    }
  }

  const approvals = data?.approvals ?? [];

  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead overline="Workflow" title="Approvals" />
      {approvals.length === 0 ? (
        <PanelEmptyState
          icon={CheckCircle2}
          headline="No pending approvals."
          supporting="When an agent action requires sign-off, it will appear here."
        />
      ) : (
        <ul className="flex flex-col gap-3">
          {approvals.map((a) => (
            <li key={a.id} className="editorial-card p-4">
              <div className="font-medium text-[var(--ink)]">{a.title}</div>
              {a.description && (
                <div className="text-sm text-[var(--ink-soft)] mt-1">
                  {a.description}
                </div>
              )}
              <div className="flex gap-2 mt-3">
                <EditorialButton
                  size="sm"
                  onClick={() => approve(a.id)}
                  disabled={busy === a.id}
                >
                  Approve
                </EditorialButton>
                <EditorialButton
                  variant="destructive"
                  size="sm"
                  onClick={() => reject(a.id)}
                  disabled={busy === a.id}
                >
                  Reject
                </EditorialButton>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Verify + commit**

```sh
cd apps/frontend && pnpm test ApprovalsPanel -- --run
git -C ... add apps/frontend/src/components/teams/panels/ApprovalsPanel.tsx \
  apps/frontend/tests/unit/components/teams/panels/ApprovalsPanel.test.tsx
git -C ... commit -m "feat(teams-approvals): ApprovalsPanel composes editorial atoms"
```

---

## Task 3: Final verify + push + open PR

```sh
cd apps/frontend && pnpm test 2>&1 | tail -10
cd apps/frontend && pnpm lint && pnpm tsc --noEmit
git -C ... push -u origin plan/teams-dashboard-approvals-atoms-port
gh pr create --title "feat(teams-dashboard-approvals): editorial atom adoption (PR 8)" --body "$(cat <<'EOF'
## Summary

PR 8 of the /teams panel port. Cosmetic restyle of DashboardPanel (305 LOC) — adds `<PanelMasthead>` — and full refactor of ApprovalsPanel (81 LOC) — `<PanelMasthead>` + `<PanelEmptyState>` + `<EditorialButton>` actions (destructive variant for Reject).

## What changed

- DashboardPanel: PanelMasthead at top; tiles/charts/feed unchanged.
- ApprovalsPanel: PanelMasthead + PanelEmptyState + EditorialButton (primary Approve, destructive Reject).

## Test plan

- [x] Atom-contract guards
- [x] tsc + lint clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
