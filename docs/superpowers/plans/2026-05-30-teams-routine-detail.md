# Teams Routine Detail + Run-History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** A routine detail page (`/teams/routines/{id}`) with a rich header (title/status/schedule/assignee) + a read-only run-history list — the deferred H "routine run-history" item.

**Architecture:** A thin BFF passthrough `GET /routines/{id}` (mirrors `get_project`) + a `RoutineDetailPanel` reading `/routines/{id}` (rich `RoutineDetail`) and the existing `/routines/{id}/runs`; `RoutinesPanel` rows get an "Open →" link.

**Tech Stack:** FastAPI BFF (admin client passthrough), Next.js App Router route page, React 19, `useTeamsApi().read`, `timeAgo`.

Conventions: backend from `apps/backend` (`uv run pytest <path> -q`); frontend from `apps/frontend` (`pnpm test -- <substr>`). Per-task targeted tests; full suite/tsc/build in the final task. SWR key for `read("/x")` is `/teams/x`. New frontend tests in `src/__tests__/teams/`.

---

## Task 1: BFF GET /routines/{id}

**Files:** Modify `apps/backend/core/services/paperclip_admin_client.py`, `apps/backend/routers/teams/work.py`; Test `apps/backend/tests/unit/routers/teams/test_routine_detail.py` (new).

- [ ] **Step 1: Failing test** — `tests/unit/routers/teams/test_routine_detail.py`:
```python
# apps/backend/tests/unit/routers/teams/test_routine_detail.py
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


def test_get_routine_forwards(client, monkeypatch):
    admin = MagicMock()
    admin.get_routine = AsyncMock(return_value={"id": "r1", "title": "Nightly digest", "status": "active"})
    from routers.teams import agents as agents_mod
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/routines/r1", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {"id": "r1", "title": "Nightly digest", "status": "active"}
    admin.get_routine.assert_awaited_once_with(routine_id="r1", session_cookie="cookie")
```

- [ ] **Step 2: Run → fail.** `uv run pytest tests/unit/routers/teams/test_routine_detail.py -q`

- [ ] **Step 3: admin method** — add to `paperclip_admin_client.py` after `list_routine_runs` (mirror `get_project`):
```python
    async def get_routine(
        self,
        *,
        session_cookie: str,
        routine_id: str,
    ) -> dict:
        """Fetch a single routine by id (the rich RoutineDetail).

        Maps to ``GET /api/routines/{routineId}``.
        """
        return await self._get(
            f"/api/routines/{routine_id}",
            session_cookie=session_cookie,
        )
```

- [ ] **Step 4: BFF route** — add to `routers/teams/work.py` next to the other `/routines/{routine_id}` routes (mirror the `get_project` route at the bottom of the file — uses `_agents._admin()` + `Depends(_ctx)`):
```python
@router.get("/routines/{routine_id}")
async def get_routine(routine_id: str, ctx: TeamsContext = Depends(_ctx)):
    """Fetch a single routine by id (the routine detail page)."""
    return await _agents._admin().get_routine(
        routine_id=routine_id,
        session_cookie=ctx.session_cookie,
    )
```
(Confirm `work.py` already imports `from . import agents as _agents`, `from .deps import TeamsContext`, and defines `_ctx = _agents._ctx` — the existing `get_project` route uses exactly these. If `_ctx`/imports differ, match what `get_project` uses.) Route-ordering note: `/routines/{routine_id}` and `/routines/{routine_id}/runs` are distinct paths — no shadowing.

- [ ] **Step 5: Run → pass.** `uv run pytest tests/unit/routers/teams/test_routine_detail.py -q`. Ensure ruff clean: `uv run ruff check core/services/paperclip_admin_client.py routers/teams/work.py` AND `uv run ruff format --check tests/unit/routers/teams/test_routine_detail.py core/services/paperclip_admin_client.py routers/teams/work.py` (run `uv run ruff format <files>` if it flags anything, then re-stage).

- [ ] **Step 6: Commit** — `git commit -m "feat(teams): BFF GET /routines/{id} (single routine detail)"`

---

## Task 2: Types + RoutineDetailPanel + route + RoutinesPanel link

**Files:** Modify `apps/frontend/src/components/teams/shared/types.ts`, `apps/frontend/src/components/teams/panels/RoutinesPanel.tsx`; Create `apps/frontend/src/components/teams/panels/RoutineDetailPanel.tsx` + `apps/frontend/src/app/teams/routines/[routineId]/page.tsx`; Test `apps/frontend/src/__tests__/teams/RoutineDetailPanel.test.tsx` (new).

- [ ] **Step 1: Add types** to `shared/types.ts` (near `IssueRun`):
```ts
/** Assignee summary on a routine (upstream RoutineAgentSummary subset). */
export interface RoutineAgentSummary {
  id: string;
  name: string;
  role?: string;
  title?: string | null;
}

/** A routine trigger / schedule (upstream RoutineTrigger subset). */
export interface RoutineTriggerSummary {
  id: string;
  kind: string;
  label?: string | null;
  enabled?: boolean;
  cronExpression?: string | null;
  timezone?: string | null;
  nextRunAt?: string | null;
}

/** Single routine from GET /routines/{id} (upstream RoutineDetail subset). */
export interface RoutineDetail {
  id: string;
  title: string;
  status?: string | null;
  description?: string | null;
  assigneeAgentId?: string | null;
  assignee?: RoutineAgentSummary | null;
  triggers?: RoutineTriggerSummary[];
}

/** A routine run (upstream RoutineRunSummary subset). `status`/`source` are free strings. */
export interface RoutineRun {
  id: string;
  source: string;
  status: string;
  triggeredAt?: string | null;
  completedAt?: string | null;
  failureReason?: string | null;
  linkedIssue?: { id: string; identifier?: string | null; title?: string | null } | null;
  trigger?: { id: string; kind: string; label?: string | null } | null;
}
```

- [ ] **Step 2: Failing test** — `src/__tests__/teams/RoutineDetailPanel.test.tsx`:
```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { RoutineDetailPanel } from "@/components/teams/panels/RoutineDetailPanel";

function mockReads(routine: Record<string, unknown>, runs: Record<string, unknown>[]) {
  read.mockImplementation((path: string) => {
    if (path === "/routines/r1")
      return { data: routine, isLoading: false, error: null, mutate: vi.fn() };
    if (path === "/routines/r1/runs")
      return { data: { runs }, isLoading: false, error: null, mutate: vi.fn() };
    return { data: undefined, isLoading: false, error: null, mutate: vi.fn() };
  });
}

beforeEach(() => read.mockReset());

describe("RoutineDetailPanel", () => {
  it("renders the header (title, status, schedule, assignee) and run history newest-first", () => {
    mockReads(
      {
        id: "r1", title: "Nightly digest", status: "active",
        assignee: { id: "ag1", name: "Atlas" },
        triggers: [{ id: "t1", kind: "cron", cronExpression: "0 9 * * *", timezone: "UTC", enabled: true }],
      },
      [
        { id: "run-old", source: "schedule", status: "succeeded", triggeredAt: "2026-05-01T00:00:00Z" },
        { id: "run-new", source: "manual", status: "failed", triggeredAt: "2026-05-03T00:00:00Z",
          failureReason: "boom", linkedIssue: { id: "iss9", identifier: "ENG-9" } },
      ],
    );
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.getByText("Nightly digest")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("0 9 * * *")).toBeInTheDocument();
    expect(screen.getByText("Atlas")).toBeInTheDocument();
    // run history present; failed run shows its reason + a linked-issue link
    expect(screen.getByText(/failed/i)).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "ENG-9" })).toHaveAttribute("href", "/teams/issues/iss9");
  });

  it("shows 'No runs yet.' when there are no runs", () => {
    mockReads({ id: "r1", title: "Empty routine", status: "active", triggers: [] }, []);
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.getByText("Empty routine")).toBeInTheDocument();
    expect(screen.getByText(/no runs yet/i)).toBeInTheDocument();
  });

  it("renders not-found when the routine read is empty", () => {
    read.mockReturnValue({ data: undefined, isLoading: false, error: null, mutate: vi.fn() });
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.getByText(/routine not found/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run → fail.** `pnpm test -- RoutineDetailPanel`

- [ ] **Step 4: Create `RoutineDetailPanel.tsx`:**
```tsx
// apps/frontend/src/components/teams/panels/RoutineDetailPanel.tsx
"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { RoutineDetail, RoutineRun } from "@/components/teams/shared/types";

function runStatusColor(status: string): string {
  const s = status.toLowerCase();
  if (s === "failed" || s === "error" || s === "cancelled") return "var(--danger)";
  if (s === "succeeded" || s === "completed" || s === "done") return "var(--forest)";
  return "var(--mute)";
}

function runTime(r: RoutineRun): number {
  const t = r.triggeredAt ? new Date(r.triggeredAt).getTime() : 0;
  return Number.isNaN(t) ? 0 : t;
}

export function RoutineDetailPanel({ routineId }: { routineId: string }) {
  const { read } = useTeamsApi();
  const { data: routine, isLoading } = read<RoutineDetail>(`/routines/${routineId}`);
  const { data: runsData } = read<{ runs: RoutineRun[] }>(`/routines/${routineId}/runs`);

  if (isLoading) return <div className="p-8">Loading…</div>;
  if (!routine) return <div className="p-8 text-[var(--mute)]">Routine not found.</div>;

  const runs = [...(runsData?.runs ?? [])].sort((a, b) => runTime(b) - runTime(a)); // newest first
  const triggers = routine.triggers ?? [];

  return (
    <div className="p-8 max-w-3xl flex flex-col gap-5">
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-semibold">{routine.title}</h1>
          {routine.status && (
            <span className="text-xs capitalize px-2 py-0.5 rounded"
              style={{ background: "var(--edge)", color: "var(--mute)" }}>
              {routine.status}
            </span>
          )}
        </div>
        {routine.description && (
          <p className="text-sm text-[var(--ink-soft)]">{routine.description}</p>
        )}
        <div className="text-sm text-[var(--mute)]">
          Assignee:{" "}
          <span className="text-[var(--ink)]">
            {routine.assignee?.name ?? routine.assigneeAgentId ?? "—"}
          </span>
        </div>
      </div>

      <section className="flex flex-col gap-2 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
        <h2 className="section-label"><b>Schedule</b></h2>
        {triggers.length === 0 ? (
          <p className="text-sm text-[var(--mute)]">No triggers.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {triggers.map((t) => (
              <li key={t.id} className="flex items-center gap-2 text-sm">
                <span className="text-[10px] uppercase tracking-wide font-mono px-1.5 py-0.5 rounded flex-shrink-0"
                  style={{ background: "var(--edge)", color: "var(--ink)" }}>
                  {t.kind}
                </span>
                {t.cronExpression && (
                  <span className="font-mono text-xs text-[var(--ink)]">{t.cronExpression}</span>
                )}
                {t.timezone && <span className="text-xs text-[var(--mute)]">{t.timezone}</span>}
                {t.nextRunAt && (
                  <span className="text-xs text-[var(--mute)]">· next {timeAgo(t.nextRunAt)}</span>
                )}
                {t.enabled === false && <span className="text-xs text-[var(--mute)]">· paused</span>}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="flex flex-col gap-2 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
        <h2 className="section-label"><b>Run history</b></h2>
        {runs.length === 0 ? (
          <p className="text-sm text-[var(--mute)]">No runs yet.</p>
        ) : (
          <ul className="flex flex-col">
            {runs.map((r) => (
              <li key={r.id} className="flex flex-col gap-0.5 px-3 py-2"
                  style={{ borderBottom: "1px solid var(--edge)" }}>
                <div className="flex items-center gap-2">
                  <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded flex-shrink-0"
                    style={{ background: "var(--edge)", color: "var(--mute)" }}>
                    {r.source}
                  </span>
                  <span className="text-xs capitalize flex-shrink-0" style={{ color: runStatusColor(r.status) }}>
                    {r.status.replace(/_/g, " ")}
                  </span>
                  {r.trigger && (
                    <span className="text-xs text-[var(--mute)] truncate">
                      {r.trigger.label ?? r.trigger.kind}
                    </span>
                  )}
                  {r.linkedIssue && (
                    <a href={`/teams/issues/${r.linkedIssue.id}`}
                      className="text-xs hover:underline" style={{ color: "var(--forest)" }}>
                      {r.linkedIssue.identifier ?? r.linkedIssue.id.slice(0, 8)}
                    </a>
                  )}
                  <span className="flex-1" />
                  {r.triggeredAt && (
                    <span className="text-xs text-[var(--mute)] flex-shrink-0">{timeAgo(r.triggeredAt)}</span>
                  )}
                </div>
                {r.failureReason && (
                  <div className="text-xs pl-1" style={{ color: "var(--danger)" }}>{r.failureReason}</div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
```

- [ ] **Step 5: Create the route page** `app/teams/routines/[routineId]/page.tsx`:
```tsx
"use client";

import { useParams } from "next/navigation";
import { RoutineDetailPanel } from "@/components/teams/panels/RoutineDetailPanel";

export default function Page() {
  const { routineId } = useParams<{ routineId: string }>();
  return <RoutineDetailPanel routineId={routineId!} />;
}
```

- [ ] **Step 6: Add an "Open →" column to `RoutinesPanel.tsx`.** Read it first. Add an import `import { ... }` is not needed (it's a plain `<a>`). In the `columns` array, append a final column (mirroring `IssuesPanel.tsx`'s open column):
```tsx
    {
      key: "open",
      header: "",
      align: "right",
      cell: (r) => (
        <a
          href={`/teams/routines/${r.id}`}
          className="text-sm text-[var(--forest)] hover:underline"
        >
          Open →
        </a>
      ),
    },
```
(Keep the existing `name`/`enabled`/`actions` columns + Run-now/Delete intact. `r` is the local slim `Routine`; only `r.id` is used here.)

- [ ] **Step 7: Run → pass.** `pnpm test -- RoutineDetailPanel`. If a `RoutinesPanel` test exists, run `pnpm test -- RoutinesPanel` and add an assertion that a row renders an "Open →" link to `/teams/routines/{id}`; keep existing assertions. Then `pnpm exec tsc --noEmit` (clean) + `pnpm exec eslint src/components/teams/panels/RoutineDetailPanel.tsx src/components/teams/panels/RoutinesPanel.tsx "src/app/teams/routines/[routineId]/page.tsx"` (clean).

- [ ] **Step 8: Commit** — `git commit -m "feat(teams): routine detail page + run-history + Open links (routine detail)"`

---

## Task 3: Full verification + roadmap

- [ ] **Step 1:** Backend — `uv run pytest tests/unit/routers/teams/ -q` (the new test + existing teams tests pass).
- [ ] **Step 2:** `pnpm exec tsc --noEmit` (clean).
- [ ] **Step 3:** `pnpm exec eslint src/components/teams/panels/RoutineDetailPanel.tsx src/components/teams/panels/RoutinesPanel.tsx "src/app/teams/routines/[routineId]/page.tsx"` (0 errors).
- [ ] **Step 4:** `pnpm exec vitest run` — tests pass; only the pre-existing local-env-only files may fail (BotSetupWizard, useChatGPTOAuth, CreditsPanel, ChatGPTOAuthStep). Confirm no regression in `RoutineDetailPanel`, `RoutinesPanel`. (vitest may hang at teardown — read per-file results.)
- [ ] **Step 5:** `pnpm run build` — "✓ Compiled successfully" (the new `app/teams/routines/[routineId]` route compiles; a later Clerk-prerender failure on `/admin/health`/marketing pages is the known env-only issue).
- [ ] **Step 6:** Roadmap: add/flip a row for the routine detail + run-history (the resolved deferred-H item) to **Done**. Commit.

---

## Self-Review
- Spec coverage: BFF `get_routine` passthrough (T1); types + `RoutineDetailPanel` (header: title/status/schedule/assignee + run-history newest-first w/ status badge + trigger + linked-issue link + relative time + failureReason) + route page + RoutinesPanel "Open →" (T2); verification + roadmap (T3). Deferred: trigger/secret editing, revision history, activity tab, variables/env, run-now-from-detail (correct).
- Type consistency: `RoutineDetail`/`RoutineRun` shapes used identically in the panel + test; `get_routine(routine_id, session_cookie)` used identically in admin + route + test; `RoutineDetailPanel({routineId})` matches the route wrapper.
- Hooks order: both `read` calls precede the early returns. OK.
- Scoping/safety: `get_routine` passes `ctx.session_cookie` (user's company-scoped session → upstream enforces access; no admin-global IDOR), matching `get_project`. Run `status`/`source` treated as free strings.
- Placeholder scan: none.
