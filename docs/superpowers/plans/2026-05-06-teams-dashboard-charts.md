# Teams Dashboard Charts Implementation Plan (PR #2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Port upstream Paperclip's Dashboard charts (4 live charts: Run Activity, Priority breakdown, Issue Status, Success Rate) + a real activity feed (replaces hardcoded `EDITORIAL_AGENTS` demo). Closes the last row of the [Teams UI parity roadmap](docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md).

**Architectural decisions:**
1. **No new npm deps.** Upstream uses vanilla flexbox + percentage heights for the bar charts (`paperclip/ui/src/components/ActivityCharts.tsx`, 273 LOC). No `recharts`/`d3`/`chart.js`. Match this.
2. **Un-flatten the BFF `/dashboard` response.** Today's `feed.py:_flatten_dashboard` collapses the rich upstream shape (`agents`, `tasks`, `runActivity`, `costs`, `pendingApprovals`, `budgets`) into 4 scalars. Charts need the rich shape. Drop the flatten.
3. **Priority + Status charts compute client-side from `/teams/issues`.** Matches upstream — no new BFF endpoint.
4. **Visual styling:** apply during implementation via the `frontend-design` skill, not prescribed in this plan. Plan focuses on structure + data wiring.
5. **Branch off origin/main** — independent of any other in-flight work.

**Architecture:** BFF stops flattening. Frontend gets a `DashboardSummary` type matching upstream's shape. Three new components ported (`ActivityCharts.tsx`, `MetricCard.tsx`, `ActivityRow.tsx`) + a hook (`useActivityFeed`). `DashboardPanel.tsx` rewrites: 4 metric tiles (top), 4 charts grid (middle), recent activity panel (bottom). Removes the hardcoded `EDITORIAL_AGENTS` demo.

**Tech Stack:** React 19 + Next 16 + Tailwind v4 + SWR + lucide-react. No new npm deps.

**Upstream reference:**
- `paperclip/ui/src/pages/Dashboard.tsx` (396 LOC)
- `paperclip/ui/src/components/ActivityCharts.tsx` (273 LOC)
- `paperclip/ui/src/components/MetricCard.tsx`
- `paperclip/ui/src/components/ActivityRow.tsx`
- `paperclip/server/src/services/dashboard.ts` (for the response shape)

---

## In scope (#2 v1)

**BFF:**
- Un-flatten `/teams/dashboard` — return upstream's full shape: `{dashboard: {agents:{active,running,paused,error}, tasks:{open,inProgress,blocked,done}, runActivity:[{date,succeeded,failed,other,total}], costs:{monthSpendCents,monthBudgetCents,monthUtilizationPercent}, pendingApprovals:number, budgets:{pendingApprovals:number}}, sidebar_badges: {...}}`.
- Existing tests adapt to the new shape.

**Frontend types:**
- `DashboardSummary` — matches upstream's response.
- `DashboardRunActivityDay` — `{date, succeeded, failed, other, total}`.

**Frontend components (new):**
- `ActivityCharts.tsx` — exports `ChartCard`, `RunActivityChart`, `PriorityChart`, `IssueStatusChart`, `SuccessRateChart`, helpers `getLast14Days`, `DateLabels`, `ChartLegend`. Verbatim port (~273 LOC) modulo color rethemes.
- `MetricCard.tsx` — top stat tile. ~50 LOC.
- `ActivityRow.tsx` — single activity row. ~80 LOC.

**Frontend hook:**
- `useActivityFeed` — SWR-wrapped read of `/teams/activity` (already exists in BFF).

**DashboardPanel rewrite:**
- 4 metric tiles (Agents, Open Issues, Month Spend, Pending Approvals) — replace existing 4 with upstream's variants. Driven by rich BFF shape.
- 4 charts grid (Run Activity, Priority, Status, Success Rate). Mobile: stack 1-col; tablet: 2-col; desktop: 4-col.
- Recent activity panel — uses real `/teams/activity` data, drops `EDITORIAL_AGENTS`.

**Visual styling pass:** invoke `frontend-design` skill during Task 5 (DashboardPanel rewrite) to ensure the result matches Isol8's design system at the integration level.

## Out of scope (deferred)

- Configurable date ranges (Last 14 days hardcoded)
- Click-through from chart bars to filtered issue lists
- Live realtime updates beyond SWR refresh — TeamsEventsProvider already invalidates `/dashboard` on agent/issue events
- ActiveAgentsPanel from upstream Dashboard
- Plugin slot mounts
- Onboarding empty state when no agents exist (Dashboard already redirects to onboarding via TeamsLayout's gate)
- Cost trend chart (only the 4 listed in roadmap)

---

## File structure

```
apps/backend/
├── routers/teams/feed.py           # MODIFY: drop _flatten_dashboard, return raw upstream shape
└── tests/unit/routers/teams/test_dashboard.py    # MODIFY: assert rich shape

apps/frontend/src/components/teams/
├── shared/types.ts                 # MODIFY: add DashboardSummary + DashboardRunActivityDay
├── dashboard/
│   ├── ActivityCharts.tsx          # NEW. Port. ~280 LOC.
│   ├── MetricCard.tsx              # NEW. Port. ~50 LOC.
│   ├── ActivityRow.tsx             # NEW. Port. ~80 LOC.
│   └── hooks/
│       └── useActivityFeed.ts      # NEW. SWR fan-out. ~30 LOC.
└── panels/
    └── DashboardPanel.tsx          # MODIFY: full rewrite.

apps/frontend/src/__tests__/components/teams/dashboard/
├── ActivityCharts.test.tsx
├── MetricCard.test.tsx
├── ActivityRow.test.tsx
└── hooks/useActivityFeed.test.ts
```

---

## Common conventions

- 3-line MIT attribution header on every ported file (PR #3b/c/d/4/5 precedent).
- Test files import explicitly from vitest.
- shadcn primitives — use what exists. No new ones needed for charts (vanilla flexbox).
- Retheme rules from #3b: blue→amber-700/dark:amber-400 IN JSX. KEEP semantic chart colors (greens/reds/yellows) because they encode meaning.
- BFF priority enum already fixed in #3d (critical|high|medium|low).
- DO NOT push between tasks. Push at Task 6.

---

## Task 1: BFF — un-flatten /dashboard

**Files:**
- Modify: `apps/backend/routers/teams/feed.py`
- Modify or create: `apps/backend/tests/unit/routers/teams/test_dashboard.py`

- [ ] **Step 1: Refactor feed.py**

Drop `_flatten_dashboard`. Return upstream's full shape verbatim:

```python
@router.get("/dashboard")
async def get_dashboard(ctx: TeamsContext = Depends(_ctx)):
    """Fetch the dashboard summary + sidebar badges in parallel.

    Returns the upstream Paperclip dashboard summary verbatim
    (``agents`` / ``tasks`` / ``runActivity`` / ``costs`` /
    ``pendingApprovals`` / ``budgets``) so the panel can render the
    full set of metric tiles + charts without further fan-out.
    """
    admin = _agents._admin()
    badges, dash = await asyncio.gather(
        admin.get_sidebar_badges(
            company_id=ctx.company_id,
            session_cookie=ctx.session_cookie,
        ),
        admin.get_dashboard(
            company_id=ctx.company_id,
            session_cookie=ctx.session_cookie,
        ),
    )
    return {"dashboard": dash, "sidebar_badges": badges}
```

If a defensive default is needed (in case upstream returns `None` or a non-dict), normalize at the top: `dash = dash if isinstance(dash, dict) else {}`. Don't reshape further.

- [ ] **Step 2: Update tests**

Existing dashboard route tests assert the flattened shape. Update to assert the rich shape passes through verbatim. Mock `admin.get_dashboard` returns the upstream shape directly.

- [ ] **Step 3: Run + commit**

```bash
cd apps/backend && uv run pytest tests/unit/routers/teams/ -v --no-cov 2>&1 | tail -20
git add apps/backend/routers/teams/feed.py apps/backend/tests/unit/routers/teams/test_dashboard.py docs/superpowers/plans/2026-05-06-teams-dashboard-charts.md
git commit -m "feat(teams): un-flatten /dashboard — return upstream shape verbatim for charts"
```

---

## Task 2: Frontend types — DashboardSummary + DashboardRunActivityDay

**Files:**
- Modify: `apps/frontend/src/components/teams/shared/types.ts`
- Modify: `apps/frontend/src/__tests__/components/teams/shared/types.test.ts`

- [ ] **Step 1: Add types**

```ts
export interface DashboardRunActivityDay {
  date: string;       // YYYY-MM-DD
  succeeded: number;
  failed: number;
  other: number;
  total: number;
}

export interface DashboardAgentBuckets {
  active?: number;
  running?: number;
  paused?: number;
  error?: number;
}

export interface DashboardTaskBuckets {
  open?: number;
  inProgress?: number;
  blocked?: number;
  done?: number;
}

export interface DashboardCosts {
  monthSpendCents: number;
  monthBudgetCents: number;
  monthUtilizationPercent: number;
}

export interface DashboardBudgets {
  pendingApprovals: number;
}

export interface DashboardSummary {
  agents: DashboardAgentBuckets;
  tasks: DashboardTaskBuckets;
  runActivity: DashboardRunActivityDay[];
  costs: DashboardCosts;
  pendingApprovals: number;
  budgets: DashboardBudgets;
}

export interface DashboardResponse {
  dashboard: DashboardSummary;
  sidebar_badges?: Record<string, number>;
}
```

- [ ] **Step 2: Compile-only tests**

Add 2-3 assertions confirming the type shape compiles + contains the expected fields.

- [ ] **Step 3: Commit**

```
feat(teams): DashboardSummary + DashboardRunActivityDay types
```

---

## Task 3: Port ActivityCharts.tsx

**Files:**
- Create: `apps/frontend/src/components/teams/dashboard/ActivityCharts.tsx`
- Test: `apps/frontend/src/__tests__/components/teams/dashboard/ActivityCharts.test.tsx`

- [ ] **Step 1: Read upstream**

```bash
cat /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/paperclip/ui/src/components/ActivityCharts.tsx
```

- [ ] **Step 2: Port verbatim with these adjustments**

Imports map:
- `@paperclipai/shared` types (`DashboardRunActivityDay`, `HeartbeatRun`) → `@/components/teams/shared/types`
- The `HeartbeatRun` variant of `RunChartProps` may be unused in our v1 — keep the union if straightforward, otherwise simplify to just `{activity?: DashboardRunActivityDay[]}`.

Color tokens (KEEP — semantic):
- emerald-500 (succeeded), red-500 (failed), neutral-500 (other) — KEEP
- Priority colors: critical=#ef4444, high=#f97316, medium=#eab308, low=#6b7280 — KEEP

3-line MIT attribution header.

- [ ] **Step 3: Tests**

For each of the 4 charts:
- Renders empty state ("No runs yet" / "No issues yet") when given empty data
- Renders bar segments when data present (count > 0 segments visible)
- DateLabels renders the 1st, 7th, and 13th day labels (covered once)
- Sanity: PriorityChart groups issues by `priority` field correctly
- Sanity: IssueStatusChart groups by `status` field
- SuccessRateChart computes `succeeded / total` correctly

Target ~12-15 tests across the 4 charts.

- [ ] **Step 4: Commit**

```
feat(teams): port ActivityCharts (RunActivity / Priority / IssueStatus / SuccessRate)
```

---

## Task 4: Port MetricCard + ActivityRow + useActivityFeed

**Files:**
- Create: `apps/frontend/src/components/teams/dashboard/MetricCard.tsx`
- Create: `apps/frontend/src/components/teams/dashboard/ActivityRow.tsx`
- Create: `apps/frontend/src/components/teams/dashboard/hooks/useActivityFeed.ts`
- Test: `apps/frontend/src/__tests__/components/teams/dashboard/MetricCard.test.tsx`
- Test: `apps/frontend/src/__tests__/components/teams/dashboard/ActivityRow.test.tsx`
- Test: `apps/frontend/src/__tests__/components/teams/dashboard/hooks/useActivityFeed.test.ts`

- [ ] **Step 1: Read upstream**

```bash
cat /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/paperclip/ui/src/components/MetricCard.tsx
cat /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/paperclip/ui/src/components/ActivityRow.tsx
```

- [ ] **Step 2: Port both components**

`MetricCard`: icon + value + label + optional description + optional `to` link. ~50 LOC.

`ActivityRow`: single activity event with actor identity + relative time + body. Translate any `Identity` / `timeAgo` imports to our equivalents from `@/components/teams/shared/components/Identity` (already ported in #3b) and `@/components/teams/shared/lib/timeAgo` (also ported).

- [ ] **Step 3: useActivityFeed hook**

```ts
import { useTeamsApi } from "@/hooks/useTeamsApi";

export interface ActivityEvent {
  id: string;
  action: string;
  actorType?: string | null;
  actorId?: string | null;
  agentId?: string | null;
  entityType?: string | null;
  entityId?: string | null;
  details?: Record<string, unknown> | null;
  createdAt: string;
}

export function useActivityFeed(limit = 10): {
  events: ActivityEvent[];
  isLoading: boolean;
  isError: boolean;
} {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<ActivityEvent[] | { events: ActivityEvent[] }>("/activity");
  const events = Array.isArray(data) ? data : data?.events ?? [];
  return { events: events.slice(0, limit), isLoading, isError: !!error };
}
```

- [ ] **Step 4: Tests**

3-5 cases per component/hook: render shape, optional fields, link behavior, hook normalizes envelope vs array.

- [ ] **Step 5: Commit**

```
feat(teams): port MetricCard + ActivityRow + useActivityFeed
```

---

## Task 5: DashboardPanel rewrite (apply frontend-design skill)

**Files:**
- Modify: `apps/frontend/src/components/teams/panels/DashboardPanel.tsx` — full rewrite
- Test: `apps/frontend/src/__tests__/components/teams/panels/DashboardPanel.test.tsx` (new)

- [ ] **Step 1: Read upstream Dashboard.tsx for layout reference**

```bash
sed -n '180,400p' /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/paperclip/ui/src/pages/Dashboard.tsx
```

Identify the layout structure: header / metric grid / charts grid / activity panel.

- [ ] **Step 2: Compose the panel**

```tsx
"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { Bot, CircleDot, DollarSign, ShieldCheck } from "lucide-react";
import { MetricCard } from "@/components/teams/dashboard/MetricCard";
import {
  ChartCard,
  RunActivityChart,
  PriorityChart,
  IssueStatusChart,
  SuccessRateChart,
} from "@/components/teams/dashboard/ActivityCharts";
import { ActivityRow } from "@/components/teams/dashboard/ActivityRow";
import { useActivityFeed } from "@/components/teams/dashboard/hooks/useActivityFeed";
import type {
  DashboardResponse,
  DashboardSummary,
  Issue,
} from "@/components/teams/shared/types";

function formatCents(cents: number): string {
  return `$${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function emptyDashboard(): DashboardSummary {
  return {
    agents: {},
    tasks: {},
    runActivity: [],
    costs: { monthSpendCents: 0, monthBudgetCents: 0, monthUtilizationPercent: 0 },
    pendingApprovals: 0,
    budgets: { pendingApprovals: 0 },
  };
}

export function DashboardPanel() {
  const { read } = useTeamsApi();
  const { data: dashboardData, isLoading, error } = read<DashboardResponse>("/dashboard");
  const issuesQuery = read<Issue[] | { items: Issue[] }>("/issues");
  const { events, isLoading: activityLoading } = useActivityFeed(10);

  if (isLoading) return <div className="p-8 text-sm text-muted-foreground">Loading…</div>;
  if (error) {
    return (
      <div role="alert" className="p-8 text-sm text-destructive">
        Failed to load dashboard.
      </div>
    );
  }

  const dashboard = dashboardData?.dashboard ?? emptyDashboard();
  const issues = Array.isArray(issuesQuery.data)
    ? issuesQuery.data
    : issuesQuery.data?.items ?? [];

  const agentsCount = Object.values(dashboard.agents).reduce((s, v) => s + (v ?? 0), 0);
  const openTasks = dashboard.tasks.open ?? 0;
  const blockedTasks = dashboard.tasks.blocked ?? 0;
  const totalApprovals = dashboard.pendingApprovals + dashboard.budgets.pendingApprovals;

  return (
    <div className="p-4 sm:p-6 flex flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold">Dashboard</h1>
        <p className="text-sm text-muted-foreground">Live snapshot of your agents.</p>
      </div>

      {/* Metric tiles */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          icon={Bot}
          value={agentsCount}
          label="Agents"
          to="/teams/agents"
          description={
            <span>
              {dashboard.agents.running ?? 0} running, {dashboard.agents.paused ?? 0} paused
            </span>
          }
        />
        <MetricCard
          icon={CircleDot}
          value={openTasks}
          label="Open Tasks"
          to="/teams/issues"
          description={
            <span>{openTasks} open, {blockedTasks} blocked</span>
          }
        />
        <MetricCard
          icon={DollarSign}
          value={formatCents(dashboard.costs.monthSpendCents)}
          label="Month Spend"
          to="/teams/costs"
          description={
            dashboard.costs.monthBudgetCents > 0 ? (
              <span>
                {dashboard.costs.monthUtilizationPercent}% of {formatCents(dashboard.costs.monthBudgetCents)} budget
              </span>
            ) : (
              <span>Unlimited budget</span>
            )
          }
        />
        <MetricCard
          icon={ShieldCheck}
          value={totalApprovals}
          label="Pending Approvals"
          to="/teams/approvals"
          description={
            dashboard.budgets.pendingApprovals > 0 ? (
              <span>{dashboard.budgets.pendingApprovals} budget overrides awaiting board review</span>
            ) : (
              <span>Awaiting board review</span>
            )
          }
        />
      </div>

      {/* Charts grid */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <ChartCard title="Run Activity" subtitle="Last 14 days">
          <RunActivityChart activity={dashboard.runActivity} />
        </ChartCard>
        <ChartCard title="Issues by Priority" subtitle="Last 14 days">
          <PriorityChart issues={issues} />
        </ChartCard>
        <ChartCard title="Issues by Status" subtitle="Last 14 days">
          <IssueStatusChart issues={issues} />
        </ChartCard>
        <ChartCard title="Success Rate" subtitle="Last 14 days">
          <SuccessRateChart activity={dashboard.runActivity} />
        </ChartCard>
      </div>

      {/* Recent activity */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-sm font-medium">Recent activity</h2>
          {events.length > 0 && (
            <a href="/teams/activity" className="text-xs text-muted-foreground hover:underline">
              View all
            </a>
          )}
        </div>
        <div className="border border-border rounded-lg divide-y divide-border">
          {activityLoading && events.length === 0 ? (
            <div className="p-4 text-sm text-muted-foreground">Loading activity…</div>
          ) : events.length === 0 ? (
            <div className="p-4 text-sm text-muted-foreground">No recent activity.</div>
          ) : (
            events.map((evt) => <ActivityRow key={evt.id} event={evt} />)
          )}
        </div>
      </div>
    </div>
  );
}
```

3-line MIT attribution header.

- [ ] **Step 3: Apply frontend-design skill**

Invoke `frontend-design` skill on the rewritten DashboardPanel + the 3 ported components (ActivityCharts/MetricCard/ActivityRow). Goal: production-grade visual quality matching the rest of /teams (#3b/c/d/4/5).

Read the agent's recommendations and apply targeted edits. Re-run targeted tests.

- [ ] **Step 4: Tests**

```tsx
// 5-6 cases:
// - loading state renders
// - error state renders alert
// - metric tiles render with values from BFF data
// - 4 charts render given activity + issues data
// - empty activity renders "No recent activity"
// - activity events render via ActivityRow
```

Mock `useTeamsApi` + `useActivityFeed`.

- [ ] **Step 5: Commit**

```
feat(teams): rewrite DashboardPanel with charts + real activity feed
```

---

## Task 6: Final verification + roadmap update + open PR

- [ ] **Step 1: Full frontend test suite**

```bash
cd apps/frontend && pnpm test 2>&1 | tail -30
```

Pre-existing failures (BotSetupWizard / MyChannelsSection / etc.) ignored. NO new failures.

- [ ] **Step 2: Lint + typecheck**

```bash
cd apps/frontend && pnpm lint 2>&1 | tail -10
pnpm --filter @isol8/frontend exec tsc --noEmit 2>&1 | grep error | head
```

- [ ] **Step 3: Backend tests**

```bash
cd apps/backend && uv run pytest tests/unit/routers/teams/ -v --no-cov 2>&1 | tail -15
```

- [ ] **Step 4: Update roadmap row #2**

`docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md` row #2 (Dashboard charts):
- Status: `Pending` → `Done`
- Plan: `2026-05-06-teams-dashboard-charts.md`
- PR: placeholder `(#2 link)`

- [ ] **Step 5: Push + open PR**

```bash
git push -u origin feat/teams-dashboard-charts
gh pr create --title "feat(teams): dashboard charts + real activity feed (#2)" --body "$(cat <<'EOF'
## Summary

Closes the **Dashboard charts** sub-project (row #2) of the [Teams UI parity roadmap](docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md). Replaces the stub DashboardPanel (4 stat tiles + hardcoded ``EDITORIAL_AGENTS`` mock activity) with a faithful port of upstream Paperclip's Dashboard: 4 metric tiles + 4 live charts (Run Activity / Priority / Status / Success Rate) + a real activity feed wired to ``/teams/activity``.

## What's new

**BFF:**
- ``GET /teams/dashboard`` no longer flattens — returns the rich upstream shape (``agents`` / ``tasks`` / ``runActivity`` / ``costs`` / ``pendingApprovals`` / ``budgets``) so the panel can render charts + tiles in a single round-trip.

**Frontend types:**
- ``DashboardSummary``, ``DashboardRunActivityDay``, ``DashboardCosts``, ``DashboardAgentBuckets``, ``DashboardTaskBuckets``, ``DashboardBudgets``, ``DashboardResponse``.

**Frontend components:**
- ``ActivityCharts`` (vanilla flexbox, no chart lib) — ChartCard, RunActivityChart, PriorityChart, IssueStatusChart, SuccessRateChart.
- ``MetricCard`` — top tile with icon + value + label + optional link.
- ``ActivityRow`` — actor identity + relative time + body.
- ``useActivityFeed`` hook — SWR wrapper for ``/teams/activity``.

**DashboardPanel rewrite:**
- 4 metric tiles (Agents / Open Tasks / Month Spend / Pending Approvals).
- 4 charts grid (responsive: 1 → 2 → 4 cols).
- Recent activity feed (real data, drops EDITORIAL_AGENTS demo).

## Architectural decisions

1. **No new npm deps.** Vanilla flexbox + percentage heights for charts (matches upstream).
2. **Un-flatten BFF** instead of adding a parallel ``/dashboard/charts`` endpoint — single SWR cache key, no migration window.
3. **Priority + Status charts compute client-side** from ``/teams/issues`` (matches upstream — no new BFF endpoint).
4. **Visual styling pass via frontend-design skill** during DashboardPanel rewrite.

## Out of scope (deferred)

- Configurable date ranges (Last 14 days hardcoded)
- Chart click-through to filtered issue lists
- ActiveAgentsPanel from upstream
- Plugin slot mounts
- Cost trend chart

## Test plan

- [x] BFF tests: dashboard route returns rich shape verbatim
- [x] Per-component unit tests for each chart + MetricCard + ActivityRow + useActivityFeed
- [x] DashboardPanel integration tests (loading / error / populated / empty)
- [x] Lint + typecheck clean
- [ ] Manual visual verification on dev (deferred — reviewer to validate)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 6: Update roadmap with the actual PR link**

Append a follow-up commit linking the actual PR number.

- [ ] **Step 7: Watch CI briefly**

```bash
gh pr checks <pr-number> --repo Isol8AI/isol8 2>&1 | head -10
```

DO NOT MERGE. Report PR URL + initial CI status.

---

## Self-review checklist

- ✅ 6 tasks; clean dependency flow (BFF → types → charts → support components → page → verification)
- ✅ No new npm deps
- ✅ Branch off origin/main (independent of any in-flight work)
- ✅ MIT attribution headers on every ported file
- ✅ Vitest explicit imports on every test file
- ✅ Branch: `feat/teams-dashboard-charts`
- ✅ Final task pushes + opens PR; does NOT merge
- ✅ frontend-design skill applied during Task 5 (visual quality pass)
