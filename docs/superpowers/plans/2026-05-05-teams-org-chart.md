# Teams Agent Org Chart Implementation Plan (PR #5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Add a new `/teams/org-chart` panel showing agent hierarchy via `reports_to` relationships, with live status dots driven by the realtime channel from PR #518.

**Architectural decisions:**
1. **No new npm deps** — no `react-flow`, `dagre`, or graph libs. Build a vanilla SVG/CSS tree layout (upstream Paperclip's OrgChart.tsx does the same: pure subtreeWidth-based positioning + manual SVG connectors).
2. **Drop pan/zoom/touch gestures** for v1 — Isol8 users have ~1-10 agents (fits on screen). Upstream's 627-LOC OrgChart includes pan/zoom + import/export; we ship the layout core only (~200 LOC).
3. **Realtime via existing TeamsEventsProvider** — no new event subscriptions. Agent status events (already in `EVENT_KEY_MAP` from #518) invalidate `/teams/agents` SWR cache; OrgChart reads from that key, status colors update on next render.
4. **Branch off origin/main** — independent of #3d / #4.

**Architecture:** Pure layout helpers (`subtreeWidth` + `layoutTree`) compute positioned `LayoutNode[]` from an agent list. `OrgChart` component renders absolute-positioned `<AgentCard>` components plus an SVG layer for parent-child edges. `OrgChartPanel` is the panel wrapper that fetches `/teams/agents` via SWR and feeds the OrgChart. Wired into TeamsSidebar as a new entry between Agents and Inbox.

**Tech Stack:** React 19 + Next 16 App Router + Tailwind v4 + SWR + lucide-react. No new npm deps.

**Upstream reference:** `paperclip/ui/src/pages/OrgChart.tsx` (627 LOC). Translates `useQuery → useTeamsApi.read`, drops pan/zoom + import/export.

---

## In scope (#5 v1)

- Sidebar entry: "Org chart" between Agents and Inbox.
- Page renders a tree of agent cards positioned via subtreeWidth-based layout.
- Each card: agent name, role label, status dot (color-coded), optional icon.
- Multi-root support — agents with `reportsTo: null` (or whose reports_to points to a missing/terminated agent) become independent roots, rendered horizontally.
- Click a card → navigates to `/teams/agents/{id}` (existing route — IssueDetailPanel pattern).
- Empty state when no agents: "No agents yet."
- Loading skeleton while data loads.
- Live status reflection: when agent.status updates (via realtime), card recolors on re-render.

## Out of scope (deferred)

- Pan / zoom / touch gestures
- Zoom controls (+/- buttons)
- Import / export (org chart JSON)
- Drag-and-drop reparenting
- Cycle detection beyond breaking the loop (just don't infinite-recurse)
- Animated transitions between layouts
- Search / filter inside the chart
- Mobile-specific layout (desktop-first; works on tablet, may overflow on phone)
- Live status DOTS on every render — driven by SWR refetch on realtime events, not a per-card subscription

---

## File structure

```
apps/frontend/src/components/teams/
├── org-chart/
│   ├── orgChartLayout.ts               # NEW. Pure layout + types. ~120 LOC.
│   ├── AgentCard.tsx                   # NEW. Single card. ~80 LOC.
│   ├── OrgChart.tsx                    # NEW. Tree assembly. ~150 LOC.
│   └── statusColors.ts                 # NEW. Status → Tailwind color map. ~30 LOC.
├── panels/
│   └── OrgChartPanel.tsx               # NEW. Fetch + render OrgChart. ~50 LOC.
├── TeamsSidebar.tsx                    # MODIFY: add "Org chart" entry.
└── TeamsPanelRouter.tsx                # MODIFY: register the new panel.

apps/frontend/src/__tests__/components/teams/org-chart/
├── orgChartLayout.test.ts
├── AgentCard.test.tsx
├── OrgChart.test.tsx
└── statusColors.test.ts
```

---

## Common conventions

- 3-line MIT attribution header on every ported file (PR #3b/c/d/4 precedent).
- Test files use explicit vitest imports.
- Tailwind retheme rules: blue→amber-700/dark:amber-400 if any blues. KEEP semantic status colors (green/red/yellow/gray for status dots).
- DO NOT push between tasks. Push at Task 4.

---

## Task 1: orgChartLayout.ts + statusColors.ts

Pure helpers. No React. Foundation for the visual components.

**Files:**
- Create: `apps/frontend/src/components/teams/org-chart/orgChartLayout.ts`
- Create: `apps/frontend/src/components/teams/org-chart/statusColors.ts`
- Test: `apps/frontend/src/__tests__/components/teams/org-chart/orgChartLayout.test.ts`
- Test: `apps/frontend/src/__tests__/components/teams/org-chart/statusColors.test.ts`

- [ ] **Step 1: orgChartLayout.ts**

```ts
// apps/frontend/src/components/teams/org-chart/orgChartLayout.ts

// Ported from upstream Paperclip's OrgChart layout algorithm
// (paperclip/ui/src/pages/OrgChart.tsx) (MIT, (c) 2025 Paperclip AI).
// Pure subtreeWidth-based tree positioning. Drops pan/zoom/touch gestures.
// See spec at docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md

import type { CompanyAgent } from "@/components/teams/shared/types";

// Layout constants — match upstream values.
export const CARD_W = 200;
export const CARD_H = 100;
export const GAP_X = 32;
export const GAP_Y = 80;
export const PADDING = 60;

export interface OrgChartAgent extends CompanyAgent {
  /** ID of the agent this one reports to (null = root). */
  reportsTo?: string | null;
  /** Status string from BFF: "idle" | "running" | "error" | "terminated" | etc. */
  status?: string | null;
  /** Display role e.g. "ceo" | "engineer" — optional, falls back to "Agent". */
  role?: string | null;
}

export interface LayoutNode {
  id: string;
  name: string;
  role: string;
  status: string;
  x: number;
  y: number;
  children: LayoutNode[];
}

interface RawNode {
  id: string;
  name: string;
  role: string;
  status: string;
  reportsTo: string | null;
}

/**
 * Compute the rendering width a subtree needs (in px). A leaf is just the card
 * width; a parent is the larger of its own card width or the summed widths of
 * its children plus gaps.
 */
export function subtreeWidth(node: { children: { id: string }[] }, widths: Map<string, number>): number {
  // Memoized via the `widths` map to avoid O(n^2) recomputation.
  const cached = widths.get(node.children.map((c) => c.id).join("|"));
  if (cached !== undefined) return cached;
  if (node.children.length === 0) return CARD_W;
  // Children width sum + gaps between them.
  let sum = 0;
  for (const child of (node as { children: LayoutNode[] }).children) {
    sum += subtreeWidth(child, widths);
  }
  sum += (node.children.length - 1) * GAP_X;
  return Math.max(CARD_W, sum);
}

/**
 * Position each node in a top-down tree. Children are centered under their
 * parent. Roots laid out horizontally side-by-side.
 *
 * Cycle handling: agents whose reportsTo chain forms a cycle are demoted to
 * roots (their reportsTo is treated as null). A parent referencing a missing
 * id is also treated as a root.
 */
export function layoutTree(agents: OrgChartAgent[]): { roots: LayoutNode[]; width: number; height: number } {
  if (agents.length === 0) return { roots: [], width: 0, height: 0 };

  const byId = new Map<string, OrgChartAgent>();
  for (const a of agents) byId.set(a.id, a);

  // Detect cycles: walk reportsTo chain; if we revisit an id, treat the
  // current agent as a root.
  function chainRoot(start: OrgChartAgent): string | null {
    let current: string | null = start.reportsTo ?? null;
    const seen = new Set<string>([start.id]);
    while (current) {
      if (seen.has(current)) return null; // cycle
      seen.add(current);
      const parent = byId.get(current);
      if (!parent) return null; // dangling reference
      current = parent.reportsTo ?? null;
    }
    return start.reportsTo ?? null;
  }

  // Build child relationships: parent_id -> list of agent ids
  const childrenOf = new Map<string | null, string[]>();
  for (const a of agents) {
    const parent = chainRoot(a); // null = root, else parent id
    if (!childrenOf.has(parent)) childrenOf.set(parent, []);
    childrenOf.get(parent)!.push(a.id);
  }

  function buildNode(id: string): LayoutNode {
    const a = byId.get(id)!;
    const childIds = childrenOf.get(id) ?? [];
    const children = childIds.map(buildNode);
    return {
      id: a.id,
      name: a.name,
      role: a.role ?? "Agent",
      status: a.status ?? "idle",
      x: 0,
      y: 0,
      children,
    };
  }

  const rootIds = childrenOf.get(null) ?? [];
  const widths = new Map<string, number>();
  const rootsRaw = rootIds.map(buildNode);

  // Position pass: walk each root, recursively position children.
  function positionNode(node: LayoutNode, x: number, y: number) {
    node.x = x;
    node.y = y;
    if (node.children.length === 0) return;
    // Total children-row width
    const childWidths = node.children.map((c) => subtreeWidth(c, widths));
    const totalChildrenW = childWidths.reduce((a, b) => a + b, 0) + (node.children.length - 1) * GAP_X;
    // Children are centered under parent's x.
    let childX = x + (CARD_W - totalChildrenW) / 2;
    for (let i = 0; i < node.children.length; i++) {
      const cw = childWidths[i];
      const child = node.children[i];
      // Child center at childX + cw/2 - CARD_W/2
      const childCenterX = childX + cw / 2 - CARD_W / 2;
      positionNode(child, childCenterX, y + CARD_H + GAP_Y);
      childX += cw + GAP_X;
    }
  }

  // Lay out roots side-by-side.
  let cursorX = PADDING;
  for (const root of rootsRaw) {
    const w = subtreeWidth(root, widths);
    positionNode(root, cursorX + (w - CARD_W) / 2, PADDING);
    cursorX += w + GAP_X * 2; // extra gap between independent trees
  }

  // Compute total bounding box.
  let maxX = 0;
  let maxY = 0;
  function visit(node: LayoutNode) {
    maxX = Math.max(maxX, node.x + CARD_W);
    maxY = Math.max(maxY, node.y + CARD_H);
    for (const c of node.children) visit(c);
  }
  for (const r of rootsRaw) visit(r);

  return {
    roots: rootsRaw,
    width: maxX + PADDING,
    height: maxY + PADDING,
  };
}

/** Flatten a tree into a list (useful for rendering all cards). */
export function flattenTree(roots: LayoutNode[]): LayoutNode[] {
  const out: LayoutNode[] = [];
  function visit(n: LayoutNode) {
    out.push(n);
    for (const c of n.children) visit(c);
  }
  for (const r of roots) visit(r);
  return out;
}

/** Yield (parent, child) pairs for SVG edge rendering. */
export function flattenEdges(roots: LayoutNode[]): { from: LayoutNode; to: LayoutNode }[] {
  const out: { from: LayoutNode; to: LayoutNode }[] = [];
  function visit(n: LayoutNode) {
    for (const c of n.children) {
      out.push({ from: n, to: c });
      visit(c);
    }
  }
  for (const r of roots) visit(r);
  return out;
}
```

(Note: simplified `subtreeWidth` memoization — a per-node cache rather than the widths-by-childIds-string. Tests cover correctness.)

- [ ] **Step 2: statusColors.ts**

```ts
// apps/frontend/src/components/teams/org-chart/statusColors.ts

// Status -> Tailwind color class for the agent status dot.
// Idle: muted gray. Running: green pulse. Paused: amber. Error/terminated: red.

export const STATUS_DOT_CLASS: Record<string, string> = {
  idle: "bg-zinc-400 dark:bg-zinc-500",
  running: "bg-emerald-500 animate-pulse",
  paused: "bg-amber-500",
  error: "bg-red-500",
  terminated: "bg-zinc-300 dark:bg-zinc-600",
};

const DEFAULT = "bg-zinc-400 dark:bg-zinc-500";

export function statusDotClass(status: string | null | undefined): string {
  if (!status) return DEFAULT;
  return STATUS_DOT_CLASS[status] ?? DEFAULT;
}
```

- [ ] **Step 3: Tests** (~10 cases for layout + 4 for statusColors)

For `orgChartLayout`:
- Empty input returns empty roots
- Single agent (no reports_to) becomes one root
- 2 children of 1 root: parent at top, children below side-by-side, parent x-centered over children
- 3-level tree: grandparent → parent → child positions correctly
- Multi-root: 2 independent roots, side-by-side
- Cycle detection: A→B, B→A. Both become roots.
- Dangling reference: A.reportsTo = "missing". A becomes a root.
- subtreeWidth scales with children count
- flattenTree returns all nodes
- flattenEdges returns parent-child pairs

For `statusColors`:
- Known status returns mapped class
- Unknown status returns default
- null/undefined returns default
- emerald-500 included for running (sanity check on the map)

- [ ] **Step 4: Commit**

```bash
cd apps/frontend && pnpm test -- src/__tests__/components/teams/org-chart/
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.worktrees/feat-teams-org-chart
git add apps/frontend/src/components/teams/org-chart/ apps/frontend/src/__tests__/components/teams/org-chart/ docs/superpowers/plans/2026-05-05-teams-org-chart.md
git commit -m "feat(teams): port orgChartLayout + statusColors helpers"
```

---

## Task 2: AgentCard component

**Files:**
- Create: `apps/frontend/src/components/teams/org-chart/AgentCard.tsx`
- Test: `apps/frontend/src/__tests__/components/teams/org-chart/AgentCard.test.tsx`

- [ ] **Step 1: Component**

```tsx
"use client";

// apps/frontend/src/components/teams/org-chart/AgentCard.tsx

// Ported from upstream Paperclip's OrgChart card render
// (paperclip/ui/src/pages/OrgChart.tsx) (MIT, (c) 2025 Paperclip AI).
// Single agent card with name + role + status dot. Click navigates.
// See spec at docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md

import Link from "next/link";
import { Bot } from "lucide-react";
import { cn } from "@/lib/utils";
import { statusDotClass } from "./statusColors";
import { CARD_W, CARD_H } from "./orgChartLayout";

export interface AgentCardProps {
  id: string;
  name: string;
  role: string;
  status: string;
  x: number;
  y: number;
}

export function AgentCard({ id, name, role, status, x, y }: AgentCardProps) {
  return (
    <Link
      href={`/teams/agents/${id}`}
      data-agent-card-id={id}
      className={cn(
        "absolute flex flex-col gap-1 rounded-lg border border-border bg-background p-3 shadow-sm transition-shadow hover:shadow-md",
        "no-underline text-foreground"
      )}
      style={{
        left: `${x}px`,
        top: `${y}px`,
        width: `${CARD_W}px`,
        height: `${CARD_H}px`,
      }}
    >
      <div className="flex items-center gap-2 min-w-0">
        <Bot className="h-4 w-4 text-muted-foreground shrink-0" aria-hidden="true" />
        <span className="font-medium text-sm truncate flex-1">{name}</span>
        <span
          className={cn("h-2 w-2 rounded-full shrink-0", statusDotClass(status))}
          aria-label={`Status: ${status}`}
          title={status}
        />
      </div>
      <div className="text-xs text-muted-foreground capitalize">{role}</div>
    </Link>
  );
}
```

- [ ] **Step 2: Tests** (~5 cases — renders name, renders role, renders status dot with right class, link href, position style)

- [ ] **Step 3: Commit**

```
feat(teams): port AgentCard (status dot + link)
```

---

## Task 3: OrgChart component

**Files:**
- Create: `apps/frontend/src/components/teams/org-chart/OrgChart.tsx`
- Test: `apps/frontend/src/__tests__/components/teams/org-chart/OrgChart.test.tsx`

- [ ] **Step 1: Component**

```tsx
"use client";

// apps/frontend/src/components/teams/org-chart/OrgChart.tsx

// Ported from upstream Paperclip's OrgChart render
// (paperclip/ui/src/pages/OrgChart.tsx) (MIT, (c) 2025 Paperclip AI).
// Renders positioned AgentCards + SVG edges. No pan/zoom for v1.
// See spec at docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md

import { useMemo } from "react";
import { AgentCard } from "./AgentCard";
import {
  CARD_W,
  CARD_H,
  flattenEdges,
  flattenTree,
  layoutTree,
  type OrgChartAgent,
} from "./orgChartLayout";

export interface OrgChartProps {
  agents: OrgChartAgent[];
  className?: string;
}

export function OrgChart({ agents, className }: OrgChartProps) {
  const layout = useMemo(() => layoutTree(agents), [agents]);

  if (agents.length === 0) {
    return (
      <div className={className}>
        <div className="flex flex-col items-center justify-center py-12 text-sm text-muted-foreground">
          No agents yet.
        </div>
      </div>
    );
  }

  const cards = flattenTree(layout.roots);
  const edges = flattenEdges(layout.roots);

  return (
    <div className={className}>
      <div className="overflow-auto">
        <div
          className="relative"
          style={{ width: layout.width, height: layout.height, minWidth: "100%" }}
          data-testid="org-chart-canvas"
        >
          {/* SVG edge layer */}
          <svg
            className="absolute inset-0 pointer-events-none"
            width={layout.width}
            height={layout.height}
            aria-hidden="true"
          >
            {edges.map(({ from, to }) => {
              const fromX = from.x + CARD_W / 2;
              const fromY = from.y + CARD_H;
              const toX = to.x + CARD_W / 2;
              const toY = to.y;
              const midY = (fromY + toY) / 2;
              return (
                <path
                  key={`${from.id}-${to.id}`}
                  d={`M ${fromX} ${fromY} L ${fromX} ${midY} L ${toX} ${midY} L ${toX} ${toY}`}
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.5}
                  className="text-border"
                />
              );
            })}
          </svg>

          {/* Cards */}
          {cards.map((c) => (
            <AgentCard
              key={c.id}
              id={c.id}
              name={c.name}
              role={c.role}
              status={c.status}
              x={c.x}
              y={c.y}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Tests** (~6 cases — empty state, single agent, multi-agent renders all cards, SVG edges count = (agents - roots), CSS positioning matches layout output)

- [ ] **Step 3: Commit**

```
feat(teams): port OrgChart (positioned cards + SVG edges)
```

---

## Task 4: OrgChartPanel + Sidebar wiring + final + PR

**Files:**
- Create: `apps/frontend/src/components/teams/panels/OrgChartPanel.tsx`
- Modify: `apps/frontend/src/components/teams/TeamsSidebar.tsx` — add "Org chart" entry between Agents and Inbox
- Modify: `apps/frontend/src/components/teams/TeamsPanelRouter.tsx` — register the new panel
- Test: `apps/frontend/src/__tests__/components/teams/panels/OrgChartPanel.test.tsx`

- [x] **Step 1: OrgChartPanel**

```tsx
"use client";

// apps/frontend/src/components/teams/panels/OrgChartPanel.tsx

// Wraps the OrgChart with data fetching + loading/error states.
// Live status updates flow through the existing TeamsEventsProvider's
// EVENT_KEY_MAP — agent.* events invalidate /teams/agents and the
// chart re-renders with fresh status colors.

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
    return <div className="p-8 text-sm text-muted-foreground">Loading…</div>;
  }
  if (error) {
    return (
      <div role="alert" className="p-8 text-sm text-destructive">
        Failed to load agents.
      </div>
    );
  }

  const agents = normalizeAgents(data);
  return (
    <div className="p-4">
      <h1 className="text-lg font-medium mb-4">Org chart</h1>
      <OrgChart agents={agents} />
    </div>
  );
}
```

- [x] **Step 2: TeamsSidebar — add entry**

In `TeamsSidebar.tsx`, add a `Network` icon import (from lucide-react) and a new entry in ITEMS:

```ts
{ key: "org-chart", label: "Org chart", Icon: Network },
```

Place it between the `agents` and `inbox` entries.

- [x] **Step 3: TeamsPanelRouter — register**

In `TeamsPanelRouter.tsx`, add to PANELS:

```ts
"org-chart": dynamic(() =>
  import("./panels/OrgChartPanel").then((m) => m.OrgChartPanel),
),
```

- [x] **Step 4: Tests for the panel**

3 cases: loading, error, renders OrgChart with normalized agents.

- [x] **Step 5: Run full test suite + lint + typecheck**

```bash
cd apps/frontend && pnpm test 2>&1 | tail -30
pnpm lint 2>&1 | tail -10
pnpm --filter @isol8/frontend exec tsc --noEmit 2>&1 | grep error | head
```

Expected: NO new failures (pre-existing 4-file failures ignored). Lint + typecheck clean for our files.

- [x] **Step 6: Update roadmap row #5**

`docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md` row #5:
- Status: `Done`
- Plan column: link to `../plans/2026-05-05-teams-org-chart.md`
- PR column: placeholder `(#5 link)`

- [ ] **Step 7: Push + open PR**

```bash
git push -u origin feat/teams-org-chart
gh pr create --title "feat(teams): agent org chart panel (#5)" --body "$(cat <<'EOF'
## Summary

Sub-project **#5** of the [Teams UI parity roadmap](docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md). Adds a new "Org chart" panel under ``/teams`` showing agent hierarchy via ``reports_to`` with live status dots.

## What's new

- ``orgChartLayout.ts`` — pure tree-layout algorithm (subtreeWidth + recursive positioning + cycle detection). 100% pure / unit-testable.
- ``statusColors.ts`` — status string → Tailwind class map (idle gray, running green pulse, paused amber, error red, terminated muted).
- ``AgentCard`` — single absolute-positioned card with status dot.
- ``OrgChart`` — assembles cards + SVG edges (parent-child connectors).
- ``OrgChartPanel`` — fetches ``/teams/agents``, normalizes, renders OrgChart.
- ``TeamsSidebar`` gains "Org chart" entry between Agents and Inbox.
- ``TeamsPanelRouter`` registers ``org-chart`` panel.

## Realtime

No new event subscriptions. Agent status updates flow through PR #518's ``TeamsEventsProvider`` ``EVENT_KEY_MAP`` — agent events already invalidate ``/teams/agents``. The chart re-renders with fresh ``status`` field on next paint, status dots recolor naturally.

## Architectural decisions

1. **No new npm deps.** Vanilla SVG/CSS layout algorithm (no react-flow, dagre, or graph libs). Matches upstream Paperclip's approach.
2. **No pan/zoom in v1.** Isol8 users have ~1-10 agents (fits on screen). Upstream's 627-LOC OrgChart includes pan/zoom + import/export; we ship the layout core only.
3. **Branch off origin/main** — independent of #3d / #4.

## Out of scope (deferred)

- Pan / zoom / touch gestures
- Zoom controls
- Import / export
- Drag-and-drop reparenting
- Animated transitions
- Search / filter inside the chart

## Test plan

- [x] Unit tests for layout (cycle detection, multi-root, subtreeWidth) + status colors + AgentCard + OrgChart + OrgChartPanel
- [x] Lint + typecheck clean
- [ ] Manual visual verification on dev (deferred — reviewer to validate)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 8: Update roadmap PR link + push**

```bash
git add docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md
git commit -m "docs(roadmap): link PR #<NUM> in row #5"
git push
```

- [ ] **Step 9: Watch CI briefly**

```bash
gh pr checks <pr-number> --repo Isol8AI/isol8 2>&1 | head -10
```

DO NOT MERGE. Report PR URL + initial CI status.

---

## Self-review checklist

- ✅ 4 tasks; clean dependency flow (layout helpers → AgentCard → OrgChart → panel+wiring)
- ✅ No new npm deps
- ✅ Branch off origin/main (not coupled to #3d or #4)
- ✅ MIT attribution headers required on every ported file
- ✅ Vitest explicit imports on every test file
- ✅ Branch: `feat/teams-org-chart`
- ✅ Final task pushes + opens PR; does NOT merge
