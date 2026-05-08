# Teams Agents + Org Chart Atoms Port (PR 9) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `AgentsListPanel.tsx` (160 LOC), `AgentDetailPanel.tsx` (93 LOC), and `OrgChartPanel.tsx` (57 LOC) to compose editorial atoms.

**Branch:** `plan/teams-agents-orgchart-atoms-port`.

---

## Audit summary

- AgentsListPanel: list + create dialog. Same shape as ProjectsListPanel/IssuesPanel.
- AgentDetailPanel: tabs (overview/runs/config). Use `<PanelTabs>`.
- OrgChartPanel: thin wrapper around `<OrgChart>`. Just add `<PanelMasthead>`.
- BFF: `GET /agents`, `POST /agents`, `GET /agents/{id}`, `GET /agents/{id}/runs`. No DELETE. Spec mentioned restart/pause routes — defer (not in our adapter model).

---

## Task 0: Setup

- [ ] **Step 1: Branch + base**

```sh
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix
git checkout main && git pull origin main
git checkout -b plan/teams-agents-orgchart-atoms-port
```

- [ ] **Step 2: Commit the plan first** (PR 1/PR 2 pattern)

```sh
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix add docs/superpowers/plans/2026-05-07-teams-agents-orgchart-atoms-port.md
git -C /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/paperclip-prod-bootstrap-fix commit -m "plan(teams): PR 9 — Agents + Org chart atoms port implementation steps"
```

---

## Task 1: `AgentsListPanel.tsx` refactor

**Files:**
- Modify: `apps/frontend/src/components/teams/panels/AgentsListPanel.tsx`
- Test: `apps/frontend/tests/unit/components/teams/panels/AgentsListPanel.test.tsx`

- [ ] **Step 1: Atom-contract test**

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { AgentsListPanel } from "@/components/teams/panels/AgentsListPanel";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: { agents: [] }, isLoading: false, error: null, mutate: vi.fn() }),
    post: vi.fn(),
  }),
}));

describe("AgentsListPanel atom contract", () => {
  it("renders PanelMasthead + PanelToolbar + New-agent CTA", () => {
    render(<AgentsListPanel />);
    expect(screen.getByRole("heading", { level: 1, name: /agents/i })).toBeInTheDocument();
    expect(screen.getByRole("searchbox")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /new agent/i })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Refactor**

Same shape as ProjectsListPanel (PR 7 Task 2). `<PanelMasthead overline="Team" title="Agents" actions={<EditorialButton onClick=...>New agent</EditorialButton>} />` + `<PanelToolbar>` + `<PanelDataTable>` (columns: name/role/Open) + `<PanelEmptyState icon={Bot} headline="No agents yet." supporting="Spin up your first agent — assign a role + start delegating work." />`. CreateAgentDialog → `<EditorialDialog>` + `<EditorialButton>` actions. **Keep the security comment about not exposing adapter fields.**

- [ ] **Step 3: Verify + commit**

```sh
cd apps/frontend && pnpm test AgentsListPanel -- --run
git -C ... add apps/frontend/src/components/teams/panels/AgentsListPanel.tsx \
  apps/frontend/tests/unit/components/teams/panels/AgentsListPanel.test.tsx
git -C ... commit -m "feat(teams-agents): AgentsListPanel composes editorial atoms"
```

---

## Task 2: `AgentDetailPanel.tsx` refactor

- [ ] **Step 1: Atom-contract test**

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { AgentDetailPanel } from "@/components/teams/panels/AgentDetailPanel";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) => {
      if (path === "/agents/a1")
        return { data: { id: "a1", name: "Goose", role: "engineer" }, isLoading: false };
      if (path === "/agents/a1/runs")
        return { data: { runs: [] }, isLoading: false };
      return { data: undefined };
    },
  }),
}));

describe("AgentDetailPanel atom contract", () => {
  it("renders PanelMasthead with agent name", () => {
    render(<AgentDetailPanel agentId="a1" />);
    expect(screen.getByRole("heading", { level: 1, name: /goose/i })).toBeInTheDocument();
  });

  it("renders PanelTabs with overview/runs/config", () => {
    render(<AgentDetailPanel agentId="a1" />);
    expect(screen.getByRole("tab", { name: /overview/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /runs/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /config/i })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Refactor**

```tsx
"use client";

import { useState } from "react";
import { PanelMasthead, PanelTabs } from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Agent {
  id: string;
  name: string;
  role: string;
}

interface RunRow {
  id: string;
  status: string;
  startedAt?: string;
}

const TABS = [
  { value: "overview", label: "Overview" },
  { value: "runs", label: "Runs" },
  { value: "config", label: "Config" },
];

export function AgentDetailPanel({ agentId }: { agentId: string }) {
  const { read } = useTeamsApi();
  const { data: agent } = read<Agent>(`/agents/${agentId}`);
  const { data: runs } = read<{ runs: RunRow[] }>(`/agents/${agentId}/runs`);
  const [tab, setTab] = useState<string>("overview");

  if (!agent) {
    return (
      <div className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--mute)]">
        Loading…
      </div>
    );
  }

  const runRows = runs?.runs ?? [];

  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Agent"
        title={agent.name}
        meta={agent.role.toUpperCase()}
      />
      <PanelTabs tabs={TABS} value={tab} onValueChange={setTab} />

      {tab === "overview" && (
        <pre className="editorial-card text-xs p-4 whitespace-pre-wrap text-[var(--ink-soft)] bg-[var(--cream-deeper)]">
          {JSON.stringify(agent, null, 2)}
        </pre>
      )}
      {tab === "runs" && (
        <ul className="editorial-card divide-y divide-[var(--edge)] overflow-hidden">
          {runRows.map((r) => (
            <li key={r.id} className="p-3 flex justify-between items-center">
              <span className="text-[var(--ink)]">{r.status}</span>
              <a
                href={`/teams/agents/${agentId}/runs/${r.id}`}
                className="text-sm text-[var(--ink-soft)] hover:underline"
              >
                Open →
              </a>
            </li>
          ))}
          {runRows.length === 0 && (
            <li className="p-3 font-editorial-serif text-xl italic text-[var(--mute)]">
              No runs yet.
            </li>
          )}
        </ul>
      )}
      {tab === "config" && (
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
          Adapter configuration is managed by Isol8 and cannot be edited here.
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Verify + commit**

```sh
cd apps/frontend && pnpm test AgentDetailPanel -- --run
git -C ... add apps/frontend/src/components/teams/panels/AgentDetailPanel.tsx \
  apps/frontend/tests/unit/components/teams/panels/AgentDetailPanel.test.tsx
git -C ... commit -m "feat(teams-agents): AgentDetailPanel composes editorial atoms"
```

---

## Task 3: `OrgChartPanel.tsx` refactor

- [ ] **Step 1: Atom-contract test**

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { OrgChartPanel } from "@/components/teams/panels/OrgChartPanel";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: { agents: [] }, isLoading: false, error: null }),
  }),
}));

describe("OrgChartPanel atom contract", () => {
  it("renders PanelMasthead h1", () => {
    render(<OrgChartPanel />);
    expect(screen.getByRole("heading", { level: 1, name: /org chart/i })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Refactor**

```tsx
"use client";

import { PanelMasthead } from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { OrgChart } from "@/components/teams/org-chart/OrgChart";
import type { OrgChartAgent } from "@/components/teams/org-chart/orgChartLayout";

function normalizeAgents(data: unknown): OrgChartAgent[] {
  if (!data) return [];
  if (Array.isArray(data)) return data as OrgChartAgent[];
  const obj = data as Record<string, unknown>;
  if (Array.isArray(obj.agents)) return obj.agents as OrgChartAgent[];
  if (Array.isArray(obj.items)) return obj.items as OrgChartAgent[];
  return [];
}

export function OrgChartPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<unknown>("/agents");

  if (isLoading) {
    return (
      <div className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--mute)]">
        Loading…
      </div>
    );
  }
  if (error) {
    return (
      <div role="alert" className="editorial-page p-8 text-[var(--danger)]">
        Failed to load agents.
      </div>
    );
  }

  const agents = normalizeAgents(data);
  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead overline="Team" title="Org chart" />
      <OrgChart agents={agents} />
    </div>
  );
}
```

- [ ] **Step 3: Verify + commit**

```sh
cd apps/frontend && pnpm test OrgChartPanel -- --run
git -C ... add apps/frontend/src/components/teams/panels/OrgChartPanel.tsx \
  apps/frontend/tests/unit/components/teams/panels/OrgChartPanel.test.tsx
git -C ... commit -m "feat(teams-orgchart): OrgChartPanel composes editorial atoms"
```

---

## Task 4: Final verify + push + open PR

```sh
cd apps/frontend && pnpm test 2>&1 | tail -10
cd apps/frontend && pnpm lint && pnpm tsc --noEmit
git -C ... push -u origin plan/teams-agents-orgchart-atoms-port
gh pr create --title "feat(teams-agents-orgchart): editorial atom adoption (PR 9)" --body "$(cat <<'EOF'
## Summary

PR 9 of the /teams panel port. Refactors AgentsListPanel (160 LOC), AgentDetailPanel (93 LOC), OrgChartPanel (57 LOC) to compose editorial atoms.

## What changed

- AgentsListPanel: PanelMasthead + PanelToolbar + PanelDataTable + PanelEmptyState + EditorialDialog (CreateAgentDialog).
- AgentDetailPanel: PanelMasthead + PanelTabs (overview/runs/config).
- OrgChartPanel: PanelMasthead.

## Test plan

- [x] Atom-contract guards
- [x] tsc + lint clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
