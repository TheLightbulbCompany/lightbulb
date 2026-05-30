# Teams Issue Run List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** A read-only "Runs" section on the `/teams` issue-detail page (newest-first list of an issue's agent runs, each linking to its run detail).

**Architecture:** Thin issue-scoped BFF passthrough (`GET /issues/{id}/runs`) + a new `IssueRunsSection` mounted in `IssueDetailPage`.

**Spec:** `docs/superpowers/specs/2026-05-29-teams-issue-runs-design.md`

Conventions: backend from `apps/backend` (`uv run pytest <path> -q`); frontend from `apps/frontend` (`pnpm test -- <substr>`). Per-task targeted tests; full suite/tsc/build in the final task. SWR key for `read("/x")` is `/teams/x`.

---

## Task 1: BFF GET /issues/{id}/runs

**Files:** Modify `apps/backend/core/services/paperclip_admin_client.py`, `apps/backend/routers/teams/issues.py`; Test `apps/backend/tests/unit/routers/teams/test_issue_runs.py` (new).

- [ ] **Step 1: Failing test** — `tests/unit/routers/teams/test_issue_runs.py`:

```python
# apps/backend/tests/unit/routers/teams/test_issue_runs.py
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


def test_list_issue_runs_forwards_and_envelopes(client, monkeypatch):
    admin = MagicMock()
    admin.list_issue_runs = AsyncMock(return_value=[{"runId": "r1"}, {"runId": "r2"}])
    from routers.teams import agents as agents_mod
    monkeypatch.setattr(agents_mod, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/issues/iss1/runs", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {"runs": [{"runId": "r1"}, {"runId": "r2"}]}
    admin.list_issue_runs.assert_awaited_once_with(issue_id="iss1", session_cookie="cookie")
```

- [ ] **Step 2: Run → fail.** `uv run pytest tests/unit/routers/teams/test_issue_runs.py -q`

- [ ] **Step 3: admin method** (after `get_issue`, mirroring its `_get` style):
```python
    async def list_issue_runs(
        self,
        *,
        session_cookie: str,
        issue_id: str,
    ) -> list | dict:
        """List the agent runs for an issue.

        Maps to ``GET /api/issues/{issueId}/runs`` (issue-scoped).
        """
        return await self._get(
            f"/api/issues/{issue_id}/runs",
            session_cookie=session_cookie,
        )
```

- [ ] **Step 4: BFF route** (append to `routers/teams/issues.py`, near the other `/issues/{id}/...` routes):
```python
@router.get("/issues/{issue_id}/runs")
async def list_issue_runs(issue_id: str, ctx: TeamsContext = Depends(_ctx)):
    """List the agent runs for an issue (the issue-detail Runs section)."""
    rows = await _agents._admin().list_issue_runs(
        issue_id=issue_id,
        session_cookie=ctx.session_cookie,
    )
    return envelope_list(rows, "runs")
```

- [ ] **Step 5: Run → pass.** `uv run pytest tests/unit/routers/teams/test_issue_runs.py -q`. Ensure ruff clean.

- [ ] **Step 6: Commit** — `git commit -m "feat(teams): BFF GET /issues/{id}/runs (per-issue run list)"`

---

## Task 2: IssueRun type + IssueRunsSection + mount

**Files:** Modify `apps/frontend/src/components/teams/shared/types.ts`; Create `apps/frontend/src/components/teams/issues/IssueRunsSection.tsx`; Modify `apps/frontend/src/components/teams/issues/IssueDetailPage.tsx`; Test `apps/frontend/src/__tests__/teams/IssueRunsSection.test.tsx` (new); keep `IssueDetailPage.test.tsx` green.

- [ ] **Step 1: Add the `IssueRun` type** to `shared/types.ts` (near `HeartbeatRun`):
```ts
/** A run for an issue (upstream RunForIssue subset). `status` is a free string. */
export interface IssueRun {
  runId: string;
  status: string;
  agentId?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  createdAt?: string | null;
}
```

- [ ] **Step 2: Failing test** — `IssueRunsSection.test.tsx`:
```tsx
// apps/frontend/src/__tests__/teams/IssueRunsSection.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { IssueRunsSection } from "@/components/teams/issues/IssueRunsSection";

function run(runId: string, agentId: string | null, status: string, startedAt: string) {
  return { runId, agentId, status, startedAt, createdAt: startedAt };
}

beforeEach(() => read.mockReset());

describe("IssueRunsSection", () => {
  it("renders nothing when there are no runs", () => {
    read.mockReturnValue({ data: { runs: [] }, isLoading: false, error: null, mutate: vi.fn() });
    const { container } = render(<IssueRunsSection issueId="iss1" agents={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders runs newest-first with agent name, status, and a View link", () => {
    read.mockReturnValue({
      data: { runs: [
        run("r-old", "ag1", "succeeded", "2026-05-01T00:00:00Z"),
        run("r-new", "ag1", "failed", "2026-05-03T00:00:00Z"),
      ] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<IssueRunsSection issueId="iss1" agents={[{ id: "ag1", name: "Atlas" } as never]} />);
    expect(screen.getByText(/Runs/)).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: /view/i });
    expect(links[0]).toHaveAttribute("href", "/teams/agents/ag1/runs/r-new"); // newest first
    expect(links[1]).toHaveAttribute("href", "/teams/agents/ag1/runs/r-old");
    expect(screen.getAllByText(/Atlas/).length).toBeGreaterThan(0);
    expect(screen.getByText(/failed/i)).toBeInTheDocument();
  });

  it("hides the View link when a run has no agentId", () => {
    read.mockReturnValue({
      data: { runs: [run("r1", null, "succeeded", "2026-05-01T00:00:00Z")] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<IssueRunsSection issueId="iss1" agents={[]} />);
    expect(screen.queryByRole("link", { name: /view/i })).toBeNull();
  });
});
```

- [ ] **Step 3: Run → fail.** `pnpm test -- IssueRunsSection`

- [ ] **Step 4: Implement** — `IssueRunsSection.tsx`:
```tsx
// apps/frontend/src/components/teams/issues/IssueRunsSection.tsx
"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { CompanyAgent, IssueRun } from "@/components/teams/shared/types";

function statusColor(status: string): string {
  const s = status.toLowerCase();
  if (s === "succeeded" || s === "done" || s === "completed") return "var(--forest)";
  if (s === "failed" || s === "error" || s === "cancelled") return "var(--danger)";
  return "var(--mute)";
}

function runTime(r: IssueRun): number {
  const v = r.startedAt ?? r.createdAt ?? null;
  const t = v ? new Date(v).getTime() : 0;
  return Number.isNaN(t) ? 0 : t;
}

export function IssueRunsSection({
  issueId,
  agents,
}: {
  issueId: string;
  agents: CompanyAgent[];
}) {
  const { read } = useTeamsApi();
  const { data } = read<{ runs: IssueRun[] }>(`/issues/${issueId}/runs`);
  const runs = [...(data?.runs ?? [])].sort((a, b) => runTime(b) - runTime(a)); // newest first
  if (runs.length === 0) return null;

  const agentName = (id?: string | null) =>
    agents.find((a) => a.id === id)?.name ?? (id ?? "Agent");

  return (
    <section className="flex flex-col gap-3 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
      <h2 className="section-label"><b>Runs</b></h2>
      <ul className="flex flex-col">
        {runs.map((r) => {
          const when = r.startedAt ?? r.createdAt;
          return (
            <li key={r.runId} className="flex items-center gap-2 px-3 py-2">
              <span
                className="text-xs capitalize flex-shrink-0"
                style={{ color: statusColor(r.status) }}
              >
                {r.status}
              </span>
              <span className="flex-1 truncate text-[var(--ink)]">{agentName(r.agentId)}</span>
              {when && <span className="text-xs text-[var(--mute)]">{timeAgo(when)}</span>}
              {r.agentId && (
                <a
                  href={`/teams/agents/${r.agentId}/runs/${r.runId}`}
                  className="text-sm hover:underline"
                  style={{ color: "var(--forest)" }}
                >
                  View
                </a>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
```

- [ ] **Step 5: Mount in `IssueDetailPage.tsx`** — add `import { IssueRunsSection } from "./IssueRunsSection";`, and render `<IssueRunsSection issueId={issueId} agents={agents} />` immediately AFTER the existing `<IssueApprovalsSection issueId={issueId} />` line and BEFORE the Conversation `<div className="pt-4" ...>` block.

- [ ] **Step 6: Keep `IssueDetailPage.test.tsx` green** — the page now also calls `read("/issues/{id}/runs")`. Its `read` mock has a benign default (`{ data: { issues: [] }, ... }`); `IssueRunsSection` reads `.runs` → `undefined ?? []` → renders nothing. Run `pnpm test -- IssueDetailPage`; adjust the mock default only if it throws (don't weaken assertions).

- [ ] **Step 7: Run → pass.** `pnpm test -- IssueRunsSection IssueDetailPage`

- [ ] **Step 8: Commit** — `git commit -m "feat(teams): IssueRunsSection (per-issue run list) on issue detail"`

---

## Task 3: Full verification + roadmap

- [ ] **Step 1:** Backend — `uv run pytest tests/unit/routers/teams/ -q`.
- [ ] **Step 2:** `pnpm exec tsc --noEmit` (clean).
- [ ] **Step 3:** `pnpm exec eslint src/components/teams/issues/IssueRunsSection.tsx` (0 errors).
- [ ] **Step 4:** `pnpm exec vitest run` — D3 tests pass; only the pre-existing local-env-only files may fail. (vitest may hang at teardown — read per-file results.)
- [ ] **Step 5:** `pnpm run build` — "✓ Compiled successfully".
- [ ] **Step 6:** Roadmap: note D3 alongside D1/D2 under row D. Commit.

---

## Self-Review
- Spec coverage: list endpoint (T1), section newest-first + agent name + status chip + View-link-hidden-when-no-agent + hide-when-empty + mount (T2). Ledger/watchdog/liveness deferred (correct).
- Type consistency: `list_issue_runs(issue_id, session_cookie)` (T1) used identically; `IssueRunsSection({issueId, agents})` matches mount (T2); `IssueRun` shape consistent across type + section + test.
- Placeholder scan: none.
